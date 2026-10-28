from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from typing import Callable

from app.analyzers.rules import Finding
from app.collectors.mysql.mysql_connection import mysql_connection
from app.collectors.postgresql.postgresql_connection import postgresql_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, json_safe
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.inspection.script_db_checks")
_ACTIVE_SQL_TRACE: ContextVar[list[str] | None] = ContextVar(
    "inspection_sql_trace",
    default=None,
)

MYSQL_SECTION_ALIASES = {
    "connection_config": ["mysql.connection.used_pct"],
    "performance": ["mysql.slow_queries.per_min", "mysql.innodb_buffer_pool.hit_pct"],
    "replication_deep": ["mysql.replication.lag_seconds"],
    "backup_check": ["mysql.backup.age_hours"],
    "system_info": ["mysql.disk.used_pct"],
}

POSTGRESQL_SECTION_ALIASES = {
    "connection": ["postgresql.connection.used_pct"],
    "database_info": ["postgresql.database.size_growth_pct"],
    "replication": ["postgresql.replication.lag_seconds"],
    "performance": ["postgresql.deadlocks.count", "postgresql.cache.hit_pct"],
    "system_info": ["postgresql.backup.age_hours"],
}

MYSQL_SECTION_KEYS = (
    "basic_info",
    "connection_config",
    "memory_config",
    "log_config",
    "innodb_config",
    "performance",
    "sampled_metrics",
    "database_info",
    "security",
    "innodb_deep",
    "table_analysis",
    "wait_events",
    "ssl_status",
    "password_policy",
    "autoinc_check",
    "long_queries",
    "replication_deep",
    "backup_check",
    "system_info",
    "expert_tuning",
)

POSTGRESQL_SECTION_KEYS = (
    "basic_info",
    "connection",
    "database_info",
    "table_stats",
    "index_usage",
    "performance",
    "replication",
    "lock_info",
    "long_queries",
    "wait_events",
    "security",
    "settings",
    "system_info",
)

GENERIC_SECTION_KEYS = {
    "mysql": {
        "connection.basic": {
            "basic_info",
            "connection_config",
        },
        "space.capacity": {
            "database_info",
            "table_analysis",
            "autoinc_check",
            "system_info",
        },
        "performance.slow_sql": {
            "memory_config",
            "innodb_config",
            "performance",
            "sampled_metrics",
            "innodb_deep",
            "wait_events",
            "long_queries",
            "expert_tuning",
        },
        "backup.recovery": {
            "log_config",
            "replication_deep",
            "backup_check",
        },
        "security.account": {
            "security",
            "ssl_status",
            "password_policy",
        },
    },
    "postgresql": {
        "connection.basic": {
            "basic_info",
            "connection",
        },
        "space.capacity": {
            "database_info",
            "table_stats",
            "index_usage",
            "system_info",
        },
        "performance.slow_sql": {
            "performance",
            "lock_info",
            "long_queries",
            "wait_events",
        },
        "backup.recovery": {
            "replication",
        },
        "security.account": {
            "security",
            "settings",
        },
    },
}


def collect_mysql_script_findings(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    """MySQL inspection aligned with mysql_check.sh v3.5."""
    allowed_item_keys = _enabled_source_keys(policy_items)
    if allowed_item_keys == set():
        return []
    try:
        with mysql_connection(instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as conn:
            sections = _mysql_sections(conn, allowed_item_keys)
    except Exception as exc:
        logger.warning("MySQL script inspection degraded: %s", exc, exc_info=True)
        return [_runtime_degraded("MySQL", "mysql_check.sh", "3.5", exc)]
    return _sections_to_findings("MySQL", "mysql_check.sh", "3.5", 20, sections)


def collect_postgresql_script_findings(
    instance: DBInstance,
    policy_items: list[dict] | None = None,
) -> list[Finding]:
    """PostgreSQL inspection aligned with pg_check.sh v1.0."""
    allowed_item_keys = _enabled_source_keys(policy_items)
    if allowed_item_keys == set():
        return []
    try:
        with postgresql_connection(instance, statement_timeout_ms=COLLECTION_QUERY_TIMEOUT_SECONDS * 1000) as conn:
            sections = _pg_sections(conn, allowed_item_keys)
    except Exception as exc:
        logger.warning("PostgreSQL script inspection degraded: %s", exc, exc_info=True)
        return [_runtime_degraded("PostgreSQL", "pg_check.sh", "1.0", exc)]
    return _sections_to_findings("PostgreSQL", "pg_check.sh", "1.0", 13, sections)


def _enabled_source_keys(policy_items: list[dict] | None) -> set[str] | None:
    from app.services.inspection.custom_metric_service import enabled_metric_source_keys

    return enabled_metric_source_keys(policy_items)


def _matched_section_item_keys(
    engine: str,
    section_key: str,
    aliases: dict[str, list[str]],
    allowed_item_keys: set[str] | None,
) -> list[str]:
    if allowed_item_keys is None:
        return []
    candidates = [f"{engine}.script.{section_key}", *aliases.get(section_key, [])]
    matches = [key for key in candidates if key in allowed_item_keys]
    for generic_key, section_keys in GENERIC_SECTION_KEYS.get(engine, {}).items():
        if generic_key in allowed_item_keys and section_key in section_keys:
            matches.append(generic_key)
    return matches


def _fetch(conn, sql: str, params=None) -> list[dict]:
    trace = _ACTIVE_SQL_TRACE.get()
    statement = str(sql or "").strip()
    if trace is not None and statement and statement not in trace:
        trace.append(statement)
    cursor = None
    try:
        try:
            cursor = conn.cursor(as_dict=True)
        except TypeError:
            cursor = conn.cursor()
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)
        rows = cursor.fetchall() or []
        columns = [col[0] for col in (cursor.description or [])]
        result: list[dict] = []
        for row in rows:
            if isinstance(row, dict):
                result.append(dict(row))
            else:
                result.append({columns[index]: value for index, value in enumerate(row)})
        return result
    finally:
        if cursor is not None:
            cursor.close()


def _one(conn, sql: str, params=None) -> dict:
    rows = _fetch(conn, sql, params)
    return rows[0] if rows else {}


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _quote_names(names: list[str]) -> str:
    return ", ".join("'" + item.replace("'", "''") + "'" for item in names)


def _safe_fetch(conn, sql: str, warnings: list[str], label: str) -> list[dict]:
    try:
        return _fetch(conn, sql)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        warnings.append(f"{label} 采集失败：{exc}")
        return [{"subsection": label, "采集状态": "失败", "error": str(exc)}]


def _safe_fetch_any(conn, sqls: list[str], warnings: list[str], label: str) -> list[dict]:
    errors: list[str] = []
    for sql in sqls:
        try:
            return _fetch(conn, sql)
        except Exception as exc:
            errors.append(str(exc))
            try:
                conn.rollback()
            except Exception:
                pass
    warnings.append(f"{label} 采集失败：" + " | ".join(errors[:2]))
    return [{"subsection": label, "采集状态": "失败", "error": " | ".join(errors[:2])}]


def _tag(label: str, rows: list[dict]) -> list[dict]:
    if not rows:
        return [{"subsection": label, "采集状态": "无数据", "说明": "当前查询未返回明细行，等价于脚本中的空表/无相关对象。", "_empty": True}]
    tagged: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            tagged.append({"subsection": label, **row})
        else:
            tagged.append({"subsection": label, "value": row})
    return tagged


def _kv(label: str, value, subsection: str = "概要") -> dict:
    return {"subsection": subsection, "项目": label, "值": value}


def _real_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not row.get("_empty") and "error" not in row]


def _row_numbers(rows: list[dict], *names: str) -> list[float]:
    expected = {re.sub(r"[^a-z0-9]", "", name.lower()) for name in names}
    values: list[float] = []
    for row in _real_rows(rows):
        for key, value in row.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized not in expected or value in (None, "", "N/A"):
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
    return values


def _template_metric_override(engine: str, section: dict, item_key: str | None) -> dict | None:
    if not item_key:
        return None
    summary = section.get("summary") or {}
    rows = section.get("items") or []
    value = None

    if item_key == "mysql.connection.used_pct":
        value = summary.get("max_used_pct")
    elif item_key == "mysql.slow_queries.per_min":
        value = summary.get("slow_queries_per_min")
    elif item_key == "mysql.innodb_buffer_pool.hit_pct":
        value = summary.get("buffer_pool_hit_pct")
    elif item_key == "mysql.replication.lag_seconds":
        candidates = _row_numbers(
            rows,
            "heartbeat_lag_seconds",
            "seconds_behind_source",
            "seconds_behind_master",
        )
        value = max(candidates) if candidates else None
    elif item_key == "postgresql.connection.used_pct":
        value = summary.get("used_pct")
    elif item_key == "postgresql.replication.lag_seconds":
        candidates = _row_numbers(
            rows,
            "replay_lag_seconds",
            "write_lag_seconds",
            "flush_lag_seconds",
        )
        value = max(candidates) if candidates else None
    elif item_key == "postgresql.deadlocks.count":
        candidates = _row_numbers(rows, "deadlocks")
        value = sum(candidates) if candidates else None
    elif item_key == "postgresql.cache.hit_pct":
        candidates = _row_numbers(rows, "hit_pct")
        value = min(candidates) if candidates else None

    if value is None and item_key in {
        "mysql.backup.age_hours",
        "mysql.disk.used_pct",
        "postgresql.backup.age_hours",
    }:
        return {
            "metric_name": item_key,
            "metric_value": "unverified",
            "threshold_eligible": False,
            "engine": engine,
        }
    if value is None:
        return None
    return {
        "metric_name": item_key,
        "metric_value": value,
        "threshold_eligible": True,
        "engine": engine,
    }


def _section(
    order,
    key: str,
    title: str,
    *,
    summary: dict | None = None,
    items: list[dict] | None = None,
    sql: str | list[str] | None = None,
    status: str | None = None,
    metric_name: str | None = None,
    metric_value=None,
    threshold_value: str = "ok",
    finding: str | None = None,
    suggestion: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    rows = items or []
    warning_rows = warnings or []
    return json_safe(
        {
            "order": order,
            "key": key,
            "html_section_id": key,
            "title": title,
            "summary": summary or {},
            "items": rows,
            "sql": [sql] if isinstance(sql, str) else (sql or []),
            "status": status or ("warning" if warning_rows else "ok"),
            "metric_name": metric_name or key,
            "metric_value": len(rows) if metric_value is None else metric_value,
            "threshold_value": threshold_value,
            "finding": finding,
            "suggestion": suggestion,
            "warnings": warning_rows,
        }
    )


def _safe_section(order, key: str, title: str, collector: Callable[[], dict], sql: str | list[str]) -> dict:
    executed_sql: list[str] = []
    trace_token = _ACTIVE_SQL_TRACE.set(executed_sql)
    try:
        section = collector()
        if executed_sql:
            section["sql"] = executed_sql
        elif not section.get("sql"):
            section["sql"] = [sql] if isinstance(sql, str) else sql
        return section
    except Exception as exc:
        logger.warning("script inspection section degraded | section=%s | error=%s", key, exc)
        return _section(
            order,
            key,
            title,
            sql=executed_sql or sql,
            status="warning",
            metric_name="collector_status",
            metric_value="degraded",
            finding=f"{title} 采集失败：{exc}",
            suggestion="检查账号权限、数据库版本和 performance_schema/系统视图是否可读；本项仅降级，不阻断其他章节。",
            warnings=[str(exc)],
        )
    finally:
        _ACTIVE_SQL_TRACE.reset(trace_token)


def _sections_to_findings(engine: str, script_name: str, script_version: str, expected_total: int, sections: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for section in sections:
        warnings = section.get("warnings") or []
        status = section.get("status") or ("warning" if warnings else "ok")
        title = section.get("title") or section.get("key")
        order = section.get("order")
        template_item_keys = section.get("_template_item_keys") or [None]
        for template_item_key in template_item_keys:
            metric_override = _template_metric_override(
                engine.lower(),
                section,
                template_item_key,
            )
            raw_section = {
                key: value
                for key, value in section.items()
                if not key.startswith("_")
            }
            raw = json_safe(
                {
                    **raw_section,
                    **(
                        {
                            "metric_name": metric_override["metric_name"],
                            "metric_value": metric_override["metric_value"],
                        }
                        if metric_override
                        else {}
                    ),
                    "engine": engine,
                    "script_name": script_name,
                    "script_version": script_version,
                    "expected_section_total": expected_total,
                    "actual_section_total": len(sections),
                    "template_item_key": template_item_key,
                    "template_threshold_eligible": bool(
                        metric_override
                        and metric_override.get("threshold_eligible", True)
                    ),
                    "read_only": True,
                }
            )
            findings.append(
                Finding(
                    check_category=f"{order}. {title}" if order else title,
                    check_item=f"{title} 巡检",
                    check_status=status,
                    metric_name=(
                        metric_override.get("metric_name")
                        if metric_override
                        else section.get("metric_name") or section.get("key") or "collector_status"
                    ),
                    metric_value=str(
                        metric_override.get("metric_value")
                        if metric_override
                        else section.get("metric_value", len(section.get("items") or []))
                    ),
                    threshold_value=str(section.get("threshold_value") or "ok"),
                    finding=section.get("finding") or _default_finding(title, status, section),
                    suggestion=section.get("suggestion") or _default_suggestion(title, status),
                    raw_data=raw,
                )
            )
    return findings


def _default_finding(title: str, status: str, section: dict) -> str:
    warnings = section.get("warnings") or []
    if warnings:
        return f"{title} 采集完成，但存在降级项：" + "；".join(str(item) for item in warnings[:3])
    if status == "warning":
        return f"{title} 巡检发现需要关注的配置或指标。"
    return f"{title} 巡检完成，未发现采集级异常。"


def _default_suggestion(title: str, status: str) -> str:
    if status in {"critical", "warning"}:
        return f"结合 {title} 明细生成 plan_only 处置方案，人工确认后再执行变更。"
    return f"保留 {title} 为基线，后续结合历史巡检和监控趋势持续观察。"


def _runtime_degraded(engine: str, script_name: str, script_version: str, exc: Exception) -> Finding:
    return Finding(
        check_category=f"{engine} 巡检运行",
        check_item=f"{script_name} 兼容巡检",
        check_status="warning",
        metric_name="collector_status",
        metric_value="degraded",
        threshold_value="ok",
        finding=f"{engine} 脚本化巡检启动失败：{exc}",
        suggestion="检查数据库连接、驱动依赖和账号权限。巡检只读执行，不会执行变更。",
        raw_data={
            "engine": engine,
            "script_name": script_name,
            "script_version": script_version,
            "read_only": True,
            "error": str(exc),
        },
    )


def _mysql_sections(conn, allowed_item_keys: set[str] | None = None) -> list[dict]:
    collectors = {
        "basic_info": _mysql_basic_info,
        "connection_config": _mysql_connection_config,
        "memory_config": _mysql_memory_config,
        "log_config": _mysql_log_config,
        "innodb_config": _mysql_innodb_config,
        "performance": _mysql_performance,
        "sampled_metrics": _mysql_sampled_metrics,
        "database_info": _mysql_database_info,
        "security": _mysql_security,
        "innodb_deep": _mysql_innodb_deep,
        "table_analysis": _mysql_table_analysis,
        "wait_events": _mysql_wait_events,
        "ssl_status": _mysql_ssl_status,
        "password_policy": _mysql_password_policy,
        "autoinc_check": _mysql_autoinc_check,
        "long_queries": _mysql_long_queries,
        "replication_deep": _mysql_replication_deep,
        "backup_check": _mysql_backup_check,
        "system_info": _mysql_system_info,
        "expert_tuning": _mysql_expert_tuning,
    }
    section_collectors = [(key, collectors[key]) for key in MYSQL_SECTION_KEYS]
    sections = []
    for section_key, collector in section_collectors:
        matches = _matched_section_item_keys(
            "mysql",
            section_key,
            MYSQL_SECTION_ALIASES,
            allowed_item_keys,
        )
        if allowed_item_keys is not None and not matches:
            continue
        section = collector(conn)
        if matches:
            section["_template_item_keys"] = matches
        sections.append(section)
    return [json_safe(section) for section in sections]


def _mysql_var_rows(conn, names: list[str], warnings: list[str], label: str) -> list[dict]:
    sql = f"SHOW VARIABLES WHERE Variable_name IN ({_quote_names(names)})"
    return _tag(label, _safe_fetch(conn, sql, warnings, label))


def _mysql_status_rows(conn, names: list[str], warnings: list[str], label: str) -> list[dict]:
    sql = f"SHOW GLOBAL STATUS WHERE Variable_name IN ({_quote_names(names)})"
    return _tag(label, _safe_fetch(conn, sql, warnings, label))


def _mysql_row_map(rows: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        name = row.get("variable_name") or row.get("Variable_name") or row.get("参数") or row.get("指标")
        value = row.get("value")
        if value is None:
            value = row.get("variable_value")
        if value is None:
            value = row.get("Value")
        if name is not None:
            result[str(name).lower()] = value
    return result


def _mysql_version_major(conn) -> int:
    row = _one(conn, "SELECT VERSION() AS version")
    match = re.search(r"(\d+)", str(row.get("version") or ""))
    return int(match.group(1)) if match else 0


def _mysql_basic_info(conn) -> dict:
    sql = """
        SELECT
          @@hostname AS hostname,
          VERSION() AS mysql_version,
          @@port AS port,
          @@server_id AS server_id,
          @@version_compile_os AS version_compile_os,
          @@version_compile_machine AS version_compile_machine,
          @@character_set_database AS character_set_database,
          @@character_set_server AS character_set_server,
          @@collation_server AS collation_server,
          @@basedir AS basedir,
          @@datadir AS datadir,
          NOW() AS collect_time
    """
    def collect():
        warnings: list[str] = []
        summary = _one(conn, sql)
        uptime = _safe_fetch(conn, "SHOW GLOBAL STATUS LIKE 'Uptime'", warnings, "Uptime")
        uptime_map = _mysql_row_map(uptime)
        summary["uptime_seconds"] = uptime_map.get("uptime")
        if summary.get("uptime_seconds"):
            summary["startup_estimated"] = f"当前时间减去 {summary['uptime_seconds']} 秒"
        items = [_kv(k, v, "1.1 基础属性") for k, v in summary.items()]
        items.extend(_tag("1.2 Uptime", uptime))
        return _section(1, "basic_info", "基础信息", summary=summary, items=items, sql=[sql, "SHOW GLOBAL STATUS LIKE 'Uptime'"], warnings=warnings)
    return _safe_section(1, "basic_info", "基础信息", collect, sql)


def _mysql_connection_config(conn) -> dict:
    variables = ["max_connections", "interactive_timeout", "wait_timeout", "back_log", "max_allowed_packet", "skip_name_resolve", "open_files_limit"]
    statuses = ["Threads_connected", "Threads_running", "Max_used_connections"]
    def collect():
        warnings: list[str] = []
        var_rows = _mysql_var_rows(conn, variables, warnings, "2.1 连接参数")
        status_rows = _mysql_status_rows(conn, statuses, warnings, "2.2 连接状态")
        values = _mysql_row_map(var_rows + status_rows)
        max_conn = _num(values.get("max_connections"))
        max_used = _num(values.get("max_used_connections"))
        used_pct = round(max_used * 100 / max_conn, 2) if max_conn else 0
        if used_pct >= 80:
            warnings.append(f"Max_used_connections 已达到 max_connections 的 {used_pct}%。")
        if str(values.get("skip_name_resolve", "")).upper() not in {"ON", "1"}:
            warnings.append("skip_name_resolve 未启用，连接建连可能受 DNS 反查影响。")
        summary = {"max_connections": max_conn, "max_used_connections": max_used, "max_used_pct": used_pct}
        return _section(2, "connection_config", "连接配置", summary=summary, items=var_rows + status_rows, metric_value=used_pct, threshold_value="<80%", warnings=warnings)
    return _safe_section(2, "connection_config", "连接配置", collect, "SHOW VARIABLES/STATUS connection")


def _mysql_memory_config(conn) -> dict:
    variables = [
        "innodb_buffer_pool_size", "innodb_buffer_pool_instances", "sort_buffer_size", "join_buffer_size", "thread_cache_size",
        "tmp_table_size", "max_heap_table_size", "read_buffer_size", "read_rnd_buffer_size", "innodb_io_capacity", "thread_stack",
        "key_buffer_size", "binlog_cache_size", "innodb_sort_buffer_size", "table_open_cache", "table_definition_cache",
    ]
    def collect():
        warnings: list[str] = []
        rows = _mysql_var_rows(conn, variables, warnings, "3.1 内存与缓存参数")
        values = _mysql_row_map(rows)
        if _num(values.get("tmp_table_size")) != _num(values.get("max_heap_table_size")):
            warnings.append("tmp_table_size 与 max_heap_table_size 不一致，内存临时表上限以较小值为准。")
        return _section(3, "memory_config", "内存配置", items=rows, summary={"parameter_count": len(rows)}, warnings=warnings)
    return _safe_section(3, "memory_config", "内存配置", collect, "SHOW VARIABLES memory")


def _mysql_log_config(conn) -> dict:
    variables = [
        "slow_query_log", "slow_query_log_file", "long_query_time", "log_bin", "binlog_format", "expire_logs_days",
        "binlog_expire_logs_seconds", "innodb_log_file_size", "innodb_log_buffer_size", "innodb_flush_log_at_trx_commit", "sync_binlog",
    ]
    def collect():
        warnings: list[str] = []
        rows = _mysql_var_rows(conn, variables, warnings, "4.1 日志参数")
        values = _mysql_row_map(rows)
        if str(values.get("slow_query_log", "")).upper() not in {"ON", "1"}:
            warnings.append("slow_query_log 未开启，慢 SQL 画像能力会受限。")
        if str(values.get("log_bin", "")).upper() not in {"ON", "1"}:
            warnings.append("log_bin 未开启，复制、PITR 和 binlog 恢复能力受限。")
        if str(values.get("innodb_flush_log_at_trx_commit", "")) != "1":
            warnings.append("innodb_flush_log_at_trx_commit 非 1，需要确认数据安全与性能取舍。")
        if str(values.get("sync_binlog", "")) not in {"1", "ON"}:
            warnings.append("sync_binlog 非 1，主机异常时 binlog 持久性存在风险。")
        return _section(4, "log_config", "日志配置", items=rows, summary={"parameter_count": len(rows)}, warnings=warnings)
    return _safe_section(4, "log_config", "日志配置", collect, "SHOW VARIABLES log")


def _mysql_innodb_config(conn) -> dict:
    variables = [
        "innodb_file_per_table", "innodb_open_files", "innodb_thread_concurrency", "innodb_flush_method", "innodb_doublewrite",
        "innodb_read_io_threads", "innodb_write_io_threads", "innodb_checksum_algorithm", "innodb_numa_interleave",
        "innodb_max_dirty_pages_pct", "innodb_io_capacity_max", "innodb_adaptive_hash_index", "innodb_change_buffering",
        "innodb_page_size", "innodb_log_files_in_group",
    ]
    def collect():
        warnings: list[str] = []
        rows = _mysql_var_rows(conn, variables, warnings, "5.1 InnoDB 参数")
        values = _mysql_row_map(rows)
        if str(values.get("innodb_file_per_table", "")).upper() not in {"ON", "1"}:
            warnings.append("innodb_file_per_table 未开启，表级空间治理和回收能力受限。")
        if str(values.get("innodb_doublewrite", "")).upper() not in {"ON", "1"}:
            warnings.append("innodb_doublewrite 未开启，异常宕机场景存在页损坏风险。")
        return _section(5, "innodb_config", "InnoDB 配置", items=rows, summary={"parameter_count": len(rows)}, warnings=warnings)
    return _safe_section(5, "innodb_config", "InnoDB 配置", collect, "SHOW VARIABLES innodb")


def _mysql_performance(conn) -> dict:
    query_stats = [
        "Queries", "Questions", "Slow_queries", "Uptime", "Com_select", "Com_insert", "Com_update", "Com_delete", "Com_commit", "Com_rollback",
        "Sort_merge_passes", "Sort_scan", "Sort_range", "Sort_rows", "Select_full_join", "Select_full_range_join", "Select_scan",
        "Select_range", "Select_range_check",
    ]
    innodb_stats = [
        "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads", "Innodb_row_lock_waits", "Innodb_row_lock_time_avg",
        "Innodb_data_reads", "Innodb_data_writes", "Innodb_pages_read", "Innodb_pages_written", "Innodb_buffer_pool_pages_dirty",
        "Innodb_buffer_pool_pages_total", "Innodb_buffer_pool_pages_free", "Innodb_buffer_pool_read_ahead",
        "Innodb_buffer_pool_read_ahead_evicted", "Innodb_log_waits", "Innodb_log_writes", "Innodb_buffer_pool_wait_free",
    ]
    lock_tmp_stats = [
        "Table_locks_immediate", "Table_locks_waited", "Created_tmp_tables", "Created_tmp_disk_tables", "Connections", "Aborted_clients",
        "Aborted_connects", "Threads_created", "Bytes_received", "Bytes_sent", "Opened_tables", "Open_tables", "Opened_files",
        "Table_open_cache_hits", "Table_open_cache_misses", "Table_open_cache_overflows", "Open_table_definitions",
        "Opened_table_definitions", "Binlog_cache_disk_use", "Binlog_cache_use", "Key_reads", "Key_read_requests", "Key_writes",
        "Key_write_requests",
    ]
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _mysql_status_rows(conn, query_stats, warnings, "6.1 查询统计")
        rows += _mysql_status_rows(conn, innodb_stats, warnings, "6.2 InnoDB 性能")
        rows += _mysql_status_rows(conn, lock_tmp_stats, warnings, "6.3 锁 / 临时表 / 连接 / 流量")
        values = _mysql_row_map(rows)
        req = _num(values.get("innodb_buffer_pool_read_requests"))
        reads = _num(values.get("innodb_buffer_pool_reads"))
        hit_pct = round((1 - reads / req) * 100, 2) if req else None
        uptime_seconds = _num(values.get("uptime"))
        slow_queries_per_min = (
            round(_num(values.get("slow_queries")) * 60 / uptime_seconds, 4)
            if uptime_seconds
            else None
        )
        tmp_total = _num(values.get("created_tmp_tables"))
        tmp_disk = _num(values.get("created_tmp_disk_tables"))
        tmp_disk_pct = round(tmp_disk * 100 / tmp_total, 2) if tmp_total else 0
        warnings.extend(
            _mysql_performance_warnings(
                values,
                hit_pct=hit_pct,
                slow_queries_per_min=slow_queries_per_min,
                tmp_disk_pct=tmp_disk_pct,
            )
        )
        summary = {
            "buffer_pool_hit_pct": hit_pct,
            "slow_queries_per_min": slow_queries_per_min,
            "tmp_disk_pct": tmp_disk_pct,
        }
        return _section(6, "performance", "性能指标", summary=summary, items=rows, metric_name="buffer_pool_hit_pct", metric_value=hit_pct, threshold_value=">=95%", warnings=warnings)
    return _safe_section(6, "performance", "性能指标", collect, "SHOW GLOBAL STATUS performance")


def _mysql_performance_warnings(
    values: dict,
    *,
    hit_pct: float | None,
    slow_queries_per_min: float | None,
    tmp_disk_pct: float,
) -> list[str]:
    warnings: list[str] = []
    if hit_pct is not None and hit_pct < 95:
        warnings.append(f"InnoDB Buffer Pool 命中率 {hit_pct}%，低于 95%。")
    if slow_queries_per_min is not None and slow_queries_per_min >= 5:
        warnings.append(
            f"慢查询速率 {slow_queries_per_min} 次/分钟，达到模板警告阈值 5 次/分钟。"
        )
    if _num(values.get("innodb_log_waits")) > 0:
        warnings.append("Innodb_log_waits 大于 0，Redo 日志写入可能存在等待。")
    if tmp_disk_pct >= 30:
        warnings.append(f"磁盘临时表占比 {tmp_disk_pct}%，需关注排序/分组/临时表配置。")
    return warnings


def _mysql_sampled_metrics(conn) -> dict:
    names = [
        "Queries", "Com_select", "Com_insert", "Com_update", "Com_delete", "Com_commit", "Com_rollback",
        "Innodb_data_reads", "Innodb_data_writes", "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
    ]
    def snapshot(warnings: list[str]) -> dict[str, float]:
        rows = _safe_fetch(conn, f"SHOW GLOBAL STATUS WHERE Variable_name IN ({_quote_names(names)})", warnings, "6.5 实时采样快照")
        return {k: _num(v) for k, v in _mysql_row_map(rows).items()}
    def collect():
        warnings: list[str] = []
        interval = 1
        s1 = snapshot(warnings)
        time.sleep(interval)
        s2 = snapshot(warnings)
        def rate(name: str) -> float:
            return round((s2.get(name.lower(), 0) - s1.get(name.lower(), 0)) / interval, 2)
        req_delta = s2.get("innodb_buffer_pool_read_requests", 0) - s1.get("innodb_buffer_pool_read_requests", 0)
        read_delta = s2.get("innodb_buffer_pool_reads", 0) - s1.get("innodb_buffer_pool_reads", 0)
        hit_pct = round((1 - read_delta / req_delta) * 100, 2) if req_delta > 0 else None
        items = [
            _kv("采样间隔", f"{interval} 秒", "6.5 实时采样指标"),
            _kv("QPS (采样)", rate("Queries"), "6.5 实时采样指标"),
            _kv("TPS (采样)", round((rate("Com_commit") + rate("Com_rollback")), 2), "6.5 实时采样指标"),
            _kv("读 QPS (Com_select)", rate("Com_select"), "6.5 实时采样指标"),
            _kv("写 QPS (Insert+Update+Delete)", round(rate("Com_insert") + rate("Com_update") + rate("Com_delete"), 2), "6.5 实时采样指标"),
            _kv("InnoDB 读 IOPS", rate("Innodb_data_reads"), "6.5 实时采样指标"),
            _kv("InnoDB 写 IOPS", rate("Innodb_data_writes"), "6.5 实时采样指标"),
            _kv("Buffer Pool 实时命中率", hit_pct, "6.5 实时采样指标"),
        ]
        return _section("6.5", "sampled_metrics", "实时采样指标", summary={"sample_interval_seconds": interval, "buffer_pool_hit_pct": hit_pct}, items=items, metric_name="qps", metric_value=items[1]["值"], warnings=warnings)
    return _safe_section("6.5", "sampled_metrics", "实时采样指标", collect, "SHOW GLOBAL STATUS sampled")


def _mysql_database_info(conn) -> dict:
    is8 = _mysql_version_major(conn) >= 8
    process_sqls = [
        """
        SELECT id, user, host, IFNULL(db, '') AS db, command, time, IFNULL(state, '') AS state
        FROM performance_schema.processlist
        ORDER BY time DESC
        LIMIT 50
        """,
        """
        SELECT id, user, host, IFNULL(db, '') AS db, command, time, IFNULL(state, '') AS state
        FROM information_schema.PROCESSLIST
        ORDER BY time DESC
        LIMIT 50
        """,
    ] if is8 else [
        """
        SELECT id, user, host, IFNULL(db, '') AS db, command, time, IFNULL(state, '') AS state
        FROM information_schema.PROCESSLIST
        ORDER BY time DESC
        LIMIT 50
        """
    ]
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("7.1 数据库大小", _safe_fetch(conn, """
            SELECT table_schema AS db_name,
                   COALESCE(SUM(table_rows), 0) AS rows_count,
                   ROUND(COALESCE(SUM(data_length), 0) / 1024 / 1024, 2) AS data_mb,
                   ROUND(COALESCE(SUM(index_length), 0) / 1024 / 1024, 2) AS index_mb
            FROM information_schema.TABLES
            WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
            GROUP BY table_schema
            ORDER BY SUM(data_length) DESC
        """, warnings, "7.1 数据库大小"))
        rows += _tag("7.2 当前进程列表", _safe_fetch_any(conn, process_sqls, warnings, "7.2 当前进程列表"))
        rows += _tag("7.3 存储引擎", _safe_fetch(conn, """
            SELECT engine, support, comment
            FROM information_schema.ENGINES
            WHERE support != 'NO'
            ORDER BY support DESC, engine
        """, warnings, "7.3 存储引擎"))
        return _section(7, "database_info", "数据库信息", items=rows, summary={"mysql8_or_newer": is8}, warnings=warnings)
    return _safe_section(7, "database_info", "数据库信息", collect, "information_schema database/process/engine")


def _mysql_security(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("8.1 数据库用户", _safe_fetch(conn, """
            SELECT user, host, Grant_priv AS grant_priv, plugin, account_locked
            FROM mysql.user
            WHERE user NOT IN ('mysql.infoschema','mysql.session','mysql.sys')
            ORDER BY user
        """, warnings, "8.1 数据库用户"))
        rows += _tag("8.2 复制状态", _safe_fetch_any(conn, ["SHOW REPLICA STATUS", "SHOW SLAVE STATUS"], warnings, "8.2 复制状态"))
        rows += _mysql_var_rows(conn, ["server_id", "gtid_mode", "enforce_gtid_consistency"], warnings, "8.3 复制配置")
        user_warnings = [row for row in rows if str(row.get("account_locked", "")).upper() == "N" and str(row.get("host", "")) == "%"]
        if user_warnings:
            warnings.append(f"发现 {len(user_warnings)} 个可从任意主机连接且未锁定的账号。")
        return _section(8, "security", "安全信息", items=rows, summary={"row_count": len(rows)}, warnings=warnings)
    return _safe_section(8, "security", "安全信息", collect, "mysql.user / replication")


def _mysql_innodb_deep(conn) -> dict:
    status_sql = "SHOW ENGINE INNODB STATUS"
    def collect():
        warnings: list[str] = []
        rows = []
        innodb_rows = _safe_fetch(conn, status_sql, warnings, "9.1 InnoDB 状态摘要")
        status_text = ""
        if innodb_rows:
            status_text = str(innodb_rows[0].get("status") or innodb_rows[0].get("Status") or "")
        metrics = {
            "History List Length": _regex_value(status_text, r"History list length\s+(\d+)"),
            "Log Sequence Number": _regex_value(status_text, r"Log sequence number\s+(\d+)"),
            "Modified DB Pages": _regex_value(status_text, r"Modified db pages\s+(\d+)"),
            "Pending Reads": _regex_value(status_text, r"Pending normal aio reads:\s*\[?(\d+)"),
            "Pending Writes": _regex_value(status_text, r"Pending normal aio writes:\s*\[?(\d+)"),
        }
        rows += [_kv(k, v, "9.1 InnoDB 状态摘要") for k, v in metrics.items()]
        deadlock = _extract_deadlock(status_text)
        rows.append({"subsection": "9.2 最近死锁信息", "latest_deadlock": deadlock[:4000] if deadlock else "未检测到死锁"})
        rows += _tag("9.3 长事务 (>60s)", _safe_fetch(conn, """
            SELECT trx_id, trx_state,
                   TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_seconds,
                   trx_rows_locked, trx_rows_modified, LEFT(trx_query, 300) AS sql_text
            FROM information_schema.INNODB_TRX
            WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 60
            ORDER BY trx_started
            LIMIT 20
        """, warnings, "9.3 长事务"))
        rows += _tag("9.4 当前锁等待", _safe_fetch_any(conn, [
            """
            SELECT w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,
                   r.trx_state AS waiting_state,
                   w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id,
                   b.trx_state AS blocking_state,
                   TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds,
                   LEFT(r.trx_query, 200) AS waiting_sql
            FROM performance_schema.data_lock_waits w
            JOIN information_schema.INNODB_TRX r ON w.REQUESTING_ENGINE_TRANSACTION_ID = r.trx_id
            JOIN information_schema.INNODB_TRX b ON w.BLOCKING_ENGINE_TRANSACTION_ID = b.trx_id
            LIMIT 20
            """,
            """
            SELECT r.trx_id AS waiting_trx_id,
                   r.trx_state AS waiting_state,
                   b.trx_id AS blocking_trx_id,
                   b.trx_state AS blocking_state,
                   TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds,
                   LEFT(r.trx_query, 200) AS waiting_sql
            FROM information_schema.INNODB_LOCK_WAITS w
            JOIN information_schema.INNODB_TRX r ON w.requesting_trx_id = r.trx_id
            JOIN information_schema.INNODB_TRX b ON w.blocking_trx_id = b.trx_id
            LIMIT 20
            """,
        ], warnings, "9.4 当前锁等待"))
        if _num(metrics.get("History List Length")) > 100000:
            warnings.append("History List Length 较高，可能存在长事务阻止 undo 清理。")
        if deadlock:
            warnings.append("检测到最近死锁信息，请进入锁等待和 SQL 画像分析。")
        return _section(9, "innodb_deep", "InnoDB 深度诊断", summary=metrics, items=rows, metric_name="history_list_length", metric_value=metrics.get("History List Length"), threshold_value="<100000", warnings=warnings)
    return _safe_section(9, "innodb_deep", "InnoDB 深度诊断", collect, status_sql)


def _regex_value(text: str, pattern: str):
    match = re.search(pattern, text or "", flags=re.IGNORECASE)
    return match.group(1) if match else None


def _extract_deadlock(text: str) -> str:
    match = re.search(r"LATEST DETECTED DEADLOCK(.*?)(?:\n-{3,}|\Z)", text or "", flags=re.S | re.I)
    return match.group(0).strip() if match else ""


def _mysql_table_analysis(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("10.1 Top 20 大表", _safe_fetch(conn, """
            SELECT t.table_schema, t.table_name, t.engine, COALESCE(t.table_rows, 0) AS table_rows,
                   ROUND(COALESCE(t.data_length, 0) / 1024 / 1024, 2) AS data_mb,
                   ROUND(COALESCE(t.index_length, 0) / 1024 / 1024, 2) AS index_mb,
                   ROUND(COALESCE(t.data_free, 0) / 1024 / 1024, 2) AS free_mb,
                   IFNULL(t.create_options, '') AS create_options
            FROM information_schema.TABLES t
            WHERE t.table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              AND t.table_type = 'BASE TABLE'
            ORDER BY (t.data_length + t.index_length) DESC
            LIMIT 20
        """, warnings, "10.1 Top 20 大表"))
        no_pk = _safe_fetch(conn, """
            SELECT t.table_schema, t.table_name, t.engine, COALESCE(t.table_rows, 0) AS table_rows
            FROM information_schema.TABLES t
            LEFT JOIN information_schema.TABLE_CONSTRAINTS tc
              ON t.table_schema = tc.table_schema
             AND t.table_name = tc.table_name
             AND tc.constraint_type = 'PRIMARY KEY'
            WHERE t.table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              AND t.table_type = 'BASE TABLE'
              AND tc.constraint_name IS NULL
            ORDER BY t.table_rows DESC
            LIMIT 50
        """, warnings, "10.2 无主键表")
        rows += _tag("10.2 无主键表", no_pk)
        rows += _tag("10.3 高碎片率表 (碎片 > 30%)", _safe_fetch(conn, """
            SELECT table_schema, table_name,
                   ROUND(data_length / 1024 / 1024, 2) AS data_mb,
                   ROUND(data_free / 1024 / 1024, 2) AS free_mb,
                   ROUND(data_free / (data_length + data_free) * 100, 1) AS fragment_pct
            FROM information_schema.TABLES
            WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              AND table_type = 'BASE TABLE'
              AND data_length > 0
              AND data_free / (data_length + data_free) > 0.3
            ORDER BY data_free DESC
            LIMIT 20
        """, warnings, "10.3 高碎片率表"))
        rows += _tag("10.4 未使用的索引", _safe_fetch(conn, """
            SELECT s.object_schema, s.object_name, s.index_name,
                   ROUND(COALESCE((
                     SELECT SUM(stat_value) * @@innodb_page_size / 1024 / 1024
                     FROM mysql.innodb_index_stats
                     WHERE database_name = s.object_schema
                       AND table_name = s.object_name
                       AND index_name = s.index_name
                       AND stat_name = 'size'), 0), 2) AS size_mb
            FROM performance_schema.table_io_waits_summary_by_index_usage s
            WHERE s.index_name IS NOT NULL
              AND s.index_name != 'PRIMARY'
              AND s.object_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              AND s.count_star = 0
            ORDER BY s.object_schema, s.object_name
            LIMIT 30
        """, warnings, "10.4 未使用的索引"))
        rows += _tag("10.5 冗余索引", _safe_fetch(conn, """
            SELECT a.table_schema, a.table_name,
                   a.index_name AS redundant_index,
                   a.column_name AS redundant_columns,
                   b.index_name AS covering_index,
                   b.column_name AS covering_columns
            FROM (
              SELECT table_schema, table_name, index_name,
                     GROUP_CONCAT(column_name ORDER BY seq_in_index) AS column_name
              FROM information_schema.STATISTICS
              WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              GROUP BY table_schema, table_name, index_name
            ) a
            JOIN (
              SELECT table_schema, table_name, index_name,
                     GROUP_CONCAT(column_name ORDER BY seq_in_index) AS column_name
              FROM information_schema.STATISTICS
              WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','sys')
              GROUP BY table_schema, table_name, index_name
            ) b
              ON a.table_schema = b.table_schema
             AND a.table_name = b.table_name
             AND a.index_name != b.index_name
             AND LOCATE(a.column_name, b.column_name) = 1
             AND a.column_name != b.column_name
            LIMIT 30
        """, warnings, "10.5 冗余索引"))
        rows += _tag("10.6 表级 IO 热点 Top 10", _safe_fetch(conn, """
            SELECT object_schema, object_name, count_read, count_write, count_fetch,
                   count_insert, count_update, count_delete,
                   ROUND(sum_timer_wait / 1000000000000, 2) AS total_wait_seconds
            FROM performance_schema.table_io_waits_summary_by_table
            WHERE object_schema NOT IN ('mysql','information_schema','performance_schema','sys')
            ORDER BY sum_timer_wait DESC
            LIMIT 10
        """, warnings, "10.6 表级 IO 热点"))
        if no_pk and "error" not in no_pk[0]:
            warnings.append(f"发现 {len(no_pk)} 个无主键表候选，需要核查复制和更新热点风险。")
        return _section(10, "table_analysis", "表与索引分析", items=rows, summary={"subsections": 6}, warnings=warnings)
    return _safe_section(10, "table_analysis", "表与索引分析", collect, "information_schema / performance_schema table analysis")


def _mysql_wait_events(conn) -> dict:
    sql = """
        SELECT event_name, count_star,
               ROUND(sum_timer_wait / 1000000000000, 4) AS total_wait_seconds,
               ROUND(avg_timer_wait / 1000000000, 4) AS avg_wait_ms
        FROM performance_schema.events_waits_summary_global_by_event_name
        WHERE count_star > 0
          AND event_name NOT LIKE 'idle%'
        ORDER BY sum_timer_wait DESC
        LIMIT 20
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("11.1 等待事件 Top 20", _safe_fetch(conn, sql, warnings, "11.1 等待事件"))
        return _section(11, "wait_events", "等待事件 (Top 20)", items=rows, summary={"row_count": len(rows)}, warnings=warnings)
    return _safe_section(11, "wait_events", "等待事件 (Top 20)", collect, sql)


def _mysql_ssl_status(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _mysql_var_rows(conn, ["have_ssl", "have_openssl", "ssl_ca", "ssl_cert", "ssl_key", "require_secure_transport", "tls_version"], warnings, "12.1 SSL 配置")
        rows += _tag("12.2 当前连接 SSL 使用情况", _safe_fetch(conn, "SHOW STATUS WHERE Variable_name IN ('Ssl_version','Ssl_cipher')", warnings, "12.2 当前连接 SSL 使用情况"))
        values = _mysql_row_map(rows)
        if str(values.get("have_ssl", "")).upper() not in {"YES", "ON", "1"}:
            warnings.append("have_ssl 未启用或不可用。")
        if str(values.get("require_secure_transport", "")).upper() not in {"ON", "1"}:
            warnings.append("require_secure_transport 未强制开启，客户端可能明文连接。")
        return _section(12, "ssl_status", "SSL/TLS 安全状态", items=rows, summary={"ssl_enabled": values.get("have_ssl")}, warnings=warnings)
    return _safe_section(12, "ssl_status", "SSL/TLS 安全状态", collect, "SHOW VARIABLES/STATUS ssl")


def _mysql_password_policy(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("13.1 密码策略 (validate_password)", _safe_fetch(conn, """
            SELECT variable_name, variable_value
            FROM performance_schema.global_variables
            WHERE variable_name LIKE 'validate_password%'
            ORDER BY variable_name
        """, warnings, "13.1 密码策略"))
        old_auth = _safe_fetch(conn, """
            SELECT user, host, plugin
            FROM mysql.user
            WHERE plugin IN ('mysql_old_password', 'mysql_native_password')
              AND user NOT IN ('mysql.infoschema','mysql.session','mysql.sys')
            ORDER BY user
        """, warnings, "13.2 使用旧认证插件的用户")
        rows += _tag("13.2 使用旧认证插件的用户", old_auth)
        no_password = _safe_fetch(conn, """
            SELECT user, host, plugin
            FROM mysql.user
            WHERE authentication_string = '' OR authentication_string IS NULL
            ORDER BY user
        """, warnings, "13.3 无密码用户")
        rows += _tag("13.3 无密码用户", no_password)
        if old_auth and "error" not in old_auth[0]:
            warnings.append(f"发现 {len(old_auth)} 个旧认证插件账号。")
        if no_password and "error" not in no_password[0]:
            warnings.append(f"发现 {len(no_password)} 个无密码账号。")
        return _section(13, "password_policy", "密码策略与认证安全", items=rows, summary={"old_auth_count": len(old_auth), "no_password_count": len(no_password)}, warnings=warnings)
    return _safe_section(13, "password_policy", "密码策略与认证安全", collect, "mysql.user / validate_password")


def _mysql_autoinc_check(conn) -> dict:
    sql = """
        SELECT
          t.TABLE_SCHEMA AS table_schema,
          t.TABLE_NAME AS table_name,
          c.COLUMN_TYPE AS column_type,
          t.AUTO_INCREMENT AS current_value,
          CASE
            WHEN c.COLUMN_TYPE LIKE 'tinyint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 255, 127)
            WHEN c.COLUMN_TYPE LIKE 'smallint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 65535, 32767)
            WHEN c.COLUMN_TYPE LIKE 'mediumint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 16777215, 8388607)
            WHEN c.COLUMN_TYPE LIKE 'int%' AND c.COLUMN_TYPE NOT LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 4294967295, 2147483647)
            WHEN c.COLUMN_TYPE LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 18446744073709551615, 9223372036854775807)
            ELSE 0
          END AS max_value,
          ROUND(
            t.AUTO_INCREMENT /
            CASE
              WHEN c.COLUMN_TYPE LIKE 'tinyint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 255, 127)
              WHEN c.COLUMN_TYPE LIKE 'smallint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 65535, 32767)
              WHEN c.COLUMN_TYPE LIKE 'mediumint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 16777215, 8388607)
              WHEN c.COLUMN_TYPE LIKE 'int%' AND c.COLUMN_TYPE NOT LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 4294967295, 2147483647)
              WHEN c.COLUMN_TYPE LIKE 'bigint%' THEN IF(c.COLUMN_TYPE LIKE '%unsigned%', 18446744073709551615, 9223372036854775807)
              ELSE 1
            END * 100, 2) AS used_pct
        FROM information_schema.TABLES t
        JOIN information_schema.COLUMNS c
          ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
         AND t.TABLE_NAME = c.TABLE_NAME
         AND c.EXTRA LIKE '%auto_increment%'
        WHERE t.TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')
          AND t.AUTO_INCREMENT IS NOT NULL
        HAVING used_pct > 30
        ORDER BY used_pct DESC
        LIMIT 30
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("14.1 auto_increment 使用率", _safe_fetch(conn, sql, warnings, "14.1 auto_increment 使用率"))
        high = [row for row in _real_rows(rows) if _num(row.get("used_pct")) >= 80]
        if high:
            warnings.append(f"发现 {len(high)} 个 auto_increment 使用率超过 80% 的列。")
        return _section(14, "autoinc_check", "auto_increment 使用率", items=rows, metric_value=len(high), threshold_value="0", warnings=warnings)
    return _safe_section(14, "autoinc_check", "auto_increment 使用率", collect, sql)


def _mysql_long_queries(conn) -> dict:
    is8 = _mysql_version_major(conn) >= 8
    sqls = [
        """
        SELECT processlist_id AS id, processlist_user AS user, processlist_db AS db,
               processlist_command AS command, processlist_time AS time_seconds,
               processlist_state AS state, LEFT(processlist_info, 300) AS sql_text
        FROM performance_schema.threads
        WHERE processlist_command NOT IN ('Sleep','Daemon','Binlog Dump')
          AND processlist_time > 30
          AND processlist_info IS NOT NULL
          AND type = 'FOREGROUND'
        ORDER BY processlist_time DESC
        LIMIT 20
        """,
        """
        SELECT id, user, db, command, time AS time_seconds, state, LEFT(info, 300) AS sql_text
        FROM information_schema.PROCESSLIST
        WHERE command NOT IN ('Sleep','Daemon','Binlog Dump')
          AND time > 30
          AND info IS NOT NULL
        ORDER BY time DESC
        LIMIT 20
        """,
    ] if is8 else [
        """
        SELECT id, user, db, command, time AS time_seconds, state, LEFT(info, 300) AS sql_text
        FROM information_schema.PROCESSLIST
        WHERE command NOT IN ('Sleep','Daemon','Binlog Dump')
          AND time > 30
          AND info IS NOT NULL
        ORDER BY time DESC
        LIMIT 20
        """
    ]
    def collect():
        warnings: list[str] = []
        rows = _tag("15.1 长查询 (>30s)", _safe_fetch_any(conn, sqls, warnings, "15.1 长查询"))
        real_rows = _real_rows(rows)
        if real_rows:
            warnings.append(f"发现 {len(real_rows)} 条运行超过 30 秒的查询。")
        return _section(15, "long_queries", "长查询", items=rows, metric_value=len(real_rows), threshold_value="0", warnings=warnings)
    return _safe_section(15, "long_queries", "长查询", collect, sqls)


def _mysql_replication_deep(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("16.1 主库 Binlog 状态", _safe_fetch_any(conn, ["SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"], warnings, "16.1 主库 Binlog 状态"))
        rows += _tag("16.2 已连接从库", _safe_fetch_any(conn, ["SHOW REPLICAS", "SHOW SLAVE HOSTS"], warnings, "16.2 已连接从库"))
        rows += _tag("16.3 从库复制状态", _safe_fetch_any(conn, ["SHOW REPLICA STATUS", "SHOW SLAVE STATUS"], warnings, "16.3 从库复制状态"))
        rows += _tag("16.4 半同步复制参数", _safe_fetch(conn, """
            SELECT variable_name, variable_value
            FROM performance_schema.global_variables
            WHERE variable_name LIKE 'rpl_semi_sync_source%'
               OR variable_name LIKE 'rpl_semi_sync_master%'
               OR variable_name LIKE 'rpl_semi_sync_replica%'
               OR variable_name LIKE 'rpl_semi_sync_slave%'
            ORDER BY variable_name
        """, warnings, "16.4 半同步复制"))
        rows += _tag("16.4 半同步复制状态", _safe_fetch(conn, """
            SELECT variable_name, variable_value
            FROM performance_schema.global_status
            WHERE variable_name LIKE 'Rpl_semi_sync_source%'
               OR variable_name LIKE 'Rpl_semi_sync_master%'
               OR variable_name LIKE 'Rpl_semi_sync_replica%'
               OR variable_name LIKE 'Rpl_semi_sync_slave%'
            ORDER BY variable_name
        """, warnings, "16.4 半同步复制状态"))
        rows += _tag("16.5 Group Replication 成员", _safe_fetch(conn, """
            SELECT member_id, member_host, member_port, member_state, member_role, member_version
            FROM performance_schema.replication_group_members
        """, warnings, "16.5 Group Replication"))
        rows += _tag("16.6 复制过滤器", _safe_fetch(conn, """
            SELECT variable_name, variable_value
            FROM performance_schema.global_variables
            WHERE variable_name IN (
              'replicate_do_db', 'replicate_ignore_db', 'replicate_do_table', 'replicate_ignore_table',
              'replicate_wild_do_table', 'replicate_wild_ignore_table', 'binlog_do_db', 'binlog_ignore_db'
            )
            AND variable_value != ''
            ORDER BY variable_name
        """, warnings, "16.6 复制过滤器"))
        rows += _mysql_var_rows(conn, ["gtid_mode", "enforce_gtid_consistency", "gtid_executed", "gtid_purged"], warnings, "16.7 GTID 信息")
        rows += _tag("16.8 复制通道", _safe_fetch(conn, """
            SELECT c.channel_name,
                   c.service_state AS io_state,
                   a.service_state AS sql_state,
                   TIMESTAMPDIFF(SECOND, c.last_heartbeat_timestamp, NOW()) AS heartbeat_lag_seconds,
                   c.received_transaction_set
            FROM performance_schema.replication_connection_status c
            LEFT JOIN performance_schema.replication_applier_status a ON c.channel_name = a.channel_name
            ORDER BY c.channel_name
        """, warnings, "16.8 复制通道"))
        values = _mysql_row_map(rows)
        if str(values.get("gtid_mode", "")).upper() not in {"ON"}:
            warnings.append("GTID 未开启或无法读取，跨库恢复和复制拓扑治理能力受限。")
        return _section(16, "replication_deep", "复制深度诊断", items=rows, summary={"subsections": 8}, warnings=warnings)
    return _safe_section(16, "replication_deep", "复制深度诊断", collect, "SHOW REPLICA/MASTER + performance_schema replication")


def _mysql_backup_check(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _mysql_var_rows(conn, ["max_binlog_size", "binlog_cache_size", "binlog_row_image"], warnings, "17.1 Binlog 备份补充配置")
        rows += _tag("17.2 Binlog 文件列表", _safe_fetch(conn, "SHOW BINARY LOGS", warnings, "17.2 Binlog 文件列表"))
        rows += _mysql_var_rows(conn, ["innodb_redo_log_capacity", "innodb_log_files_in_group", "innodb_data_file_path"], warnings, "17.3 InnoDB 持久化补充配置")
        rows += _mysql_status_rows(conn, ["Binlog_cache_disk_use", "Binlog_cache_use", "Com_flush", "Innodb_os_log_written"], warnings, "17.4 备份相关状态指标")
        backup_events = _safe_fetch(conn, """
            SELECT event_schema, event_name, status, event_type, interval_value, interval_field, last_executed, starts
            FROM information_schema.EVENTS
            WHERE event_name LIKE '%backup%'
               OR event_name LIKE '%bak%'
               OR event_name LIKE '%dump%'
            ORDER BY last_executed DESC
        """, warnings, "17.5 备份计划任务")
        rows += _tag("17.5 备份计划任务", backup_events)
        verification_note = (
            "当前仅采集到 MySQL 内部 Binlog 与事件调度信息，未取得物理/逻辑备份最后成功时间，"
            "因此不能据此判定备份健康；需绑定备份平台、SSH 备份记录或导入备份元数据。"
        )
        return _section(
            17,
            "backup_check",
            "备份检查",
            items=rows,
            summary={
                "plan_only": True,
                "backup_event_count": len(_real_rows(backup_events)),
                "backup_verification": "unverified",
            },
            status="warning" if warnings else "info",
            finding=verification_note,
            suggestion="绑定实际备份来源并验证最近成功时间、备份完整性与恢复演练结果。",
            warnings=warnings,
        )
    return _safe_section(17, "backup_check", "备份检查", collect, "SHOW BINARY LOGS / backup variables")


def _mysql_system_info(conn) -> dict:
    sql = "SELECT @@hostname AS hostname, @@version_compile_os AS os, @@version_compile_machine AS machine, @@datadir AS datadir, @@tmpdir AS tmpdir"
    def collect():
        warnings = ["系统资源章节中的 CPU、内存、磁盘、IO、网络和进程资源需要通过主机 SSH、Zabbix 或 Prometheus 绑定持续采集；数据库连接本身无法读取远端操作系统命令输出。"]
        summary = _one(conn, sql)
        items = [_kv(k, v, "18.1 主机信息") for k, v in summary.items()]
        for label in [
            "18.2 CPU 信息", "18.2.0 Load Average", "18.2.1 NUMA 信息", "18.3 内存信息", "18.4 磁盘信息",
            "18.5 磁盘 IO 统计", "18.6 mysqld 进程资源", "18.7 虚拟内存统计", "18.8 网络接口统计", "18.9 TCP 连接状态",
        ]:
            items.append({"subsection": label, "采集状态": "需要主机监控源", "建议": "绑定 Zabbix Host / Prometheus Target / SSH 主机资产后补齐。"})
        return _section(18, "system_info", "系统资源", summary=summary, items=items, status="info", metric_value="external_required", warnings=warnings)
    return _safe_section(18, "system_info", "系统资源", collect, sql)


def _mysql_expert_tuning(conn) -> dict:
    names = ["Handler_read_key", "Handler_read_rnd_next", "Handler_read_first", "Handler_read_rnd", "Handler_read_next", "Handler_read_prev"]
    def collect():
        warnings: list[str] = []
        rows = _mysql_status_rows(conn, names, warnings, "19.1 Handler 读取模式统计")
        values = _mysql_row_map(rows)
        key_reads = _num(values.get("handler_read_key"))
        rnd_next = _num(values.get("handler_read_rnd_next"))
        ratio = round(rnd_next / key_reads, 2) if key_reads else None
        if ratio is not None and ratio > 10:
            warnings.append(f"Handler_read_rnd_next / Handler_read_key = {ratio}，全表扫描或索引利用不足风险较高。")
        return _section(19, "expert_tuning", "专家级调优指标", summary={"rnd_next_to_key_ratio": ratio}, items=rows, metric_name="rnd_next_to_key_ratio", metric_value=ratio, threshold_value="<10", warnings=warnings)
    return _safe_section(19, "expert_tuning", "专家级调优指标", collect, "SHOW GLOBAL STATUS handler")


def _pg_sections(conn, allowed_item_keys: set[str] | None = None) -> list[dict]:
    collectors = {
        "basic_info": _pg_basic_info,
        "connection": _pg_connection_info,
        "database_info": _pg_database_info,
        "table_stats": _pg_table_stats,
        "index_usage": _pg_index_usage,
        "performance": _pg_performance,
        "replication": _pg_replication,
        "lock_info": _pg_lock_info,
        "long_queries": _pg_long_queries,
        "wait_events": _pg_wait_events,
        "security": _pg_security,
        "settings": _pg_settings,
        "system_info": _pg_system_info,
    }
    section_collectors = [(key, collectors[key]) for key in POSTGRESQL_SECTION_KEYS]
    sections = []
    for section_key, collector in section_collectors:
        matches = _matched_section_item_keys(
            "postgresql",
            section_key,
            POSTGRESQL_SECTION_ALIASES,
            allowed_item_keys,
        )
        if allowed_item_keys is not None and not matches:
            continue
        section = collector(conn)
        if matches:
            section["_template_item_keys"] = matches
        sections.append(section)
    return [json_safe(section) for section in sections]


def _pg_basic_info(conn) -> dict:
    sql = """
        SELECT version() AS pg_version,
               current_database() AS database_name,
               current_user AS current_user,
               pg_postmaster_start_time() AS startup_time,
               now() - pg_postmaster_start_time() AS uptime,
               current_setting('data_directory', true) AS data_directory,
               current_setting('config_file', true) AS config_file,
               pg_is_in_recovery() AS in_recovery,
               current_setting('cluster_name', true) AS cluster_name,
               current_setting('server_encoding', true) AS server_encoding
    """
    def collect():
        summary = _one(conn, sql)
        items = [_kv(k, v, "1.1 基本信息") for k, v in summary.items()]
        return _section(1, "basic_info", "基本信息", summary=summary, items=items, sql=sql)
    return _safe_section(1, "basic_info", "基本信息", collect, sql)


def _pg_connection_info(conn) -> dict:
    def collect():
        warnings: list[str] = []
        settings = _safe_fetch(conn, "SELECT name, setting FROM pg_settings WHERE name IN ('max_connections','superuser_reserved_connections')", warnings, "2.1 连接概览")
        setting_map = {row.get("name"): _num(row.get("setting")) for row in settings}
        current = _one(conn, "SELECT count(*) AS current_connections FROM pg_stat_activity")
        max_conn = setting_map.get("max_connections", 0)
        current_conn = _num(current.get("current_connections"))
        used_pct = round(current_conn * 100 / max_conn, 2) if max_conn else 0
        if used_pct >= 80:
            warnings.append(f"当前连接使用率 {used_pct}%，超过 80%。")
        rows = _tag("2.1 连接概览", settings + [current | {"used_pct": used_pct}])
        rows += _tag("2.2 连接状态分布", _safe_fetch(conn, """
            SELECT state, count(*) AS count,
                   round(100.0 * count(*) / greatest(sum(count(*)) over(), 1), 1) AS pct
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid()
            GROUP BY state
            ORDER BY count(*) DESC
        """, warnings, "2.2 连接状态分布"))
        rows += _tag("2.3 按用户连接分布", _safe_fetch(conn, """
            SELECT usename, count(*) AS connections,
                   count(*) FILTER (WHERE state = 'active') AS active,
                   count(*) FILTER (WHERE state = 'idle') AS idle
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid()
            GROUP BY usename
            ORDER BY count(*) DESC
            LIMIT 15
        """, warnings, "2.3 按用户连接分布"))
        return _section(2, "connection", "连接信息", summary={"current_connections": current_conn, "max_connections": max_conn, "used_pct": used_pct}, items=rows, metric_name="connection_used_pct", metric_value=used_pct, threshold_value="<80%", warnings=warnings)
    return _safe_section(2, "connection", "连接信息", collect, "pg_stat_activity / pg_settings")


def _pg_database_info(conn) -> dict:
    sql = """
        SELECT d.datname AS database_name,
               pg_size_pretty(pg_database_size(d.datname)) AS size_pretty,
               pg_database_size(d.datname) AS size_bytes,
               d.encoding AS encoding_id,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collate,
               d.datconnlimit AS connection_limit,
               s.numbackends AS current_connections
        FROM pg_database d
        LEFT JOIN pg_stat_database s ON d.datname = s.datname
        WHERE d.datistemplate = false
        ORDER BY pg_database_size(d.datname) DESC
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("3.1 数据库信息", _safe_fetch(conn, sql, warnings, "3.1 数据库信息"))
        return _section(3, "database_info", "数据库信息", items=rows, summary={"database_count": len(rows)}, warnings=warnings)
    return _safe_section(3, "database_info", "数据库信息", collect, sql)


def _pg_table_stats(conn) -> dict:
    sql = """
        SELECT schemaname, relname AS table_name,
               n_live_tup, n_dead_tup,
               CASE WHEN n_live_tup + n_dead_tup > 0
                    THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1)
                    ELSE 0
               END AS dead_tuple_pct,
               last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
        FROM pg_stat_user_tables
        ORDER BY n_dead_tup DESC
        LIMIT 20
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("4.1 表统计 Top 20", _safe_fetch(conn, sql, warnings, "4.1 表统计"))
        stale = [row for row in _real_rows(rows) if not row.get("last_analyze") and not row.get("last_autoanalyze")]
        if stale:
            warnings.append(f"发现 {len(stale)} 个表缺少最近 ANALYZE 时间，需关注统计信息新鲜度。")
        return _section(4, "table_stats", "表统计", items=rows, metric_name="stale_analyze_tables", metric_value=len(stale), threshold_value="0", warnings=warnings)
    return _safe_section(4, "table_stats", "表统计", collect, sql)


def _pg_index_usage(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("5.1 索引扫描率", _safe_fetch(conn, """
            SELECT schemaname, relname AS table_name,
                   idx_scan, seq_scan,
                   CASE WHEN idx_scan + seq_scan > 0
                        THEN round(100.0 * idx_scan / (idx_scan + seq_scan), 1)
                        ELSE 0
                   END AS index_scan_pct
            FROM pg_stat_user_tables
            WHERE seq_scan + idx_scan > 100
            ORDER BY seq_scan DESC
            LIMIT 20
        """, warnings, "5.1 索引扫描率"))
        unused = _safe_fetch(conn, """
            SELECT s.schemaname, s.relname AS table_name, s.indexrelname AS index_name,
                   pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
                   pg_relation_size(s.indexrelid) AS index_bytes,
                   s.idx_scan
            FROM pg_stat_user_indexes s
            JOIN pg_index i ON s.indexrelid = i.indexrelid
            WHERE s.idx_scan = 0
              AND NOT i.indisunique
              AND NOT i.indisprimary
              AND pg_relation_size(s.indexrelid) > 65536
            ORDER BY pg_relation_size(s.indexrelid) DESC
            LIMIT 20
        """, warnings, "5.2 未使用索引")
        rows += _tag("5.2 未使用索引", unused)
        if unused and "error" not in unused[0]:
            warnings.append(f"发现 {len(unused)} 个未使用索引候选。")
        return _section(5, "index_usage", "索引使用分析", items=rows, metric_value=len(unused), threshold_value="0", warnings=warnings)
    return _safe_section(5, "index_usage", "索引使用分析", collect, "pg_stat_user_tables / pg_stat_user_indexes")


def _pg_performance(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("6.1 缓存命中率", _safe_fetch(conn, """
            SELECT datname AS database_name,
                   blks_read, blks_hit,
                   CASE WHEN blks_read + blks_hit > 0
                        THEN round(100.0 * blks_hit / (blks_read + blks_hit), 2)
                        ELSE 0
                   END AS hit_pct
            FROM pg_stat_database
            WHERE datname NOT LIKE 'template%'
            ORDER BY blks_read + blks_hit DESC
        """, warnings, "6.1 缓存命中率"))
        rows += _tag("6.2 BGWriter 统计", _safe_fetch_any(conn, [
            """
            SELECT c.num_timed AS checkpoints_timed,
                   c.num_requested AS checkpoints_req,
                   c.buffers_written AS buffers_checkpoint,
                   b.buffers_clean,
                   io.writes AS buffers_backend,
                   b.buffers_alloc
            FROM pg_stat_checkpointer c
            CROSS JOIN pg_stat_bgwriter b
            LEFT JOIN (
              SELECT sum(writes) AS writes
              FROM pg_stat_io
              WHERE backend_type = 'client backend' AND context = 'normal'
            ) io ON true
            """,
            """
            SELECT checkpoints_timed, checkpoints_req, buffers_checkpoint,
                   buffers_clean, buffers_backend, buffers_alloc
            FROM pg_stat_bgwriter
            """,
        ], warnings, "6.2 BGWriter 统计"))
        rows += _tag("6.3 数据库事务统计", _safe_fetch(conn, """
            SELECT datname AS database_name,
                   xact_commit, xact_rollback, deadlocks,
                   CASE WHEN xact_commit + xact_rollback > 0
                        THEN round(100.0 * xact_rollback / (xact_commit + xact_rollback), 2)
                        ELSE 0
                   END AS rollback_pct
            FROM pg_stat_database
            WHERE datname NOT LIKE 'template%'
              AND xact_commit + xact_rollback > 0
            ORDER BY xact_commit + xact_rollback DESC
        """, warnings, "6.3 数据库事务统计"))
        low_hit = [row for row in _real_rows(rows) if row.get("hit_pct") is not None and _num(row.get("hit_pct")) < 95]
        if low_hit:
            warnings.append(f"发现 {len(low_hit)} 个数据库缓存命中率低于 95%。")
        return _section(6, "performance", "性能指标", items=rows, metric_name="low_cache_hit_count", metric_value=len(low_hit), threshold_value="0", warnings=warnings)
    return _safe_section(6, "performance", "性能指标", collect, "pg_stat_database / pg_stat_bgwriter")


def _pg_replication(conn) -> dict:
    def collect():
        warnings: list[str] = []
        recovery = _one(conn, "SELECT pg_is_in_recovery() AS in_recovery")
        rows = _tag("7.0 复制配置", _safe_fetch(conn, "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level','archive_mode','max_wal_senders') ORDER BY name", warnings, "7.0 复制配置"))
        rows += _tag("7.1 复制插槽", _safe_fetch(conn, """
            SELECT slot_name, plugin, slot_type, active, restart_lsn
            FROM pg_replication_slots
            ORDER BY slot_name
        """, warnings, "7.1 复制插槽"))
        if recovery.get("in_recovery"):
            rows += _tag("7.2 WAL 接收进程（备库）", _safe_fetch(conn, """
                SELECT pid, status, sender_host, sender_port, received_lsn, latest_end_lsn,
                       CASE
                           WHEN pg_last_xact_replay_timestamp() IS NULL THEN NULL
                           ELSE round(extract(epoch FROM (clock_timestamp() - pg_last_xact_replay_timestamp()))::numeric, 3)
                       END AS replay_lag_seconds
                FROM pg_stat_wal_receiver
            """, warnings, "7.2 WAL 接收进程"))
        else:
            rows += _tag("7.2 WAL 发送进程（主库）", _safe_fetch(conn, """
                SELECT pid, usename, client_addr, state, sent_lsn, replay_lsn,
                       CASE WHEN sent_lsn IS NOT NULL AND replay_lsn IS NOT NULL
                            THEN pg_wal_lsn_diff(sent_lsn, replay_lsn)::text
                            ELSE 'N/A'
                       END AS lag_bytes,
                       round(extract(epoch FROM write_lag)::numeric, 3) AS write_lag_seconds,
                       round(extract(epoch FROM flush_lag)::numeric, 3) AS flush_lag_seconds,
                       round(extract(epoch FROM replay_lag)::numeric, 3) AS replay_lag_seconds,
                       sync_state
                FROM pg_stat_replication
                ORDER BY client_addr
            """, warnings, "7.2 WAL 发送进程"))
        return _section(7, "replication", "复制状态", summary=recovery, items=rows, warnings=warnings)
    return _safe_section(7, "replication", "复制状态", collect, "pg_replication_slots / pg_stat_replication")


def _pg_lock_info(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        rows += _tag("8.1 锁概览", _safe_fetch(conn, """
            SELECT mode, count(*) AS count,
                   count(*) FILTER (WHERE granted) AS granted,
                   count(*) FILTER (WHERE NOT granted) AS waiting
            FROM pg_locks
            GROUP BY mode
            ORDER BY count(*) DESC
        """, warnings, "8.1 锁概览"))
        waits = _safe_fetch(conn, """
            SELECT blocked.pid AS blocked_pid,
                   blocked_activity.usename AS blocked_user,
                   blocked.mode AS requested_mode,
                   blocking.pid AS blocking_pid,
                   blocking_activity.usename AS blocking_user,
                   blocking_activity.state AS blocking_state,
                   left(blocking_activity.query, 300) AS blocking_sql
            FROM pg_locks blocked
            JOIN pg_locks blocking ON blocking.locktype = blocked.locktype
              AND blocking.database IS NOT DISTINCT FROM blocked.database
              AND blocking.relation IS NOT DISTINCT FROM blocked.relation
              AND blocking.page IS NOT DISTINCT FROM blocked.page
              AND blocking.tuple IS NOT DISTINCT FROM blocked.tuple
              AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
              AND blocking.pid <> blocked.pid
              AND blocking.granted
            JOIN pg_stat_activity blocked_activity ON blocked.pid = blocked_activity.pid
            JOIN pg_stat_activity blocking_activity ON blocking.pid = blocking_activity.pid
            WHERE NOT blocked.granted
            LIMIT 20
        """, warnings, "8.2 锁等待详情")
        rows += _tag("8.2 锁等待详情", waits)
        real_waits = [row for row in waits if "error" not in row]
        if real_waits:
            warnings.append(f"发现 {len(real_waits)} 条锁等待。")
        return _section(8, "lock_info", "锁信息", items=rows, metric_value=len(real_waits), threshold_value="0", warnings=warnings)
    return _safe_section(8, "lock_info", "锁信息", collect, "pg_locks")


def _pg_long_queries(conn) -> dict:
    sql = """
        SELECT pid, usename, datname, state,
               round(extract(epoch FROM (now() - query_start)))::int AS running_seconds,
               wait_event_type, wait_event, left(query, 300) AS sql_text
        FROM pg_stat_activity
        WHERE state <> 'idle'
          AND pid <> pg_backend_pid()
          AND query_start < now() - interval '30 seconds'
        ORDER BY query_start ASC
        LIMIT 50
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("9.1 长查询 (>30s)", _safe_fetch(conn, sql, warnings, "9.1 长查询"))
        real_rows = _real_rows(rows)
        if real_rows:
            warnings.append(f"发现 {len(real_rows)} 条运行超过 30 秒的查询。")
        return _section(9, "long_queries", "长查询", items=rows, metric_value=len(real_rows), threshold_value="0", warnings=warnings)
    return _safe_section(9, "long_queries", "长查询", collect, sql)


def _pg_wait_events(conn) -> dict:
    sql = """
        SELECT wait_event_type, wait_event, count(*) AS session_count,
               string_agg(DISTINCT state, ', ') AS session_states
        FROM pg_stat_activity
        WHERE wait_event IS NOT NULL
          AND pid <> pg_backend_pid()
        GROUP BY wait_event_type, wait_event
        ORDER BY count(*) DESC
        LIMIT 20
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("10.1 等待事件", _safe_fetch(conn, sql, warnings, "10.1 等待事件"))
        return _section(10, "wait_events", "等待事件", items=rows, summary={"row_count": len(rows)}, warnings=warnings)
    return _safe_section(10, "wait_events", "等待事件", collect, sql)


def _pg_security(conn) -> dict:
    def collect():
        warnings: list[str] = []
        rows = []
        roles = _safe_fetch(conn, """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin,
                   rolreplication, rolconnlimit, rolvaliduntil
            FROM pg_roles
            WHERE rolname NOT LIKE 'pg_%'
            ORDER BY rolsuper DESC, rolname
        """, warnings, "11.1 用户角色")
        rows += _tag("11.1 用户角色", roles)
        rows += _tag("11.2 超级用户列表", _safe_fetch(conn, """
            SELECT rolname, rolcanlogin, rolconnlimit
            FROM pg_roles
            WHERE rolsuper = true
            ORDER BY rolname
        """, warnings, "11.2 超级用户列表"))
        rows += _tag("11.3 已安装扩展", _safe_fetch(conn, """
            SELECT extname, extversion, n.nspname AS schema_name
            FROM pg_extension e
            JOIN pg_namespace n ON e.extnamespace = n.oid
            ORDER BY extname
        """, warnings, "11.3 已安装扩展"))
        superusers = [row for row in roles if row.get("rolsuper")]
        if len(superusers) > 1:
            warnings.append(f"发现 {len(superusers)} 个超级用户角色，请确认最小权限。")
        return _section(11, "security", "安全信息", items=rows, metric_name="superuser_count", metric_value=len(superusers), threshold_value="<=1", warnings=warnings)
    return _safe_section(11, "security", "安全信息", collect, "pg_roles / pg_extension")


def _pg_settings(conn) -> dict:
    sql = """
        SELECT name, setting, unit, source, short_desc
        FROM pg_settings
        WHERE name IN (
          'max_connections', 'shared_buffers', 'effective_cache_size',
          'work_mem', 'maintenance_work_mem', 'wal_level',
          'max_wal_size', 'min_wal_size', 'checkpoint_completion_target',
          'archive_mode', 'archive_command', 'max_wal_senders',
          'hot_standby', 'log_statement', 'log_min_duration_statement',
          'log_destination', 'autovacuum', 'autovacuum_max_workers',
          'random_page_cost', 'effective_io_concurrency',
          'huge_pages', 'listen_addresses', 'max_worker_processes',
          'max_parallel_workers_per_gather', 'max_parallel_workers',
          'shared_preload_libraries', 'password_encryption',
          'ssl', 'track_activities', 'track_counts'
        )
        ORDER BY name
    """
    def collect():
        warnings: list[str] = []
        rows = _tag("12.1 关键参数", _safe_fetch(conn, sql, warnings, "12.1 关键参数"))
        setting_map = {row.get("name"): row.get("setting") for row in rows}
        if str(setting_map.get("autovacuum", "")).lower() not in {"on", "true"}:
            warnings.append("autovacuum 未开启，表膨胀和统计信息过期风险较高。")
        if str(setting_map.get("track_counts", "")).lower() not in {"on", "true"}:
            warnings.append("track_counts 未开启，统计视图和 autovacuum 判断会受限。")
        return _section(12, "settings", "关键参数", items=rows, summary={"parameter_count": len(rows)}, warnings=warnings)
    return _safe_section(12, "settings", "关键参数", collect, sql)


def _pg_system_info(conn) -> dict:
    sql = """
        SELECT inet_server_addr()::text AS server_addr,
               inet_server_port() AS server_port,
               current_setting('data_directory', true) AS data_directory,
               current_setting('log_directory', true) AS log_directory,
               pg_backend_pid() AS backend_pid
    """
    def collect():
        warnings = ["系统信息章节中的 CPU、内存、磁盘、IO、网络和 PostgreSQL 主进程资源需要通过主机 SSH、Zabbix 或 Prometheus 绑定持续采集；数据库连接本身无法读取远端操作系统命令输出。"]
        summary = _one(conn, sql)
        items = [_kv(k, v, "13.1 主机信息") for k, v in summary.items()]
        for label in [
            "13.2 CPU", "13.3 内存", "13.4 磁盘", "13.5 Load Average", "13.6 磁盘 IO 统计",
            "13.7 PostgreSQL 进程资源", "13.8 虚拟内存统计", "13.9 网络接口统计", "13.10 TCP 连接状态",
        ]:
            items.append({"subsection": label, "采集状态": "需要主机监控源", "建议": "绑定 Zabbix Host / Prometheus Target / SSH 主机资产后补齐。"})
        return _section(13, "system_info", "系统信息", summary=summary, items=items, status="info", metric_value="external_required", warnings=warnings)
    return _safe_section(13, "system_info", "系统信息", collect, sql)
