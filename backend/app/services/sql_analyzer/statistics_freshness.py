from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.collectors.mysql.mysql_connection import mysql_connection
from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.postgresql.postgresql_connection import postgresql_connection
from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS
from app.models.db_instance import DBInstance

STALE_AFTER_DAYS = 30
MAX_TABLES_TO_CHECK = 8


def _profile_value(profile: Any, key: str, default: Any = None) -> Any:
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _db_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _clean_identifier(value: Any) -> str:
    return str(value or "").strip().strip('"`[]')


def _split_table_name(raw_name: Any, default_schema: str | None = None) -> tuple[str | None, str]:
    cleaned = _clean_identifier(raw_name)
    if "." in cleaned:
        schema, table = cleaned.rsplit(".", 1)
        return _clean_identifier(schema) or default_schema, _clean_identifier(table)
    return default_schema, cleaned


def _quote_mysql(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def _quote_pg(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) + chr(34))}"'


def _quote_mssql(value: str) -> str:
    return f"[{value.replace(']', ']]')}]"


def _stringify_dt(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _age_days(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, str):
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value[:19], fmt)
                break
            except ValueError:
                continue
        value = parsed
    if not isinstance(value, datetime):
        return None
    if value.tzinfo:
        value = value.replace(tzinfo=None)
    return max((datetime.utcnow() - value).days, 0)


def _first_datetime(*values: Any) -> Any:
    present = [value for value in values if value]
    if not present:
        return None
    datetimes = [value for value in present if isinstance(value, datetime)]
    if datetimes:
        return max(datetimes)
    return present[0]


def _status_from_age(age_days: int | None, stale_flag: Any = None, locked: Any = None) -> tuple[str, bool, str]:
    stale_text = str(stale_flag or "").upper()
    if locked:
        return "locked", False, "统计信息已锁定，先确认是否允许解锁后再更新。"
    if stale_text in {"YES", "Y", "TRUE", "1"}:
        return "stale", True, "数据库标记统计信息已过期，建议在低峰期重新采集。"
    if age_days is None:
        return "unknown", True, "未获取到统计信息采集时间，建议补采一次统计信息。"
    if age_days >= STALE_AFTER_DAYS:
        return "old", True, f"统计信息已 {age_days} 天未更新，超过 {STALE_AFTER_DAYS} 天阈值。"
    return "fresh", False, f"统计信息 {age_days} 天内更新，暂不需要强制采集。"


def _base_item(db_type: str, schema: str | None, table: str, source: str = "statistics") -> dict[str, Any]:
    object_name = f"{schema}.{table}" if schema else table
    return {
        "name": object_name,
        "schema": schema,
        "table": table,
        "db_type": db_type,
        "source": source,
        "status": "unknown",
        "status_label": "未知",
        "last_analyzed": None,
        "age_days": None,
        "should_update": True,
        "reason": "未获取到统计信息时间。",
        "recommendation": "建议在低峰期生成并复核统计信息更新方案。",
        "script": _generic_script(db_type, schema, table),
        "plan_status": "plan_only",
    }


def _finalize(item: dict[str, Any], last_time: Any, stale_flag: Any = None, locked: Any = None) -> dict[str, Any]:
    age = _age_days(last_time)
    status, should_update, reason = _status_from_age(age, stale_flag, locked)
    labels = {
        "fresh": "较新",
        "old": "偏旧",
        "stale": "已过期",
        "unknown": "未知",
        "locked": "已锁定",
    }
    item.update(
        {
            "status": status,
            "status_label": labels.get(status, status),
            "last_analyzed": _stringify_dt(last_time),
            "age_days": age,
            "should_update": should_update,
            "reason": reason,
            "recommendation": "建议更新统计信息。" if should_update else "当前无需更新统计信息，保持观察。",
        }
    )
    return item


def _generic_script(db_type: str, schema: str | None, table: str) -> str:
    if db_type == "oracle":
        owner = (schema or "USER").upper()
        return (
            "BEGIN\n"
            "  DBMS_STATS.GATHER_TABLE_STATS(\n"
            f"    ownname => '{owner}',\n"
            f"    tabname => '{table.upper()}',\n"
            "    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,\n"
            "    method_opt => 'FOR ALL COLUMNS SIZE AUTO',\n"
            "    cascade => TRUE,\n"
            "    no_invalidate => DBMS_STATS.AUTO_INVALIDATE\n"
            "  );\n"
            "END;\n/"
        )
    if db_type == "mysql":
        prefix = f"{_quote_mysql(schema)}." if schema else ""
        return f"ANALYZE TABLE {prefix}{_quote_mysql(table)};"
    if db_type in {"postgresql", "postgres"}:
        prefix = f"{_quote_pg(schema)}." if schema else ""
        return f"ANALYZE VERBOSE {prefix}{_quote_pg(table)};"
    if db_type in {"sqlserver", "sql_server", "mssql"}:
        prefix = f"{_quote_mssql(schema or 'dbo')}."
        return f"UPDATE STATISTICS {prefix}{_quote_mssql(table)} WITH FULLSCAN;"
    return f"-- plan_only: refresh statistics for {schema + '.' if schema else ''}{table}"


def _cursor_rows(cursor: Any) -> list[dict[str, Any]]:
    columns = [str(col[0]).lower() for col in (cursor.description or [])]
    rows = cursor.fetchall() or []
    if rows and isinstance(rows[0], dict):
        return [{str(k).lower(): v for k, v in row.items()} for row in rows]
    return [dict(zip(columns, row)) for row in rows]


def _oracle_stats(instance: DBInstance, profile: Any, table_name: str) -> dict[str, Any]:
    default_schema = (
        _profile_value(profile, "schema_name")
        or instance.schema_name
        or instance.username
    )
    schema, table = _split_table_name(table_name, str(default_schema or "").upper())
    item = _base_item("oracle", str(schema or "").upper() or None, table.upper())
    with oracle_connection(instance, call_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT owner, table_name, num_rows, blocks, sample_size, last_analyzed,
                   stale_stats, stattype_locked
            FROM dba_tab_statistics
            WHERE owner = :owner AND table_name = :table_name
              AND partition_name IS NULL
            """,
            {"owner": item["schema"], "table_name": item["table"]},
        )
        rows = _cursor_rows(cursor)
    if not rows:
        item["reason"] = "未在 DBA_TAB_STATISTICS 中找到该对象，可能对象名解析失败或权限不足。"
        return item
    row = rows[0]
    item.update(
        {
            "num_rows": row.get("num_rows"),
            "blocks": row.get("blocks"),
            "sample_size": row.get("sample_size"),
            "stale_stats": row.get("stale_stats"),
            "stattype_locked": row.get("stattype_locked"),
            "script": _generic_script("oracle", item["schema"], item["table"]),
        }
    )
    return _finalize(item, row.get("last_analyzed"), row.get("stale_stats"), row.get("stattype_locked"))


def _mysql_stats(instance: DBInstance, profile: Any, table_name: str) -> dict[str, Any]:
    default_schema = _profile_value(profile, "schema_name") or instance.schema_name or instance.database_name
    schema, table = _split_table_name(table_name, default_schema)
    item = _base_item("mysql", schema, table)
    with mysql_connection(instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_schema, table_name, table_rows, avg_row_length,
                       data_length, index_length, update_time, check_time
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            row = cursor.fetchone()
    if not row:
        item["reason"] = "未在 information_schema.tables 中找到该对象，可能对象名解析失败或权限不足。"
        return item
    last_time = row.get("update_time") or row.get("check_time")
    item.update(
        {
            "num_rows": row.get("table_rows"),
            "avg_row_length": row.get("avg_row_length"),
            "data_length": row.get("data_length"),
            "index_length": row.get("index_length"),
            "script": _generic_script("mysql", schema, table),
        }
    )
    return _finalize(item, last_time)


def _postgresql_stats(instance: DBInstance, profile: Any, table_name: str) -> dict[str, Any]:
    default_schema = _profile_value(profile, "schema_name") or instance.schema_name or "public"
    schema, table = _split_table_name(table_name, default_schema)
    item = _base_item("postgresql", schema, table)
    with postgresql_connection(instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT schemaname, relname, n_live_tup, n_dead_tup,
                       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
                FROM pg_stat_user_tables
                WHERE schemaname = %s AND relname = %s
                """,
                (schema, table),
            )
            row = cursor.fetchone()
    if not row:
        item["reason"] = "未在 pg_stat_user_tables 中找到该对象，可能对象名解析失败或权限不足。"
        return item
    last_time = _first_datetime(row.get("last_analyze"), row.get("last_autoanalyze"))
    live = int(row.get("n_live_tup") or 0)
    dead = int(row.get("n_dead_tup") or 0)
    stale_flag = "YES" if live and dead / max(live, 1) >= 0.2 else None
    item.update(
        {
            "num_rows": live,
            "dead_rows": dead,
            "last_vacuum": _stringify_dt(row.get("last_vacuum")),
            "last_autovacuum": _stringify_dt(row.get("last_autovacuum")),
            "last_autoanalyze": _stringify_dt(row.get("last_autoanalyze")),
            "script": _generic_script("postgresql", schema, table),
        }
    )
    return _finalize(item, last_time, stale_flag)


def _sqlserver_stats(instance: DBInstance, profile: Any, table_name: str) -> dict[str, Any]:
    default_schema = _profile_value(profile, "schema_name") or instance.schema_name or "dbo"
    schema, table = _split_table_name(table_name, default_schema)
    item = _base_item("sqlserver", schema or "dbo", table)
    with sqlserver_connection(instance) as conn:
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(
                """
                SELECT s.name AS schema_name, o.name AS table_name, st.name AS stats_name,
                       STATS_DATE(st.object_id, st.stats_id) AS last_updated,
                       SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END) AS row_count
                FROM sys.stats st
                JOIN sys.objects o ON st.object_id = o.object_id
                JOIN sys.schemas s ON o.schema_id = s.schema_id
                LEFT JOIN sys.partitions p ON o.object_id = p.object_id
                WHERE o.type = 'U' AND s.name = %s AND o.name = %s
                GROUP BY s.name, o.name, st.name, st.object_id, st.stats_id
                ORDER BY last_updated ASC
                """,
                (item["schema"], table),
            )
            rows = cursor.fetchall() or []
    if not rows:
        item["reason"] = "未在 sys.stats 中找到该对象，可能对象名解析失败或权限不足。"
        return item
    oldest = rows[0].get("last_updated")
    newest = _first_datetime(*[row.get("last_updated") for row in rows])
    item.update(
        {
            "num_rows": rows[0].get("row_count"),
            "stats_count": len(rows),
            "oldest_stats_time": _stringify_dt(oldest),
            "newest_stats_time": _stringify_dt(newest),
            "script": _generic_script("sqlserver", item["schema"], table),
        }
    )
    return _finalize(item, oldest)


def _inspect_table(instance: DBInstance, profile: Any, table: dict[str, Any]) -> dict[str, Any]:
    db_type = _db_type(_profile_value(profile, "db_type") or instance.db_type)
    table_name = table.get("name") if isinstance(table, dict) else table
    default_schema = _profile_value(profile, "schema_name") or instance.schema_name
    schema, clean_table = _split_table_name(table_name, default_schema)
    try:
        if db_type == "oracle":
            return _oracle_stats(instance, profile, str(table_name))
        if db_type == "mysql":
            return _mysql_stats(instance, profile, str(table_name))
        if db_type in {"postgresql", "postgres"}:
            return _postgresql_stats(instance, profile, str(table_name))
        if db_type in {"sqlserver", "sql_server", "mssql"}:
            return _sqlserver_stats(instance, profile, str(table_name))
    except Exception as exc:
        item = _base_item(db_type, schema, clean_table)
        item["reason"] = f"统计信息采集失败：{exc}"
        item["recommendation"] = "请确认对象名、账号权限和数据库连通性后，再在低峰期执行计划脚本。"
        return item
    return _base_item(db_type, schema, clean_table)


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "fresh": sum(1 for item in items if item.get("status") == "fresh"),
        "stale": sum(1 for item in items if item.get("status") in {"stale", "old"}),
        "unknown": sum(1 for item in items if item.get("status") == "unknown"),
        "should_update": sum(1 for item in items if item.get("should_update")),
    }


def enrich_statistics_freshness(
    db: Session,
    profile: Any,
    related_objects: dict[str, Any],
) -> dict[str, Any]:
    instance_id = _profile_value(profile, "instance_id")
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first() if instance_id else None
    if not instance:
        return related_objects

    enriched = dict(related_objects or {})
    tables = list(enriched.get("tables") or [])[:MAX_TABLES_TO_CHECK]
    statistics = [_inspect_table(instance, profile, table) for table in tables]
    enriched["statistics"] = statistics
    enriched["statistics_summary"] = _summary(statistics)
    return enriched
