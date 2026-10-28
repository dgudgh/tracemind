import logging
import re

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.oracle_partition")

_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_TIME_NAME_WEIGHTS = {
    "CREATE_DATE": 45,
    "CREATE_TIME": 45,
    "UPDATE_DATE": 35,
    "FINISH_TIME": 35,
    "BEGIN_TIME": 35,
    "BUSINESS_DATE": 40,
    "WORK_DATE": 40,
    "OP_DATE": 35,
}


def list_partition_candidates(
    instance: DBInstance,
    owner: str | None = None,
    table_name: str | None = None,
    min_rows: int = 5_000_000,
) -> dict:
    owner = _optional_identifier(owner)
    table_name = _optional_identifier(table_name)
    params = {"min_rows": int(min_rows)}
    filters = [
        "t.partitioned = 'NO'",
        "t.temporary = 'N'",
        "t.nested = 'NO'",
        "t.owner NOT IN ('SYS','SYSTEM','SYSMAN','DBSNMP','OUTLN','XDB','WMSYS')",
        "NVL(t.num_rows, 0) >= :min_rows",
    ]
    if owner:
        filters.append("t.owner = :owner")
        params["owner"] = owner
    if table_name:
        filters.append("t.table_name = :table_name")
        params["table_name"] = table_name

    sql = f"""
        SELECT t.owner,
               t.table_name,
               t.num_rows,
               t.blocks,
               ROUND(t.blocks * 8 / 1024, 2) AS approx_size_mb,
               NVL(s.size_mb, 0) AS size_mb,
               t.partitioned,
               t.last_analyzed,
               t.tablespace_name
        FROM dba_tables t
        LEFT JOIN (
            SELECT owner, segment_name, ROUND(SUM(bytes) / 1024 / 1024, 2) AS size_mb
            FROM dba_segments
            WHERE segment_type LIKE 'TABLE%'
            GROUP BY owner, segment_name
        ) s ON s.owner = t.owner AND s.segment_name = t.table_name
        WHERE {' AND '.join(filters)}
        ORDER BY t.num_rows DESC NULLS LAST
    """
    version_info = get_database_version(instance)
    recommendation = recommend_partition_method(version_info)
    rows = _query(instance, sql, params, "分区候选大表查询失败，请确认账号具备 DBA_TABLES/DBA_SEGMENTS 查询权限。")
    for row in rows:
        num_rows = int(row.get("num_rows") or 0)
        row["candidate_level"] = "high" if num_rows >= min_rows * 2 else "medium"
        row["recommended_method"] = recommendation["method"]
        row["suggestion"] = (
            f"非分区大表，系统已按数据库版本推荐改造方式：{recommendation['label']}。"
            if num_rows >= 5_000_000
            else "已达到当前阈值，建议结合业务 SQL 是否按分区键过滤评估收益。"
        )
    return {
        "items": rows,
        "total": len(rows),
        "min_rows": min_rows,
        "owner": owner,
        "table_name": table_name,
        "database_version": version_info,
        "recommendation": recommendation,
    }


def get_table_profile(instance: DBInstance, owner: str, table_name: str, exact_count: bool = False) -> dict:
    owner = _identifier(owner, "owner")
    table_name = _identifier(table_name, "table_name")
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT owner, table_name, num_rows, blocks, partitioned, last_analyzed, tablespace_name,
                   temporary, nested, iot_type, degree
            FROM dba_tables
            WHERE owner = :owner AND table_name = :table_name
            """,
            {"owner": owner, "table_name": table_name},
        )
        tables = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        if not tables:
            raise ValueError(f"表 {owner}.{table_name} 不存在。")
        table = tables[0]

        cursor.execute(
            """
            SELECT owner, segment_name AS table_name, segment_type, tablespace_name,
                   ROUND(SUM(bytes) / 1024 / 1024, 2) AS size_mb
            FROM dba_segments
            WHERE owner = :owner AND segment_name = :table_name AND segment_type LIKE 'TABLE%'
            GROUP BY owner, segment_name, segment_type, tablespace_name
            ORDER BY size_mb DESC
            """,
            {"owner": owner, "table_name": table_name},
        )
        segments = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            """
            SELECT column_name, data_type, nullable, num_distinct, density, num_nulls,
                   histogram, last_analyzed, column_id
            FROM dba_tab_columns
            WHERE owner = :owner AND table_name = :table_name
            ORDER BY column_id
            """,
            {"owner": owner, "table_name": table_name},
        )
        columns = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            """
            SELECT owner, index_name, index_type, uniqueness, partitioned, status,
                   tablespace_name
            FROM dba_indexes
            WHERE table_owner = :owner AND table_name = :table_name
            ORDER BY owner, index_name
            """,
            {"owner": owner, "table_name": table_name},
        )
        indexes = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            """
            SELECT owner, constraint_name, constraint_type, status, r_owner, r_constraint_name
            FROM dba_constraints
            WHERE owner = :owner AND table_name = :table_name
            ORDER BY constraint_type, constraint_name
            """,
            {"owner": owner, "table_name": table_name},
        )
        constraints = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        cursor.execute(
            """
            SELECT owner, trigger_name, status, triggering_event
            FROM dba_triggers
            WHERE table_owner = :owner AND table_name = :table_name
            ORDER BY trigger_name
            """,
            {"owner": owner, "table_name": table_name},
        )
        triggers = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))

        real_count = None
        if exact_count:
            cursor.execute(f"SELECT COUNT(*) FROM {owner}.{table_name}")
            real_count = int(cursor.fetchone()[0])

    version_info = get_database_version(instance)
    return {
        "table": table,
        "segments": segments,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "triggers": triggers,
        "exact_count": real_count,
        "risks": _profile_risks(table, columns, indexes, constraints, triggers),
        "database_version": version_info,
        "recommendation": recommend_partition_method(version_info),
    }


def suggest_partition_keys(instance: DBInstance, owner: str, table_name: str) -> dict:
    profile = get_table_profile(instance, owner, table_name, exact_count=False)
    items = []
    for col in profile["columns"]:
        name = col.get("column_name", "")
        data_type = str(col.get("data_type") or "").upper()
        score = 0
        reasons = []
        if data_type in ("DATE", "TIMESTAMP", "TIMESTAMP(6)", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"):
            score += 35
            reasons.append("日期/时间类型")
        for key, weight in _TIME_NAME_WEIGHTS.items():
            if key in name:
                score += weight
                reasons.append(f"字段名包含 {key}")
                break
        num_nulls = int(col.get("num_nulls") or 0)
        num_distinct = int(col.get("num_distinct") or 0)
        if num_nulls == 0:
            score += 10
            reasons.append("无空值")
        if num_distinct >= 12:
            score += 10
            reasons.append("不同值数量较多")
        if score > 0:
            items.append({
                "column_name": name,
                "data_type": data_type,
                "num_distinct": num_distinct,
                "num_nulls": num_nulls,
                "histogram": col.get("histogram"),
                "score": min(score, 100),
                "reason": "; ".join(reasons),
            })
    items.sort(key=lambda item: item["score"], reverse=True)
    return {"items": items, "owner": profile["table"]["owner"], "table_name": profile["table"]["table_name"]}


def build_partition_convert_plan(instance: DBInstance, payload: dict) -> dict:
    version_info = get_database_version(instance)
    recommendation = recommend_partition_method(version_info)
    method = recommendation["method"]
    payload = {**payload, "method": method}
    if method == "CTAS":
        result = build_ctas_partition_plan(payload)
    elif method == "ALTER_ONLINE":
        result = _build_alter_online_plan(payload)
    else:
        result = build_redefinition_plan(payload)
    result["database_version"] = version_info
    result["recommended_method"] = recommendation
    return result


def get_database_version(instance: DBInstance) -> dict:
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT version FROM v$instance")
            row = cursor.fetchone()
            version = row[0] if row else None
    except Exception as exc:
        logger.warning("Failed to query Oracle version: %s", exc)
        version = None
    major, minor = _parse_version(version)
    return {
        "version": version,
        "major": major,
        "minor": minor,
        "is_122_or_newer": bool(major and (major > 12 or (major == 12 and minor >= 2))),
    }


def recommend_partition_method(version_info: dict) -> dict:
    if version_info.get("is_122_or_newer"):
        return {
            "method": "ALTER_ONLINE",
            "label": "ALTER TABLE MODIFY PARTITION BY ONLINE",
            "reason": "Oracle 12.2 及以上可使用在线分区改造候选方案，执行前必须先在测试库验证。",
        }
    return {
        "method": "DBMS_REDEFINITION",
        "label": "DBMS_REDEFINITION 在线重定义",
        "reason": "Oracle 版本低于 12.2 或无法识别，推荐使用 DBMS_REDEFINITION 在线重定义方案。",
    }


def _parse_version(version: str | None) -> tuple[int | None, int | None]:
    if not version:
        return None, None
    match = re.search(r"(\d+)(?:\.(\d+))?", str(version))
    if not match:
        return None, None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major, minor


def build_redefinition_plan(payload: dict) -> dict:
    owner, table_name, part_col = _payload_names(payload)
    interim = _identifier(payload.get("interim_table_name") or f"{table_name}_INT", "interim_table_name")
    partition_clause = _partition_clause(payload)
    parallel = int(payload.get("parallel") or 4)
    lines = [
        "-- DBMS_REDEFINITION 在线分区改造方案",
        "-- 仅生成 SQL，不直接执行。执行前必须先在测试库验证完整流程。",
        "",
        "BEGIN",
        "  DBMS_REDEFINITION.CAN_REDEF_TABLE(",
        f"    uname        => '{owner}',",
        f"    tname        => '{table_name}',",
        "    options_flag => DBMS_REDEFINITION.CONS_USE_ROWID",
        "  );",
        "END;",
        "/",
        "",
        f"CREATE TABLE {owner}.{interim}",
        partition_clause,
        "AS",
        f"SELECT * FROM {owner}.{table_name} WHERE 1 = 2;",
        "",
        "BEGIN",
        "  DBMS_REDEFINITION.START_REDEF_TABLE(",
        f"    uname        => '{owner}',",
        f"    orig_table   => '{table_name}',",
        f"    int_table    => '{interim}',",
        "    options_flag => DBMS_REDEFINITION.CONS_USE_ROWID",
        "  );",
        "END;",
        "/",
        "",
        "DECLARE",
        "  l_errors PLS_INTEGER;",
        "BEGIN",
        "  DBMS_REDEFINITION.COPY_TABLE_DEPENDENTS(",
        f"    uname            => '{owner}',",
        f"    orig_table       => '{table_name}',",
        f"    int_table        => '{interim}',",
        "    copy_indexes     => DBMS_REDEFINITION.CONS_ORIG_PARAMS,",
        "    copy_triggers    => TRUE,",
        "    copy_constraints => TRUE,",
        "    copy_privileges  => TRUE,",
        "    ignore_errors    => FALSE,",
        "    num_errors       => l_errors",
        "  );",
        "END;",
        "/",
        "",
        "BEGIN",
        "  DBMS_REDEFINITION.SYNC_INTERIM_TABLE(",
        f"    uname      => '{owner}',",
        f"    orig_table => '{table_name}',",
        f"    int_table  => '{interim}'",
        "  );",
        "END;",
        "/",
        "",
        "BEGIN",
        "  DBMS_REDEFINITION.FINISH_REDEF_TABLE(",
        f"    uname      => '{owner}',",
        f"    orig_table => '{table_name}',",
        f"    int_table  => '{interim}'",
        "  );",
        "END;",
        "/",
        "",
        _gather_stats_block(owner, table_name, parallel),
    ]
    return _plan_response("已生成 DBMS_REDEFINITION 在线分区改造 SQL。", "\n".join(lines), part_col)


def build_ctas_partition_plan(payload: dict) -> dict:
    owner, table_name, part_col = _payload_names(payload)
    new_table = _identifier(payload.get("new_table_name") or f"{table_name}_NEW", "new_table_name")
    backup_suffix = _identifier(payload.get("backup_suffix") or "BAK", "backup_suffix")
    parallel = int(payload.get("parallel") or 4)
    partition_clause = _partition_clause(payload)
    lines = [
        "-- CTAS 离线分区改造方案",
        "-- 仅生成 SQL，不直接执行。该方案需要停机窗口，并人工处理依赖对象。",
        "",
        f"CREATE TABLE {owner}.{new_table}",
        partition_clause,
        "AS",
        f"SELECT * FROM {owner}.{table_name} WHERE 1 = 2;",
        "",
        f"INSERT /*+ APPEND PARALLEL({parallel}) */ INTO {owner}.{new_table}",
        f"SELECT /*+ PARALLEL({parallel}) */ * FROM {owner}.{table_name};",
        "COMMIT;",
        "",
        f"ALTER TABLE {owner}.{table_name} RENAME TO {table_name}_{backup_suffix};",
        f"ALTER TABLE {owner}.{new_table} RENAME TO {table_name};",
        "",
        "-- 请人工补充/核对索引、约束、触发器、权限、同义词和注释。",
        _gather_stats_block(owner, table_name, parallel),
    ]
    return _plan_response("已生成 CTAS 离线分区改造 SQL。", "\n".join(lines), part_col)


def _build_alter_online_plan(payload: dict) -> dict:
    owner, table_name, part_col = _payload_names(payload)
    partition_clause = _partition_clause(payload)
    sql = "\n".join([
        "-- ALTER TABLE 在线分区改造候选方案",
        "-- 仅适用于支持在线改造为分区表的 Oracle 版本，执行前必须在测试库验证。",
        f"ALTER TABLE {owner}.{table_name}",
        "MODIFY",
        partition_clause,
        "ONLINE;",
    ])
    return _plan_response("已生成 ALTER TABLE 在线分区改造 SQL。", sql, part_col)


def _partition_clause(payload: dict) -> str:
    part_col = _identifier(payload.get("partition_column"), "partition_column")
    partition_type = str(payload.get("partition_type") or "RANGE_INTERVAL").upper()
    start_less_than = str(payload.get("start_less_than") or "2025-01-01")
    tablespace = _optional_identifier(payload.get("tablespace_name"))
    tablespace_clause = f"\nTABLESPACE {tablespace}" if tablespace else ""
    if partition_type == "RANGE_INTERVAL":
        unit = str(payload.get("interval_unit") or "MONTH").upper()
        interval_value = int(payload.get("interval_value") or (7 if unit == "DAY" else 1))
        interval = (
            f"NUMTOYMINTERVAL({interval_value}, 'MONTH')"
            if unit == "MONTH"
            else f"NUMTOYMINTERVAL({interval_value}, 'YEAR')"
            if unit == "YEAR"
            else f"NUMTODSINTERVAL({interval_value}, 'DAY')"
        )
        return "\n".join([
            f"PARTITION BY RANGE ({part_col})",
            f"INTERVAL ({interval})",
            "(",
            f"  PARTITION P_BEFORE_{start_less_than.replace('-', '')} VALUES LESS THAN (DATE '{start_less_than}')",
            f"){tablespace_clause}",
        ])
    if partition_type == "RANGE":
        return "\n".join([
            f"PARTITION BY RANGE ({part_col})",
            "(",
            f"  PARTITION P_BEFORE_{start_less_than.replace('-', '')} VALUES LESS THAN (DATE '{start_less_than}')",
            f"){tablespace_clause}",
        ])
    if partition_type == "HASH":
        return f"PARTITION BY HASH ({part_col}) PARTITIONS 16{tablespace_clause}"
    return f"PARTITION BY LIST ({part_col}) (PARTITION P_DEFAULT VALUES (DEFAULT)){tablespace_clause}"


def _gather_stats_block(owner: str, table_name: str, degree: int) -> str:
    return "\n".join([
        "BEGIN",
        "  DBMS_STATS.GATHER_TABLE_STATS(",
        f"    ownname          => '{owner}',",
        f"    tabname          => '{table_name}',",
        "    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,",
        "    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',",
        "    cascade          => TRUE,",
        f"    degree           => {degree},",
        "    no_invalidate    => DBMS_STATS.AUTO_INVALIDATE",
        "  );",
        "END;",
        "/",
    ])


def _plan_response(message: str, sql: str, partition_column: str) -> dict:
    return {
        "status": "plan_only",
        "message": message,
        "sql": sql,
        "warnings": [
            "分区表改造风险较高，请先在测试库完整验证。",
            "请确认主键/唯一索引是否包含分区键，否则本地唯一索引会受限。",
            "请确认业务 SQL 是否按分区键过滤，否则分区收益有限。",
            "请检查外键、触发器、同义词、权限、LOB/LONG 字段和全局索引维护成本。",
            f"当前候选分区键：{partition_column}。",
        ],
    }


def _profile_risks(
    table: dict,
    columns: list[dict],
    indexes: list[dict],
    constraints: list[dict],
    triggers: list[dict],
) -> list[str]:
    risks = []
    if table.get("partitioned") == "YES":
        risks.append("该表已经是分区表。")
    if not table.get("last_analyzed"):
        risks.append("统计信息为空，建议先收集统计信息再评估。")
    if any(str(col.get("data_type") or "").upper() == "LONG" for col in columns):
        risks.append("存在 LONG 字段，在线重定义可能受限。")
    if any("LOB" in str(col.get("data_type") or "").upper() for col in columns):
        risks.append("存在 LOB 字段，重定义需要额外验证存储和性能。")
    if any(idx.get("partitioned") == "NO" for idx in indexes):
        risks.append("存在非分区索引，改造后请评估本地化或全局索引维护成本。")
    if any(c.get("constraint_type") in ("P", "U") for c in constraints):
        risks.append("存在主键/唯一约束，请确认唯一约束是否包含分区键。")
    if triggers:
        risks.append("存在触发器，改造后需验证触发器状态和业务逻辑。")
    return risks


def _payload_names(payload: dict) -> tuple[str, str, str]:
    return (
        _identifier(payload.get("owner"), "owner"),
        _identifier(payload.get("table_name"), "table_name"),
        _identifier(payload.get("partition_column"), "partition_column"),
    )


def _query(instance: DBInstance, sql: str, params: dict, error_prefix: str) -> list[dict]:
    try:
        with oracle_connection(instance) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    except Exception as exc:
        raise RuntimeError(f"{error_prefix} 原始错误: {exc}") from exc


def _optional_identifier(value: str | None) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _identifier(value, "identifier")


def _identifier(value: str | None, field: str) -> str:
    normalized = str(value or "").strip().upper()
    if not _IDENT_RE.match(normalized):
        raise ValueError(f"{field} 只能包含字母、数字、_、$、#，且必须以字母开头。")
    return normalized
