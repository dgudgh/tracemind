import logging
import re
from typing import Optional

from app.collectors.oracle.oracle_connection import oracle_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_stats")

SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def _check_safe(name: str, field: str):
    if not name or not SAFE_NAME_PATTERN.match(name):
        raise ValueError(f"{field} 非法：{name}")


def _query(instance: DBInstance, sql: str, bind: dict = None):
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        if bind:
            cursor.execute(sql, bind)
        else:
            cursor.execute(sql)
        cols = [d[0].lower() for d in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(cols, r)) for r in rows]


def _estimate_expr(value) -> str:
    text = str(value or "AUTO").strip().upper()
    if text in ("AUTO", "DBMS_STATS.AUTO_SAMPLE_SIZE"):
        return "DBMS_STATS.AUTO_SAMPLE_SIZE"
    try:
        number = float(text)
        if number <= 0 or number > 100:
            raise ValueError
        return str(int(number) if number.is_integer() else number)
    except Exception as exc:
        raise ValueError("estimate_percent must be AUTO or a number from 0 to 100") from exc


def _no_invalidate_expr(value) -> str:
    text = str(value or "AUTO").strip().upper()
    if text in ("AUTO", "DBMS_STATS.AUTO_INVALIDATE"):
        return "DBMS_STATS.AUTO_INVALIDATE"
    if text in ("TRUE", "FALSE"):
        return text
    raise ValueError("no_invalidate must be AUTO, TRUE or FALSE")


def list_table_stats(instance: DBInstance, owner: str = None, table_name: str = None, stale_only: bool = False):
    bind = {}
    where = "WHERE object_type = 'TABLE' AND owner NOT IN ('SYS', 'SYSTEM')"
    if owner:
        _check_safe(owner, "owner")
        where += " AND owner = :owner"
        bind["owner"] = owner.upper()
    if table_name:
        _check_safe(table_name, "table_name")
        where += " AND table_name = :table_name"
        bind["table_name"] = table_name.upper()
    if stale_only:
        where += " AND stale_stats = 'YES'"

    sql = f"""SELECT owner, table_name, object_type, num_rows, blocks,
                     avg_row_len, sample_size, last_analyzed,
                     stale_stats, stattype_locked, partitioned
              FROM dba_tab_statistics
              {where}
              ORDER BY last_analyzed ASC NULLS FIRST, owner, table_name"""
    return {"items": _query(instance, sql, bind)}


def get_table_stats_detail(instance: DBInstance, owner: str, table_name: str):
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")
    bind = {"owner": owner.upper(), "table_name": table_name.upper()}

    sql = """SELECT owner, table_name, num_rows, blocks, empty_blocks,
                    avg_space, avg_row_len, sample_size, last_analyzed,
                    stale_stats, stattype_locked, partitioned,
                    chain_cnt, row_movement, compression, compress_for,
                    tablespace_name, degree, instances, caching
             FROM dba_tab_statistics t
             LEFT JOIN (SELECT owner, table_name, chain_cnt, row_movement,
                               compression, compress_for, tablespace_name,
                               degree, instances, caching
                        FROM dba_tables WHERE owner = :owner AND table_name = :table_name) tb
             ON t.owner = tb.owner AND t.table_name = tb.table_name
             WHERE t.owner = :owner AND t.table_name = :table_name AND t.object_type = 'TABLE'"""
    rows = _query(instance, sql, bind)
    return {"detail": rows[0] if rows else None}


def list_column_stats(instance: DBInstance, owner: str, table_name: str):
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")
    sql = """SELECT owner, table_name, column_name, data_type,
                    num_distinct, density, num_nulls, num_buckets,
                    histogram, sample_size, last_analyzed
             FROM dba_tab_col_statistics
             WHERE owner = :owner AND table_name = :table_name
             ORDER BY column_name"""
    return {"columns": _query(instance, sql, {"owner": owner.upper(), "table_name": table_name.upper()})}


def list_index_stats(instance: DBInstance, owner: str, table_name: str):
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")
    sql = """SELECT owner, index_name, table_owner, table_name,
                    index_type, blevel, leaf_blocks, distinct_keys,
                    clustering_factor, num_rows, sample_size,
                    last_analyzed, status, uniqueness, degree
             FROM dba_indexes
             WHERE table_owner = :owner AND table_name = :table_name
             ORDER BY owner, index_name"""
    return {"indexes": _query(instance, sql, {"owner": owner.upper(), "table_name": table_name.upper()})}


def list_stats_history(instance: DBInstance, owner: str, table_name: str):
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")
    sql = """SELECT owner, table_name, partition_name, subpartition_name,
                    stats_update_time
             FROM dba_tab_stats_history
             WHERE owner = :owner AND table_name = :table_name
             ORDER BY stats_update_time DESC"""
    return {"history": _query(instance, sql, {"owner": owner.upper(), "table_name": table_name.upper()})}


def list_stats_operations(instance: DBInstance):
    sql = """SELECT operation, target, start_time, end_time, status
             FROM dba_optstat_operations
             ORDER BY start_time DESC"""
    return {"operations": _query(instance, sql)}


def build_gather_table_stats_plan(instance: DBInstance, payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    partition_name = payload.get("partition_name")
    if partition_name:
        _check_safe(partition_name, "partition_name")

    degree = payload.get("degree", 4)
    cascade = "TRUE" if payload.get("cascade", True) else "FALSE"
    method_opt = payload.get("method_opt", "FOR ALL COLUMNS SIZE AUTO")
    granularity = payload.get("granularity", "AUTO")
    estimate_percent = _estimate_expr(payload.get("estimate_percent", "AUTO"))
    no_invalidate = _no_invalidate_expr(payload.get("no_invalidate", "AUTO"))

    warnings = []

    if partition_name:
        _check_safe(partition_name, "partition_name")
        warnings.append(f"将收集分区 {owner}.{table_name} 的 {partition_name} 分区统计信息。")

    table_check = _query(instance,
        "SELECT num_rows, last_analyzed, stale_stats FROM dba_tab_statistics WHERE owner = :o AND table_name = :t AND object_type = 'TABLE'",
        {"o": owner.upper(), "t": table_name.upper()})
    if not table_check:
        raise ValueError(f"表 {owner}.{table_name} 不存在")

    if table_check[0].get("stale_stats") == "YES":
        warnings.append("该表统计信息已过期，收集后可能改变执行计划。")
    if table_check[0].get("last_analyzed") is None:
        warnings.append("该表从未收集过统计信息，首次收集可能耗时较长。")

    lines = [
        "BEGIN",
        "  DBMS_STATS.GATHER_TABLE_STATS(",
        f"    ownname          => '{owner.upper()}',",
        f"    tabname          => '{table_name.upper()}',",
    ]
    if partition_name:
        lines.append(f"    partname         => '{partition_name.upper()}',")
    lines.extend([
        f"    estimate_percent => {estimate_percent},",
        f"    method_opt       => '{method_opt}',",
        f"    cascade          => {cascade},",
        f"    degree           => {degree},",
    ])
    if partition_name:
        lines.append(f"    granularity      => '{granularity}',")
    lines.extend([
        f"    no_invalidate    => {no_invalidate}",
        "  );",
        "END;",
        "/",
    ])

    return {
        "status": "plan_only",
        "message": "已生成表统计信息收集 SQL，请在业务低峰期人工确认后执行。",
        "warnings": warnings,
        "sql": "\n".join(lines),
    }


def build_gather_schema_stats_plan(payload: dict):
    owner = payload["owner"]
    _check_safe(owner, "owner")

    options = payload.get("options", "GATHER STALE")
    degree = payload.get("degree", 4)
    cascade = "TRUE" if payload.get("cascade", True) else "FALSE"
    method_opt = payload.get("method_opt", "FOR ALL COLUMNS SIZE AUTO")
    estimate_percent = _estimate_expr(payload.get("estimate_percent", "AUTO"))
    no_invalidate = _no_invalidate_expr(payload.get("no_invalidate", "AUTO"))

    lines = [
        "BEGIN",
        "  DBMS_STATS.GATHER_SCHEMA_STATS(",
        f"    ownname          => '{owner.upper()}',",
        f"    estimate_percent => {estimate_percent},",
        f"    method_opt       => '{method_opt}',",
        f"    cascade          => {cascade},",
        f"    degree           => {degree},",
        f"    options          => '{options}',",
        f"    no_invalidate    => {no_invalidate}",
        "  );",
        "END;",
        "/",
    ]

    return {
        "status": "plan_only",
        "message": f"已生成 Schema 统计信息收集 SQL（{options}），请在业务低峰期人工确认后执行。",
        "sql": "\n".join(lines),
    }


def build_lock_table_stats_plan(payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    sql = f"""BEGIN
  DBMS_STATS.LOCK_TABLE_STATS(
    ownname => '{owner.upper()}',
    tabname => '{table_name.upper()}'
  );
END;
/"""
    return {"status": "plan_only", "message": "已生成锁定统计信息 SQL，锁定后自动收集将跳过该表。", "sql": sql}


def build_unlock_table_stats_plan(payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    sql = f"""BEGIN
  DBMS_STATS.UNLOCK_TABLE_STATS(
    ownname => '{owner.upper()}',
    tabname => '{table_name.upper()}'
  );
END;
/"""
    return {"status": "plan_only", "message": "已生成解锁统计信息 SQL，解锁后可正常收集统计信息。", "sql": sql}


def build_delete_table_stats_plan(payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    sql = f"""BEGIN
  DBMS_STATS.DELETE_TABLE_STATS(
    ownname => '{owner.upper()}',
    tabname => '{table_name.upper()}'
  );
END;
/"""
    return {
        "status": "plan_only",
        "message": "已生成删除统计信息 SQL。删除统计信息风险较高，可能导致执行计划变化，请谨慎执行。",
        "warnings": ["删除统计信息后，优化器将使用动态采样，可能导致执行计划不稳定。"],
        "sql": sql,
    }
