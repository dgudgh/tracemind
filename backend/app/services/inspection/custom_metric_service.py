from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

from app.analyzers.rules import Finding
from app.collectors.utils import json_safe


MAX_SQL_BYTES = 20 * 1024
MAX_PREVIEW_ROWS = 20
QUERY_TIMEOUT_MS = 5000
SUPPORTED_DB_TYPES = {"oracle", "mysql", "postgresql", "sqlserver"}
FORBIDDEN_KEYWORDS = {
    "ALTER",
    "BEGIN",
    "CALL",
    "COMMIT",
    "COPY",
    "CREATE",
    "DELETE",
    "DO",
    "DROP",
    "EXEC",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "INTO",
    "LOAD",
    "LOCK",
    "MERGE",
    "REPLACE",
    "REVOKE",
    "ROLLBACK",
    "SAVEPOINT",
    "SET",
    "TRUNCATE",
    "UNLOCK",
    "UPDATE",
    "UPSERT",
    "USE",
}


class CustomMetricError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sql_code_without_literals(sql: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            output.append(" ")
            if quote == "]":
                if char == "]":
                    quote = None
            elif char == quote:
                if next_char == quote:
                    output.append(" ")
                    index += 1
                else:
                    quote = None
            elif char == "\\" and next_char:
                output.append(" ")
                index += 1
            index += 1
            continue
        if char == "-" and next_char == "-":
            raise CustomMetricError("sql_comment_forbidden", "自定义指标 SQL 不允许包含注释")
        if char == "/" and next_char == "*":
            raise CustomMetricError("sql_comment_forbidden", "自定义指标 SQL 不允许包含注释")
        if char == "#":
            raise CustomMetricError("sql_comment_forbidden", "自定义指标 SQL 不允许包含注释")
        if char == ";":
            raise CustomMetricError("multiple_statements", "自定义指标只允许一条查询语句，不能包含分号")
        if char in {"'", '"', "`"}:
            quote = char
            output.append(" ")
        elif char == "[":
            quote = "]"
            output.append(" ")
        else:
            output.append(char)
        index += 1
    if quote:
        raise CustomMetricError("invalid_sql", "自定义指标 SQL 存在未闭合的引号")
    return "".join(output)


def validate_readonly_sql(sql: str) -> str:
    normalized = str(sql or "").strip()
    if not normalized:
        raise CustomMetricError("sql_required", "请填写自定义指标的采集 SQL")
    if len(normalized.encode("utf-8")) > MAX_SQL_BYTES:
        raise CustomMetricError("sql_too_large", "自定义指标 SQL 不能超过 20 KB")

    code = _sql_code_without_literals(normalized)
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*", code.upper())
    if not words or words[0] not in {"SELECT", "WITH"}:
        raise CustomMetricError("readonly_required", "自定义指标只允许只读 SELECT 查询")
    if words[0] == "WITH" and "SELECT" not in words:
        raise CustomMetricError("readonly_required", "WITH 语句最终必须执行 SELECT 查询")
    forbidden = next((word for word in words if word in FORBIDDEN_KEYWORDS), None)
    if forbidden:
        raise CustomMetricError("unsafe_keyword", f"自定义指标 SQL 包含禁止关键字：{forbidden}")
    return normalized


def is_custom_sql_metric(item: Mapping[str, Any] | None) -> bool:
    item = item or {}
    source_type = str(item.get("source_type") or "").strip().lower()
    collect_method = str(item.get("collect_method") or "").strip().lower()
    return source_type == "database_sql" or "自定义 sql" in collect_method or "custom sql" in collect_method


def metric_source_key(item: Mapping[str, Any] | None) -> str:
    item = item or {}
    if is_custom_sql_metric(item):
        return str(item.get("key") or "").strip()
    return str(item.get("source_key") or item.get("key") or "").strip()


def enabled_metric_source_keys(items: list[dict] | None) -> set[str] | None:
    if items is None:
        return None
    return {
        metric_source_key(item)
        for item in items
        if _is_enabled(item.get("enabled", True)) and metric_source_key(item)
    }


def _is_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "disabled", "停用"}
    return bool(value)


def normalize_custom_metric(item: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(item or {})
    if not is_custom_sql_metric(normalized):
        return normalized

    normalized.pop("source_key", None)
    normalized["source_type"] = "database_sql"
    normalized["enabled"] = _is_enabled(normalized.get("enabled", True))
    normalized["db_type"] = str(normalized.get("db_type") or "").strip().lower()
    normalized["sql"] = validate_readonly_sql(normalized.get("sql") or normalized.get("query") or "")
    normalized["result_field"] = str(normalized.get("result_field") or "metric_value").strip()
    normalized["value_type"] = str(normalized.get("value_type") or "number").strip().lower()
    normalized["threshold_direction"] = str(
        normalized.get("threshold_direction") or "higher_is_worse"
    ).strip().lower()

    if not normalized["db_type"]:
        raise CustomMetricError("db_type_required", "自定义 SQL 指标必须选择适用数据库")
    if normalized["db_type"] not in SUPPORTED_DB_TYPES:
        raise CustomMetricError("unsupported_db_type", "自定义 SQL 指标仅支持 Oracle、MySQL、PostgreSQL 和 SQL Server")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", normalized["result_field"]):
        raise CustomMetricError("invalid_result_field", "返回字段只能包含字母、数字、下划线、$ 或 #")
    if normalized["value_type"] not in {"number", "text"}:
        raise CustomMetricError("invalid_value_type", "指标值类型必须是数值或文本")

    if normalized["value_type"] == "number":
        if normalized["threshold_direction"] not in {"higher_is_worse", "lower_is_worse"}:
            raise CustomMetricError("invalid_threshold_direction", "数值指标必须选择升高恶化或降低恶化")
        warning = _decimal(normalized.get("warning_value"), "预警阈值")
        critical = _decimal(normalized.get("critical_value"), "严重阈值")
        if normalized["threshold_direction"] == "higher_is_worse" and critical < warning:
            raise CustomMetricError("invalid_threshold_order", "升高恶化指标的严重阈值不能小于预警阈值")
        if normalized["threshold_direction"] == "lower_is_worse" and critical > warning:
            raise CustomMetricError("invalid_threshold_order", "降低恶化指标的严重阈值不能大于预警阈值")
        normalized["warning_value"] = float(warning)
        normalized["critical_value"] = float(critical)
    else:
        expected = str(normalized.get("expected_value") or "").strip()
        if not expected:
            raise CustomMetricError("expected_value_required", "文本指标必须填写正常期望值")
        normalized["expected_value"] = expected
    return normalized


def normalize_item_configs(items: list[dict] | None) -> list[dict]:
    normalized_items: list[dict] = []
    for item in items or []:
        normalized = normalize_custom_metric(item) if is_custom_sql_metric(item) else dict(item)
        if not is_custom_sql_metric(normalized):
            source_key = metric_source_key(normalized)
            if source_key:
                normalized["source_key"] = source_key
        normalized_items.append(normalized)
    return normalized_items


def validate_metric_db_types(items: list[dict] | None, db_types: list[str] | None) -> None:
    allowed = {str(value).strip().lower() for value in (db_types or []) if str(value).strip()}
    custom_items = [item for item in (items or []) if is_custom_sql_metric(item)]
    if not custom_items:
        return
    if not allowed:
        raise CustomMetricError(
            "policy_db_types_required",
            "包含自定义 SQL 指标时必须选择模板适用数据库",
        )
    for item in custom_items:
        configured = str(item.get("db_type") or "").strip().lower()
        if configured and configured not in allowed:
            name = str(item.get("name") or item.get("key") or "自定义指标")
            raise CustomMetricError(
                "metric_db_type_outside_policy",
                f"自定义指标“{name}”的适用数据库 {configured} 不在模板适用数据库范围内",
            )


def execute_metric(
    instance,
    item: Mapping[str, Any],
    *,
    connection_factory: Callable | None = None,
) -> dict[str, Any]:
    metric = normalize_custom_metric(item)
    instance_db_type = str(instance.db_type or "").lower()
    configured_db_type = metric.get("db_type") or ""
    if instance_db_type not in SUPPORTED_DB_TYPES:
        raise CustomMetricError("unsupported_db_type", f"不支持的数据源类型：{instance_db_type or '-'}")
    if configured_db_type and configured_db_type != instance_db_type:
        raise CustomMetricError("db_type_mismatch", "指标适用数据库与测试实例类型不一致")

    factory = connection_factory or _connection_factory(instance_db_type)
    started_at = time.perf_counter()
    cursor = None
    connection = None
    try:
        with factory(instance) as connection:
            cursor = connection.cursor(as_dict=True) if instance_db_type == "sqlserver" else connection.cursor()
            _configure_readonly(connection, cursor, instance_db_type)
            query_started_at = time.perf_counter()
            cursor.execute(metric["sql"])
            columns = [str(column[0]).lower() for column in (cursor.description or [])]
            rows = cursor.fetchmany(MAX_PREVIEW_ROWS + 1)
            query_duration_ms = (time.perf_counter() - query_started_at) * 1000
            if query_duration_ms >= QUERY_TIMEOUT_MS:
                raise CustomMetricError(
                    "query_timeout",
                    f"自定义指标查询超时（超过 {QUERY_TIMEOUT_MS / 1000:g} 秒）",
                )
            normalized_rows = [_row_dict(row, columns) for row in rows]
            connection.rollback()
    except CustomMetricError:
        raise
    except Exception as exc:
        if _is_query_timeout_error(exc):
            raise CustomMetricError(
                "query_timeout",
                f"自定义指标查询超时（超过 {QUERY_TIMEOUT_MS / 1000:g} 秒）",
            ) from exc
        raise CustomMetricError("query_failed", f"自定义指标查询失败：{exc}") from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass

    if not normalized_rows:
        raise CustomMetricError("empty_result", "自定义指标查询没有返回数据")
    result_field = metric["result_field"].lower()
    if result_field not in normalized_rows[0]:
        available = "、".join(columns) or "无"
        raise CustomMetricError(
            "result_field_missing",
            f"查询结果中找不到返回字段 {metric['result_field']}，当前字段：{available}",
        )

    metric_value = normalized_rows[0][result_field]
    status, threshold_text = _evaluate(metric_value, metric)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 1)
    return {
        "sql": metric["sql"],
        "metric_value": json_safe(metric_value),
        "status": status,
        "threshold": threshold_text,
        "columns": columns,
        "rows": json_safe(normalized_rows[:MAX_PREVIEW_ROWS]),
        "truncated": len(normalized_rows) > MAX_PREVIEW_ROWS,
        "duration_ms": duration_ms,
        "source": {
            "instance_id": instance.id,
            "instance_name": instance.instance_name,
            "db_type": instance_db_type,
            "host": instance.host,
            "port": instance.port,
            "database": getattr(instance, "database_name", None) or "",
            "query_objects": _query_objects(metric["sql"]),
            "result_field": metric["result_field"],
        },
    }


def collect_policy_metrics(
    instance,
    items: list[dict] | None,
    *,
    connection_factory: Callable | None = None,
) -> list[Finding]:
    instance_db_type = str(instance.db_type or "").lower()
    eligible_items = [
        raw_item
        for raw_item in (items or [])
        if is_custom_sql_metric(raw_item)
        and _is_enabled(raw_item.get("enabled", True))
        and str(raw_item.get("db_type") or "").lower() in {"", instance_db_type}
    ]
    if not eligible_items:
        return []

    findings: list[Finding] = []
    completed = 0
    factory = connection_factory or _connection_factory(instance_db_type)
    try:
        with factory(instance) as connection:
            @contextmanager
            def shared_connection_factory(_instance):
                yield connection

            for raw_item in eligible_items:
                findings.append(_collect_metric_finding(instance, raw_item, shared_connection_factory))
                completed += 1
    except Exception as exc:
        for raw_item in eligible_items[completed:]:
            findings.append(_metric_failure_finding(instance, raw_item, exc))
    return findings


def _collect_metric_finding(instance, raw_item: Mapping[str, Any], connection_factory: Callable) -> Finding:
    try:
        result = execute_metric(instance, raw_item, connection_factory=connection_factory)
    except Exception as exc:
        return _metric_failure_finding(instance, raw_item, exc)

    status = result["status"]
    value = result["metric_value"]
    status_text = _status_text(status)
    return Finding(
        check_category=str(raw_item.get("category") or "Custom Metrics"),
        check_item=str(raw_item.get("name") or raw_item.get("key") or "自定义指标"),
        check_status=status,
        metric_name=str(raw_item.get("key") or "custom.metric"),
        metric_value=str(value),
        threshold_value=result["threshold"],
        finding=(
            f"自定义指标采集正常，当前值为 {value}。"
            if status == "normal"
            else f"自定义指标当前值为 {value}，判定为{status_text}。"
        ),
        suggestion=str(raw_item.get("advice") or "核对指标查询结果和业务基线。"),
        raw_data=result,
    )


def _metric_failure_finding(instance, raw_item: Mapping[str, Any], exc: Exception) -> Finding:
    message = str(exc)
    return Finding(
        check_category=str(raw_item.get("category") or "Custom Metrics"),
        check_item=str(raw_item.get("name") or raw_item.get("key") or "自定义指标"),
        check_status="warning",
        metric_name=str(raw_item.get("key") or "custom.metric"),
        metric_value="collection_failed",
        threshold_value="只读查询成功",
        finding=f"自定义指标只读采集失败：{message}",
        suggestion=str(raw_item.get("advice") or "检查 SQL、返回字段、实例类型和监控账号权限。"),
        raw_data={
            "error": message,
            "sql": str(raw_item.get("sql") or ""),
            "source": {
                "instance_id": instance.id,
                "instance_name": instance.instance_name,
                "db_type": instance.db_type,
                "query_objects": _query_objects(str(raw_item.get("sql") or "")),
                "result_field": raw_item.get("result_field") or "metric_value",
            },
        },
    )


def _connection_factory(db_type: str):
    if db_type == "oracle":
        from app.collectors.oracle.oracle_connection import oracle_connection

        return oracle_connection
    if db_type == "mysql":
        from app.collectors.mysql.mysql_connection import mysql_connection

        return mysql_connection
    if db_type == "postgresql":
        from app.collectors.postgresql.postgresql_connection import postgresql_connection

        return postgresql_connection
    if db_type == "sqlserver":
        from app.collectors.sqlserver.sqlserver_connection import sqlserver_connection

        return partial(
            sqlserver_connection,
            query_timeout_seconds=max(1, (QUERY_TIMEOUT_MS + 999) // 1000),
            read_only=True,
        )
    raise CustomMetricError("unsupported_db_type", f"不支持的数据源类型：{db_type}")


def _is_query_timeout_error(exc: Exception) -> bool:
    error_codes = {
        str(value).upper()
        for value in (
            getattr(exc, "pgcode", None),
            getattr(exc, "sqlstate", None),
            exc.args[0] if getattr(exc, "args", ()) else None,
        )
        if value is not None
    }
    if error_codes & {"3024", "57014"}:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "statement timeout",
            "maximum statement execution time",
            "query timeout",
            "call timeout",
            "dpy-4024",
            "ora-01013",
            "db-lib error message 20003",
        )
    )


def _configure_readonly(connection, cursor, db_type: str) -> None:
    if db_type == "oracle":
        if hasattr(connection, "call_timeout"):
            connection.call_timeout = QUERY_TIMEOUT_MS
        cursor.execute("SET TRANSACTION READ ONLY")
        return
    if db_type == "mysql":
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={QUERY_TIMEOUT_MS}")
        cursor.execute("START TRANSACTION READ ONLY")
        return
    if db_type == "postgresql":
        cursor.execute("BEGIN READ ONLY")
        cursor.execute(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_MS}ms'")
        return
    if db_type == "sqlserver":
        cursor.execute(f"SET LOCK_TIMEOUT {QUERY_TIMEOUT_MS}")


def _row_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {str(key).lower(): json_safe(value) for key, value in row.items()}
    return {column: json_safe(value) for column, value in zip(columns, row)}


def _evaluate(value: Any, metric: Mapping[str, Any]) -> tuple[str, str]:
    if metric["value_type"] == "text":
        expected = str(metric["expected_value"])
        status = "normal" if str(value).strip().casefold() == expected.casefold() else _risk(metric.get("severity"))
        return status, f"期望值 = {expected}"

    actual = _decimal(value, "查询结果")
    warning = _decimal(metric["warning_value"], "预警阈值")
    critical = _decimal(metric["critical_value"], "严重阈值")
    direction = metric["threshold_direction"]
    if direction == "higher_is_worse":
        status = "critical" if actual >= critical else "warning" if actual >= warning else "normal"
        return status, f"预警 >= {_number_text(warning)}，严重 >= {_number_text(critical)}"
    status = "critical" if actual <= critical else "warning" if actual <= warning else "normal"
    return status, f"预警 <= {_number_text(warning)}，严重 <= {_number_text(critical)}"


def _decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
        raise CustomMetricError("invalid_number", f"{label}必须是有效数值") from exc


def _number_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _risk(value: Any) -> str:
    normalized = str(value or "warning").lower()
    return normalized if normalized in {"critical", "warning", "info"} else "warning"


def _status_text(value: str) -> str:
    return {
        "critical": "严重",
        "warning": "预警",
        "info": "提示",
        "normal": "正常",
    }.get(value, value)


def _query_objects(sql: str) -> list[str]:
    objects: list[str] = []
    pattern = r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_$#.\[\]`\"]+)"
    for match in re.finditer(pattern, sql, flags=re.IGNORECASE):
        value = match.group(1).strip("[]`\"")
        if value and value not in objects:
            objects.append(value)
    return objects[:20]
