import logging
import re
from typing import Optional

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_index")

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


def list_unusable_indexes(instance: DBInstance, owner: str = None, table_name: str = None):
    where = "WHERE status = 'UNUSABLE'"
    bind = {}
    if owner:
        _check_safe(owner, "owner")
        where += " AND owner = :owner"
        bind["owner"] = owner.upper()
    if table_name:
        _check_safe(table_name, "table_name")
        where += " AND table_name = :table_name"
        bind["table_name"] = table_name.upper()

    sql = f"""SELECT owner, index_name, table_owner, table_name,
                     tablespace_name, index_type, uniqueness, status,
                     degree, partitioned, last_analyzed
              FROM dba_indexes {where}
              ORDER BY owner, table_name, index_name"""
    return {"items": _query(instance, sql, bind)}


def list_unusable_index_partitions(instance: DBInstance, owner: str = None, table_name: str = None):
    where = "WHERE status = 'UNUSABLE'"
    bind = {}
    if owner:
        _check_safe(owner, "owner")
        where += " AND index_owner = :owner"
        bind["owner"] = owner.upper()
    if table_name:
        _check_safe(table_name, "table_name")
        where += " AND table_name = :table_name"
        bind["table_name"] = table_name.upper()

    sql_part = f"""SELECT 'PARTITION' AS level_type,
                          index_owner AS owner, index_name,
                          partition_name, NULL AS subpartition_name,
                          tablespace_name, status, last_analyzed
                   FROM dba_ind_partitions {where}
                   ORDER BY index_owner, index_name, partition_position"""

    where2 = "WHERE status = 'UNUSABLE'"
    bind2 = dict(bind)
    if owner:
        where2 += " AND index_owner = :owner"
    if table_name:
        where2 += " AND table_name = :table_name"

    sql_sub = f"""SELECT 'SUBPARTITION' AS level_type,
                         index_owner AS owner, index_name,
                         partition_name, subpartition_name,
                         tablespace_name, status, last_analyzed
                  FROM dba_ind_subpartitions {where2}
                  ORDER BY index_owner, index_name, partition_name, subpartition_position"""

    items = _query(instance, sql_part, bind) + _query(instance, sql_sub, bind2)
    return {"items": items}


def list_abnormal_indexes(instance: DBInstance, owner: str = None, table_name: str = None):
    where = "WHERE NVL(domidx_status, 'VALID') <> 'VALID' OR NVL(domidx_opstatus, 'VALID') <> 'VALID' OR NVL(funcidx_status, 'ENABLED') <> 'ENABLED'"
    bind = {}
    if owner:
        _check_safe(owner, "owner")
        where += " AND owner = :owner"
        bind["owner"] = owner.upper()
    if table_name:
        _check_safe(table_name, "table_name")
        where += " AND table_name = :table_name"
        bind["table_name"] = table_name.upper()

    sql = f"""SELECT owner, index_name, table_name, index_type,
                     domidx_status, domidx_opstatus, funcidx_status, status
              FROM dba_indexes {where}
              ORDER BY owner, table_name, index_name"""
    return {"items": _query(instance, sql, bind)}


def build_rebuild_index_plan(instance: DBInstance, payload: dict):
    owner = payload["owner"]
    index_name = payload["index_name"]
    _check_safe(owner, "owner")
    _check_safe(index_name, "index_name")

    partition_name = payload.get("partition_name")
    subpartition_name = payload.get("subpartition_name")
    tablespace_name = payload.get("tablespace_name")
    online = payload.get("online", True)
    parallel = payload.get("parallel", 1)

    if tablespace_name:
        _check_safe(tablespace_name, "tablespace_name")
    if partition_name:
        _check_safe(partition_name, "partition_name")
    if subpartition_name:
        _check_safe(subpartition_name, "subpartition_name")

    qualified = f"{owner}.{index_name}"

    if subpartition_name:
        target = f"{qualified} REBUILD SUBPARTITION {subpartition_name}"
    elif partition_name:
        target = f"{qualified} REBUILD PARTITION {partition_name}"
    else:
        target = f"{qualified} REBUILD"

    parts = [f"ALTER INDEX {target}"]
    if tablespace_name:
        parts.append(f"TABLESPACE {tablespace_name}")
    if online:
        parts.append("ONLINE")
    if parallel > 1:
        parts.append(f"PARALLEL {parallel}")

    sql_lines = [" ".join(parts) + ";"]
    if parallel > 1 and not partition_name and not subpartition_name:
        sql_lines.append(f"ALTER INDEX {qualified} NOPARALLEL;")

    msg = "已生成重建索引 SQL，请在维护窗口执行。"
    if parallel > 1:
        msg += " 并行重建完成后已自动追加 NOPARALLEL。"

    return {
        "status": "plan_only",
        "message": msg,
        "sql": "\n".join(sql_lines),
    }


def list_table_columns(instance: DBInstance, owner: str, table_name: str):
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    sql = """SELECT column_name, data_type, data_length, data_precision, data_scale,
                    nullable, column_id, num_distinct, density, histogram
             FROM dba_tab_columns
             WHERE owner = :owner AND table_name = :table_name
             ORDER BY column_id"""
    return {"columns": _query(instance, sql, {"owner": owner.upper(), "table_name": table_name.upper()})}


def check_duplicate_index(instance: DBInstance, payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    columns = payload["columns"]
    for c in columns:
        _check_safe(c, "column")
    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")

    col_list = ",".join(columns).upper()
    bind = {"owner": owner.upper(), "table_name": table_name.upper()}

    sql = """SELECT ic.index_owner, ic.index_name,
                    LISTAGG(ic.column_name, ',') WITHIN GROUP (ORDER BY ic.column_position) AS index_columns
             FROM dba_ind_columns ic
             WHERE ic.table_owner = :owner AND ic.table_name = :table_name
             GROUP BY ic.index_owner, ic.index_name"""
    existing = _query(instance, sql, bind)

    duplicates = []
    for idx in existing:
        idx_cols = idx.get("index_columns", "").upper()
        if idx_cols == col_list or idx_cols.startswith(col_list + ",") or col_list.startswith(idx_cols + ","):
            duplicates.append({
                "index_owner": idx["index_owner"],
                "index_name": idx["index_name"],
                "index_columns": idx_cols,
            })

    return {"duplicates": duplicates, "checked_columns": col_list}


def build_create_index_plan(instance: DBInstance, payload: dict):
    owner = payload["owner"]
    table_name = payload["table_name"]
    index_name = payload["index_name"]
    columns = payload["columns"]
    index_type = payload.get("index_type", "NORMAL")
    tablespace_name = payload.get("tablespace_name")
    online = payload.get("online", True)
    parallel = payload.get("parallel", 1)
    compress = payload.get("compress", False)

    _check_safe(owner, "owner")
    _check_safe(table_name, "table_name")
    _check_safe(index_name, "index_name")
    for c in columns:
        _check_safe(c, "column")

    if index_type not in ("NORMAL", "UNIQUE", "BITMAP"):
        raise ValueError("index_type 只能是 NORMAL / UNIQUE / BITMAP")

    warnings = []

    table_sql = "SELECT num_rows, blocks, last_analyzed FROM dba_tables WHERE owner = :o AND table_name = :t"
    tables = _query(instance, table_sql, {"o": owner.upper(), "t": table_name.upper()})
    if not tables:
        raise ValueError(f"表 {owner}.{table_name} 不存在")
    if tables[0].get("last_analyzed") is None:
        warnings.append(f"表 {owner}.{table_name} 统计信息从未收集，建议先执行 DBMS_STATS.GATHER_TABLE_STATS。")

    col_sql = "SELECT column_name FROM dba_tab_columns WHERE owner = :o AND table_name = :t"
    db_cols = {r["column_name"].upper() for r in _query(instance, col_sql, {"o": owner.upper(), "t": table_name.upper()})}
    for c in columns:
        if c.upper() not in db_cols:
            raise ValueError(f"字段 {c} 在 {owner}.{table_name} 中不存在")

    dup_result = check_duplicate_index(instance, {"owner": owner, "table_name": table_name, "columns": columns})
    for d in dup_result["duplicates"]:
        warnings.append(f"已发现相似索引 {d['index_owner']}.{d['index_name']}（{d['index_columns']}），请确认是否重复。")

    if tablespace_name:
        _check_safe(tablespace_name, "tablespace_name")
        ts_sql = "SELECT ROUND(SUM(bytes)/1024/1024, 2) AS free_mb FROM dba_free_space WHERE tablespace_name = :ts GROUP BY tablespace_name"
        ts_rows = _query(instance, ts_sql, {"ts": tablespace_name.upper()})
        if not ts_rows:
            warnings.append(f"表空间 {tablespace_name} 未找到或无可用空间信息。")

    col_expr = ", ".join(columns)
    type_keyword = ""
    if index_type == "UNIQUE":
        type_keyword = "UNIQUE "
    elif index_type == "BITMAP":
        type_keyword = "BITMAP "

    parts = [f"CREATE {type_keyword}INDEX {owner}.{index_name}"]
    parts.append(f"ON {owner}.{table_name} ({col_expr})")
    if tablespace_name:
        parts.append(f"TABLESPACE {tablespace_name}")
    if compress:
        parts.append("COMPRESS")
    if online:
        parts.append("ONLINE")
    if parallel > 1:
        parts.append(f"PARALLEL {parallel}")

    sql_lines = [" ".join(parts) + ";"]
    if parallel > 1:
        sql_lines.append(f"ALTER INDEX {owner}.{index_name} NOPARALLEL;")

    msg = "已生成创建索引 SQL，请确认字段选择性、已有索引和维护窗口后执行。"

    return {
        "status": "plan_only",
        "message": msg,
        "warnings": warnings,
        "sql": "\n".join(sql_lines),
    }
