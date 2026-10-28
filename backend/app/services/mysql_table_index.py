import logging
import re
from collections import defaultdict

from app.collectors.mysql.mysql_connection import mysql_connection
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.mysql_table_index")

SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")
IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")


def _fetch(cur, sql: str, warnings: list | None = None, label: str = "") -> list:
    try:
        cur.execute(sql)
        return list(cur.fetchall() or [])
    except Exception as exc:
        logger.warning("mysql table index query failed [%s]: %s", label, exc)
        if warnings is not None:
            warnings.append(f"{label or '查询'}采集失败：{exc}")
        return []


def _one(cur, sql: str, warnings: list | None = None, label: str = "") -> dict:
    rows = _fetch(cur, sql, warnings, label)
    return rows[0] if rows else {}


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _identifier(name: str) -> str:
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        raise ValueError(f"非法对象名：{name}")
    return f"`{name}`"


def _table_ref(schema: str, table: str) -> str:
    return f"{_identifier(schema)}.{_identifier(table)}"


def _index_rows_to_overview(rows: list) -> list:
    grouped = {}
    for row in rows:
        table_schema = row.get("table_schema") or row.get("TABLE_SCHEMA") or row.get("Table_schema")
        table_name = row.get("table_name") or row.get("TABLE_NAME") or row.get("Table_name")
        index_name = row.get("index_name") or row.get("INDEX_NAME") or row.get("Index_name")
        column_name = row.get("column_name") or row.get("COLUMN_NAME") or row.get("Column_name")
        non_unique = row.get("non_unique")
        if non_unique is None:
            non_unique = row.get("NON_UNIQUE") if row.get("NON_UNIQUE") is not None else row.get("Non_unique")
        index_type = row.get("index_type") or row.get("INDEX_TYPE") or row.get("Index_type")
        cardinality = row.get("cardinality")
        if cardinality is None:
            cardinality = row.get("CARDINALITY") if row.get("CARDINALITY") is not None else row.get("Cardinality")
        key = (table_schema, table_name, index_name)
        item = grouped.setdefault(
            key,
            {
                "table_schema": table_schema,
                "table_name": table_name,
                "index_name": index_name,
                "non_unique": non_unique,
                "index_type": index_type,
                "columns": [],
                "cardinality": cardinality,
            },
        )
        item["columns"].append(column_name)
        if item.get("cardinality") is None:
            item["cardinality"] = cardinality
    result = []
    for item in grouped.values():
        columns = [c for c in item["columns"] if c]
        item["columns"] = ",".join(columns)
        item["column_count"] = len(columns)
        item["unique_text"] = "普通索引" if item.get("non_unique") else "唯一索引"
        result.append(item)
    return sorted(result, key=lambda x: (x.get("table_schema") or "", x.get("table_name") or "", x.get("index_name") or ""))


def _normalize_name_rows(rows: list) -> list:
    result = []
    for row in rows or []:
        item = dict(row)
        if "table_schema" not in item:
            item["table_schema"] = item.get("object_schema") or item.get("schema_name") or item.get("database_name")
        if "table_name" not in item:
            item["table_name"] = item.get("object_name")
        item["table_schema"] = item.get("table_schema") or item.get("TABLE_SCHEMA") or item.get("Table_schema")
        item["table_name"] = item.get("table_name") or item.get("TABLE_NAME") or item.get("Table_name")
        item["engine"] = item.get("engine") or item.get("ENGINE") or item.get("Engine")
        item["table_rows"] = item.get("table_rows") if item.get("table_rows") is not None else item.get("TABLE_ROWS") or item.get("Table_rows")
        item["update_time"] = item.get("update_time") or item.get("UPDATE_TIME") or item.get("Update_time") or item.get("create_time") or item.get("CREATE_TIME") or item.get("Create_time")
        item["create_time"] = item.get("create_time") or item.get("CREATE_TIME") or item.get("Create_time")
        result.append(item)
    return result


def _duplicate_indexes(indexes: list) -> list:
    groups = defaultdict(list)
    for idx in indexes:
        if idx.get("index_name") == "PRIMARY":
            continue
        groups[(idx.get("table_schema"), idx.get("table_name"), idx.get("columns"), idx.get("non_unique"))].append(idx)
    result = []
    for items in groups.values():
        if len(items) > 1:
            names = ",".join(i.get("index_name") or "" for i in items)
            for item in items:
                result.append({**item, "duplicate_group": names, "risk": "完全重复索引"})
    return result


def _redundant_indexes(indexes: list) -> list:
    by_table = defaultdict(list)
    for idx in indexes:
        if idx.get("index_name") != "PRIMARY" and idx.get("columns"):
            by_table[(idx.get("table_schema"), idx.get("table_name"))].append(idx)
    result = []
    for (_, _), items in by_table.items():
        for left in items:
            left_cols = (left.get("columns") or "").split(",")
            for right in items:
                if left is right:
                    continue
                right_cols = (right.get("columns") or "").split(",")
                if len(right_cols) > len(left_cols) and right_cols[: len(left_cols)] == left_cols:
                    result.append(
                        {
                            "table_schema": left.get("table_schema"),
                            "table_name": left.get("table_name"),
                            "index_name": left.get("index_name"),
                            "columns": left.get("columns"),
                            "covered_by_index": right.get("index_name"),
                            "covered_by_columns": right.get("columns"),
                            "risk": "前缀被更长组合索引覆盖，建议结合使用频率评估合并",
                        }
                    )
                    break
    return result


def _build_risks(summary: dict, tables: dict, indexes: dict) -> list:
    risks = []

    def add(level, item, message):
        risks.append({"level": level, "item": item, "message": message})

    if summary.get("large_table_count", 0) > 0:
        add("warning", "大表分析", f"发现 {summary['large_table_count']} 张表超过 10GB，建议评估归档、分区或索引治理。")
    if summary.get("fragmentation_gb", 0) >= 50:
        add("critical", "表碎片", f"碎片空间约 {summary['fragmentation_gb']} GB，建议挑选维护窗口生成 OPTIMIZE 方案。")
    elif summary.get("fragmented_table_count", 0) > 0:
        add("warning", "表碎片", f"发现 {summary['fragmented_table_count']} 张存在明显碎片的表。")
    if summary.get("no_primary_key_table_count", 0) > 0:
        add("warning", "无主键表", f"发现 {summary['no_primary_key_table_count']} 张无主键表，复制、归档和行定位风险较高。")
    if summary.get("non_innodb_table_count", 0) > 0:
        add("warning", "非 InnoDB 表", f"发现 {summary['non_innodb_table_count']} 张非 InnoDB 表。")
    if summary.get("duplicate_index_count", 0) > 0:
        add("warning", "重复索引", f"发现 {summary['duplicate_index_count']} 个重复索引。")
    if summary.get("redundant_index_count", 0) > 0:
        add("warning", "冗余索引", f"发现 {summary['redundant_index_count']} 个疑似冗余索引。")
    if summary.get("unused_index_count", 0) > 0:
        add("warning", "未使用索引", f"发现 {summary['unused_index_count']} 个近期未读索引，建议先观察再处理。")
    for row in tables.get("auto_increment_risk", [])[:5]:
        if _num(row.get("usage_pct")) >= 90:
            add("critical", "自增列风险", f"{row.get('table_schema')}.{row.get('table_name')} 自增列使用率 {row.get('usage_pct')}%。")
    return risks


def get_no_primary_key_tables(instance: DBInstance) -> list:
    return get_table_index_center(instance).get("tables", {}).get("no_primary_key", [])


def get_fragmentation(instance: DBInstance, threshold_mb: int = 100) -> list:
    rows = get_table_index_center(instance).get("tables", {}).get("fragmentation", [])
    return [r for r in rows if _num(r.get("free_mb")) >= threshold_mb]


def get_duplicate_indexes(instance: DBInstance) -> list:
    return get_table_index_center(instance).get("indexes", {}).get("duplicate_indexes", [])


def get_table_index_center(instance: DBInstance) -> dict:
    warnings = []
    with mysql_connection(instance) as conn:
        with conn.cursor() as cur:
            schema_overview = _fetch(
                cur,
                """
                SELECT table_schema,
                       COUNT(*) AS table_count,
                       ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 2) AS total_gb,
                       ROUND(SUM(data_length) / 1024 / 1024 / 1024, 2) AS data_gb,
                       ROUND(SUM(index_length) / 1024 / 1024 / 1024, 2) AS index_gb,
                       ROUND(SUM(data_free) / 1024 / 1024 / 1024, 2) AS free_gb,
                       SUM(table_rows) AS table_rows
                FROM information_schema.tables
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND table_type = 'BASE TABLE'
                GROUP BY table_schema
                ORDER BY total_gb DESC
                """,
                warnings,
                "Schema 概览",
            )
            large_tables = _fetch(
                cur,
                """
                SELECT table_schema, table_name, engine, table_rows,
                       ROUND(data_length / 1024 / 1024, 2) AS data_mb,
                       ROUND(index_length / 1024 / 1024, 2) AS index_mb,
                       ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_mb,
                       update_time, create_time
                FROM information_schema.tables
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND table_type = 'BASE TABLE'
                ORDER BY data_length + index_length DESC
                LIMIT 200
                """,
                warnings,
                "大表分析",
            )
            large_tables = _normalize_name_rows(large_tables)
            fragmentation = _fetch(
                cur,
                """
                SELECT table_schema, table_name, engine, table_rows,
                       ROUND(data_free / 1024 / 1024, 2) AS free_mb,
                       ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_mb,
                       ROUND(data_free / NULLIF(data_length + index_length, 0) * 100, 2) AS frag_pct
                FROM information_schema.tables
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND table_type = 'BASE TABLE'
                  AND data_free > 0
                ORDER BY data_free DESC
                LIMIT 200
                """,
                warnings,
                "表碎片",
            )
            fragmentation = _normalize_name_rows(fragmentation)
            no_primary_key = _fetch(
                cur,
                """
                SELECT t.table_schema, t.table_name, t.engine, t.table_rows,
                       ROUND((t.data_length + t.index_length) / 1024 / 1024, 2) AS total_mb
                FROM information_schema.tables t
                LEFT JOIN information_schema.table_constraints c
                  ON t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                 AND c.constraint_type = 'PRIMARY KEY'
                WHERE t.table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND t.table_type = 'BASE TABLE'
                  AND c.constraint_name IS NULL
                ORDER BY t.table_rows DESC
                LIMIT 300
                """,
                warnings,
                "无主键表",
            )
            no_primary_key = _normalize_name_rows(no_primary_key)
            non_innodb = _fetch(
                cur,
                """
                SELECT table_schema, table_name, engine, table_rows,
                       ROUND((data_length + index_length) / 1024 / 1024, 2) AS total_mb
                FROM information_schema.tables
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND table_type = 'BASE TABLE'
                  AND UPPER(COALESCE(engine, '')) <> 'INNODB'
                ORDER BY total_mb DESC
                LIMIT 200
                """,
                warnings,
                "非 InnoDB 表",
            )
            non_innodb = _normalize_name_rows(non_innodb)
            partitioned = _fetch(
                cur,
                """
                SELECT table_schema, table_name,
                       COUNT(DISTINCT partition_name) AS partition_count,
                       MIN(partition_method) AS partition_method,
                       MIN(partition_expression) AS partition_expression
                FROM information_schema.partitions
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND partition_name IS NOT NULL
                GROUP BY table_schema, table_name
                ORDER BY partition_count DESC
                LIMIT 200
                """,
                warnings,
                "分区表",
            )
            partitioned = _normalize_name_rows(partitioned)
            auto_increment_risk = _fetch(
                cur,
                """
                SELECT c.table_schema, c.table_name, c.column_name, c.column_type,
                       t.auto_increment,
                       CASE
                         WHEN c.column_type LIKE '%tinyint%unsigned%' THEN 255
                         WHEN c.column_type LIKE '%tinyint%' THEN 127
                         WHEN c.column_type LIKE '%smallint%unsigned%' THEN 65535
                         WHEN c.column_type LIKE '%smallint%' THEN 32767
                         WHEN c.column_type LIKE '%mediumint%unsigned%' THEN 16777215
                         WHEN c.column_type LIKE '%mediumint%' THEN 8388607
                         WHEN c.column_type LIKE '%bigint%unsigned%' THEN 18446744073709551615
                         WHEN c.column_type LIKE '%bigint%' THEN 9223372036854775807
                         WHEN c.column_type LIKE '%int%unsigned%' THEN 4294967295
                         WHEN c.column_type LIKE '%int%' THEN 2147483647
                         ELSE NULL
                       END AS max_value,
                       ROUND(t.auto_increment / NULLIF(CASE
                         WHEN c.column_type LIKE '%tinyint%unsigned%' THEN 255
                         WHEN c.column_type LIKE '%tinyint%' THEN 127
                         WHEN c.column_type LIKE '%smallint%unsigned%' THEN 65535
                         WHEN c.column_type LIKE '%smallint%' THEN 32767
                         WHEN c.column_type LIKE '%mediumint%unsigned%' THEN 16777215
                         WHEN c.column_type LIKE '%mediumint%' THEN 8388607
                         WHEN c.column_type LIKE '%bigint%unsigned%' THEN 18446744073709551615
                         WHEN c.column_type LIKE '%bigint%' THEN 9223372036854775807
                         WHEN c.column_type LIKE '%int%unsigned%' THEN 4294967295
                         WHEN c.column_type LIKE '%int%' THEN 2147483647
                         ELSE NULL
                       END, 0) * 100, 4) AS usage_pct
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON c.table_schema = t.table_schema
                 AND c.table_name = t.table_name
                WHERE c.extra LIKE '%auto_increment%'
                  AND c.table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                ORDER BY usage_pct DESC
                LIMIT 100
                """,
                warnings,
                "自增列风险",
            )
            auto_increment_risk = _normalize_name_rows(auto_increment_risk)
            table_statistics = _fetch(
                cur,
                """
                SELECT table_schema, table_name, engine, table_rows, avg_row_length,
                       ROUND(data_length / 1024 / 1024, 2) AS data_mb,
                       ROUND(index_length / 1024 / 1024, 2) AS index_mb,
                       ROUND(data_free / 1024 / 1024, 2) AS free_mb,
                       create_time, update_time, check_time, table_collation
                FROM information_schema.tables
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND table_type = 'BASE TABLE'
                ORDER BY data_length + index_length DESC
                LIMIT 300
                """,
                warnings,
                "表统计信息",
            )
            table_statistics = _normalize_name_rows(table_statistics)
            index_raw = _fetch(
                cur,
                """
                SELECT table_schema, table_name, index_name, non_unique, seq_in_index,
                       column_name, cardinality, index_type
                FROM information_schema.statistics
                WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                ORDER BY table_schema, table_name, index_name, seq_in_index
                """,
                warnings,
                "索引总览",
            )
            index_overview = _index_rows_to_overview(index_raw)
            duplicate_indexes = _duplicate_indexes(index_overview)
            redundant_indexes = _redundant_indexes(index_overview)
            low_cardinality = []
            table_rows_map = {(r.get("table_schema"), r.get("table_name")): _num(r.get("table_rows")) for r in table_statistics}
            for idx in index_overview:
                rows = table_rows_map.get((idx.get("table_schema"), idx.get("table_name")), 0)
                cardinality = _num(idx.get("cardinality"))
                if rows >= 10000 and cardinality >= 0:
                    selectivity = round(cardinality / rows * 100, 4) if rows else 0
                    if selectivity < 1 and idx.get("index_name") != "PRIMARY":
                        low_cardinality.append({**idx, "table_rows": int(rows), "selectivity_pct": selectivity})

            unused_indexes = _fetch(
                cur,
                """
                SELECT object_schema AS table_schema, object_name AS table_name, index_name
                FROM performance_schema.table_io_waits_summary_by_index_usage
                WHERE object_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND index_name IS NOT NULL
                  AND index_name <> 'PRIMARY'
                  AND count_read = 0
                ORDER BY object_schema, object_name
                LIMIT 200
                """,
                warnings,
                "未使用索引",
            )
            unused_indexes = _normalize_name_rows(unused_indexes)
            hot_tables = _fetch(
                cur,
                """
                SELECT object_schema AS table_schema, object_name AS table_name,
                       count_read, count_write, count_fetch, count_insert, count_update, count_delete,
                       ROUND(sum_timer_wait / 1000000000000, 2) AS wait_sec
                FROM performance_schema.table_io_waits_summary_by_table
                WHERE object_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                ORDER BY count_read + count_write DESC
                LIMIT 200
                """,
                warnings,
                "表访问画像",
            )
            hot_tables = _normalize_name_rows(hot_tables)
            hot_indexes = _fetch(
                cur,
                """
                SELECT object_schema AS table_schema, object_name AS table_name, index_name,
                       count_read, count_fetch, count_insert, count_update, count_delete,
                       ROUND(sum_timer_wait / 1000000000000, 2) AS wait_sec
                FROM performance_schema.table_io_waits_summary_by_index_usage
                WHERE object_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND index_name IS NOT NULL
                ORDER BY count_read DESC
                LIMIT 200
                """,
                warnings,
                "索引访问画像",
            )
            hot_indexes = _normalize_name_rows(hot_indexes)
            full_scan_risk = _fetch(
                cur,
                """
                SELECT object_schema AS table_schema, object_name AS table_name,
                       count_read, count_fetch,
                       ROUND(sum_timer_wait / 1000000000000, 2) AS wait_sec
                FROM performance_schema.table_io_waits_summary_by_index_usage
                WHERE object_schema NOT IN ('mysql','information_schema','performance_schema','sys')
                  AND index_name IS NULL
                  AND count_read > 0
                ORDER BY count_read DESC
                LIMIT 200
                """,
                warnings,
                "全表扫描风险",
            )
            full_scan_risk = _normalize_name_rows(full_scan_risk)

    summary = {
        "schema_count": len(schema_overview),
        "table_count": int(sum(_num(r.get("table_count")) for r in schema_overview)),
        "total_size_gb": round(sum(_num(r.get("total_gb")) for r in schema_overview), 2),
        "data_size_gb": round(sum(_num(r.get("data_gb")) for r in schema_overview), 2),
        "index_size_gb": round(sum(_num(r.get("index_gb")) for r in schema_overview), 2),
        "index_count": len(index_overview),
        "large_table_count": len([r for r in large_tables if _num(r.get("total_mb")) >= 10240]),
        "fragmented_table_count": len([r for r in fragmentation if _num(r.get("free_mb")) >= 100]),
        "fragmentation_gb": round(sum(_num(r.get("free_mb")) for r in fragmentation) / 1024, 2),
        "no_primary_key_table_count": len(no_primary_key),
        "non_innodb_table_count": len(non_innodb),
        "duplicate_index_count": len(duplicate_indexes),
        "redundant_index_count": len(redundant_indexes),
        "unused_index_count": len(unused_indexes),
        "low_cardinality_index_count": len(low_cardinality),
    }
    tables = {
        "schema_overview": schema_overview,
        "large_tables": large_tables,
        "fragmentation": fragmentation,
        "no_primary_key": no_primary_key,
        "non_innodb": non_innodb,
        "partitioned": partitioned,
        "auto_increment_risk": auto_increment_risk,
        "statistics": table_statistics,
    }
    indexes = {
        "index_overview": index_overview[:500],
        "duplicate_indexes": duplicate_indexes,
        "redundant_indexes": redundant_indexes,
        "low_cardinality_indexes": low_cardinality[:200],
        "unused_indexes": unused_indexes,
        "missing_index_suggestions": [
            {
                "table_schema": row.get("table_schema"),
                "table_name": row.get("table_name"),
                "evidence": "performance_schema 显示该表存在无索引读访问，请结合慢 SQL 谓词字段评估索引。",
                "suggestion": "先查看慢 SQL 和执行计划，再使用索引创建方案生成 SQL。",
            }
            for row in full_scan_risk[:20]
        ],
    }
    access_profile = {
        "hot_tables": hot_tables,
        "hot_indexes": hot_indexes,
        "full_scan_risk": full_scan_risk,
        "slow_sql_related_tables": [],
    }
    risk_items = _build_risks(summary, tables, indexes)
    summary["risk_level"] = "critical" if any(r["level"] == "critical" for r in risk_items) else ("warning" if risk_items else "normal")
    return {
        "status": "ok",
        "summary": summary,
        "tables": tables,
        "indexes": indexes,
        "access_profile": access_profile,
        "risk_items": risk_items,
        "recommendations": [
            "大表优先结合归档、分区和慢 SQL 谓词字段评估，不建议只按容量盲目 OPTIMIZE。",
            "重复、冗余、未使用索引只生成方案，先设置观察窗口或压测验证，再考虑删除。",
            "无主键大表会影响复制、归档和行定位，建议优先补充业务主键或稳定唯一键。",
        ],
        "warnings": warnings,
    }


def generate_optimize_plan(instance: DBInstance, schema: str, table: str) -> dict:
    return build_optimize_table_plan({"schema": schema, "table": table})


def build_analyze_table_plan(payload: dict) -> dict:
    table = _table_ref(payload.get("schema") or payload.get("table_schema"), payload.get("table") or payload.get("table_name"))
    return {"status": "plan_only", "message": "已生成 ANALYZE TABLE 脚本，请在低峰期人工确认后执行。", "script": f"ANALYZE TABLE {table};"}


def build_optimize_table_plan(payload: dict) -> dict:
    table = _table_ref(payload.get("schema") or payload.get("table_schema"), payload.get("table") or payload.get("table_name"))
    return {
        "status": "plan_only",
        "message": "已生成 OPTIMIZE TABLE 脚本。大表 OPTIMIZE 可能锁表，请在维护窗口执行。",
        "script": f"OPTIMIZE TABLE {table};",
    }


def build_add_index_plan(payload: dict) -> dict:
    schema = payload.get("schema") or payload.get("table_schema")
    table = payload.get("table") or payload.get("table_name")
    index_name = payload.get("index_name") or f"idx_{table}_{'_'.join(payload.get('columns') or [])}"
    columns = payload.get("columns") or []
    if isinstance(columns, str):
        columns = [c.strip() for c in columns.split(",") if c.strip()]
    if not columns:
        raise ValueError("请选择索引列")
    table_ref = _table_ref(schema, table)
    idx = _identifier(index_name)
    cols = ", ".join(_identifier(c) for c in columns)
    unique = "UNIQUE " if payload.get("unique") else ""
    return {
        "status": "plan_only",
        "message": "已生成新增索引 SQL，请先结合慢 SQL、执行计划和写入压力验证。",
        "warnings": ["新增索引会增加写入成本和空间占用，不自动执行。"],
        "script": f"CREATE {unique}INDEX {idx} ON {table_ref} ({cols});",
    }


def build_drop_index_plan(payload: dict) -> dict:
    table_ref = _table_ref(payload.get("schema") or payload.get("table_schema"), payload.get("table") or payload.get("table_name"))
    idx = _identifier(payload.get("index_name"))
    return {
        "status": "plan_only",
        "message": "已生成删除索引 SQL。建议先设置观察窗口，确认无业务影响后再执行。",
        "warnings": ["不要直接删除主键、唯一约束或业务关键索引。"],
        "script": f"ALTER TABLE {table_ref} DROP INDEX {idx};",
    }


def build_add_primary_key_plan(payload: dict) -> dict:
    table_ref = _table_ref(payload.get("schema") or payload.get("table_schema"), payload.get("table") or payload.get("table_name"))
    columns = payload.get("columns") or []
    if isinstance(columns, str):
        columns = [c.strip() for c in columns.split(",") if c.strip()]
    if not columns:
        raise ValueError("请选择主键列")
    cols = ", ".join(_identifier(c) for c in columns)
    return {
        "status": "plan_only",
        "message": "已生成添加主键 SQL，请先检查重复值和业务写入影响。",
        "script": f"ALTER TABLE {table_ref} ADD PRIMARY KEY ({cols});",
    }


def build_archive_suggestion(payload: dict) -> dict:
    table_ref = _table_ref(payload.get("schema") or payload.get("table_schema"), payload.get("table") or payload.get("table_name"))
    column = _identifier(payload.get("archive_column") or "create_time")
    keep_days = int(payload.get("keep_days") or 180)
    condition = f"{column} < DATE_SUB(CURDATE(), INTERVAL {keep_days} DAY)"
    return {
        "status": "plan_only",
        "message": "已生成历史归档评估方案，只生成脚本，不直接清理生产数据。",
        "warnings": ["清理生产表前必须完成归档校验和备份确认。"],
        "script": "\n".join(
            [
                f"SELECT COUNT(*) AS archive_rows FROM {table_ref} WHERE {condition};",
                f"SELECT MIN({column}), MAX({column}) FROM {table_ref} WHERE {condition};",
                f"-- 校验完成后再按批次生成 INSERT INTO history_table SELECT ... FROM {table_ref} WHERE {condition};",
                f"-- 校验归档成功后再生成 DELETE FROM {table_ref} WHERE {condition} LIMIT 10000;",
            ]
        ),
    }
