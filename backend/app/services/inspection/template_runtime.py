from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from app.collectors.oracle.oracle_health_collector import ORACLE_QUERY_ITEM_KEYS
from app.services import generic_db_ops, host_overview
from app.services.inspection import (
    custom_metric_service,
    oracle_script_checks,
    script_db_checks,
)


SUPPORTED_DB_TYPES = {"oracle", "mysql", "postgresql", "sqlserver", "host"}
DB_TYPE_LABELS = {
    "oracle": "Oracle",
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "sqlserver": "SQL Server",
    "host": "主机",
}
class TemplateRuntimeError(ValueError):
    def __init__(self, code: str, message: str, summary: dict | None = None):
        super().__init__(message)
        self.code = code
        self.summary = summary or {}


def _is_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {
            "",
            "0",
            "false",
            "off",
            "no",
            "disabled",
            "停用",
        }
    return bool(value)


def _normalized_db_types(values: list[str] | None) -> list[str]:
    return list(
        dict.fromkeys(
            str(value).strip().lower()
            for value in (values or [])
            if str(value).strip()
        )
    )


def _item_identity(item: Mapping[str, Any], index: int) -> dict:
    key = str(item.get("key") or "").strip()
    source_key = custom_metric_service.metric_source_key(item)
    return {
        "index": index,
        "key": key or source_key or f"item_{index + 1}",
        "source_key": source_key,
        "name": str(item.get("name") or key or source_key or f"检查项 {index + 1}"),
    }


@lru_cache(maxsize=16)
def _parsed_oracle_units(path: str, mtime_ns: int, size: int) -> tuple:
    return tuple((section.key, section.item_key) for section in oracle_script_checks.parse_script_sections(path))


def _oracle_units(source_key: str, profile: str = "auto") -> set[str]:
    query_only = source_key.startswith(oracle_script_checks.QUERY_SOURCE_PREFIX)
    script_only = source_key.startswith(oracle_script_checks.SCRIPT_SOURCE_PREFIX)
    if query_only:
        source_key = source_key.removeprefix(oracle_script_checks.QUERY_SOURCE_PREFIX)
    allowed = oracle_script_checks.enabled_oracle_item_keys(
        [{"key": source_key, "source_key": source_key, "enabled": True}]
    ) or set()
    units = set() if script_only else {
        f"query:{query_name}"
        for query_name, keys in ORACLE_QUERY_ITEM_KEYS.items()
        if keys & allowed
    }
    if query_only:
        return units
    if profile != "auto":
        from app.collectors.oracle.oracle_inspection_profile import SCRIPT_DIR, SCRIPT_CATALOG

        path = SCRIPT_DIR / SCRIPT_CATALOG[profile]["script_name"]
        stat = path.stat()
        sections = _parsed_oracle_units(str(path), stat.st_mtime_ns, stat.st_size)
        units.update(f"script:{key}" for key, item_key in sections if item_key in allowed or oracle_script_checks.SCRIPT_SOURCE_PREFIX + key in allowed)
    else:
        if script_only:
            from app.collectors.oracle.oracle_inspection_profile import SCRIPT_DIR, SCRIPT_CATALOG

            for entry in SCRIPT_CATALOG.values():
                path = SCRIPT_DIR / entry["script_name"]
                if path.is_file():
                    stat = path.stat()
                    units.update(f"script:{key}" for key, _ in _parsed_oracle_units(str(path), stat.st_mtime_ns, stat.st_size) if oracle_script_checks.SCRIPT_SOURCE_PREFIX + key in allowed)
            return units
        units.update(
            f"script:{anchor}"
            for anchor, item_key in oracle_script_checks.ORACLE_SCRIPT_ITEM_KEYS.items()
            if item_key in allowed
        )
    return units


def _script_units(
    engine: str,
    source_key: str,
) -> set[str]:
    allowed = {source_key}
    if engine == "mysql":
        return {
            f"section:{section_key}"
            for section_key in script_db_checks.MYSQL_SECTION_KEYS
            if script_db_checks._matched_section_item_keys(
                engine,
                section_key,
                script_db_checks.MYSQL_SECTION_ALIASES,
                allowed,
            )
        }
    if engine == "postgresql":
        return {
            f"section:{section_key}"
            for section_key in script_db_checks.POSTGRESQL_SECTION_KEYS
            if script_db_checks._matched_section_item_keys(
                engine,
                section_key,
                script_db_checks.POSTGRESQL_SECTION_ALIASES,
                allowed,
            )
        }
    if engine == "sqlserver":
        return {
            f"section:{section_key}"
            for section_key in generic_db_ops.SQLSERVER_SECTION_KEYS
            if generic_db_ops._mssql_matched_item_keys(section_key, allowed)
        }
    return set()


def _source_units(db_type: str, source_key: str, profile: str = "auto", host_platform: str = "auto") -> set[str]:
    if not source_key:
        return set()
    if db_type == "oracle":
        return _oracle_units(source_key, profile)
    if db_type in {"mysql", "postgresql", "sqlserver"}:
        return _script_units(db_type, source_key)
    if db_type == "host":
        from .windows_host_checks import WINDOWS_HOST_ITEMS

        if host_platform == "windows":
            if source_key in {item["key"] for item in WINDOWS_HOST_ITEMS if item["supported"]}:
                return {f"host:{source_key}"}
        elif source_key in host_overview.HOST_RUNTIME_ITEM_KEYS:
            return {f"host:{source_key}"}
    return set()


def inspect_template_runtime(
    db_types: list[str] | None,
    item_config: list[dict] | None,
    report_config: dict | None = None,
) -> dict:
    normalized_db_types = _normalized_db_types(db_types)
    from .template_profiles import normalize_profile_config

    profile_errors = []
    profile = "auto"
    host_platform = "auto"
    try:
        config = normalize_profile_config(normalized_db_types, report_config)
        host_platform = config.get("host_platform") or "auto"
        profile = config.get("oracle_profile") or "auto"
        if profile != "auto":
            from app.collectors.oracle.oracle_inspection_profile import SCRIPT_DIR, SCRIPT_CATALOG

            path = SCRIPT_DIR / SCRIPT_CATALOG[profile]["script_name"]
            if not path.is_file():
                raise TemplateRuntimeError("missing_script", f"缺少巡检脚本：{path.name}")
    except (TemplateRuntimeError, OSError) as exc:
        profile_errors.append(str(exc))
        profile = "auto"
    items = list(item_config or [])
    enabled_items = [
        (index, item)
        for index, item in enumerate(items)
        if _is_enabled(item.get("enabled", True))
    ]
    unsupported_db_types = [
        db_type for db_type in normalized_db_types if db_type not in SUPPORTED_DB_TYPES
    ]
    valid_db_types = [
        db_type for db_type in normalized_db_types if db_type in SUPPORTED_DB_TYPES
    ]
    matched_any: set[int] = set()
    by_db_type: dict[str, dict] = {}

    for db_type in valid_db_types:
        matched_indices: set[int] = set()
        execution_units: set[str] = set()
        custom_count = 0
        for index, item in enabled_items:
            if custom_metric_service.is_custom_sql_metric(item):
                metric_db_type = str(item.get("db_type") or "").strip().lower()
                if metric_db_type != db_type:
                    continue
                matched_indices.add(index)
                execution_units.add(f"custom:{index}")
                custom_count += 1
                continue
            source_key = custom_metric_service.metric_source_key(item)
            units = _source_units(db_type, source_key, profile, host_platform)
            if not units:
                continue
            matched_indices.add(index)
            execution_units.update(units)

        matched_any.update(matched_indices)
        matched_items = [
            _item_identity(items[index], index)
            for index in sorted(matched_indices)
        ]
        by_db_type[db_type] = {
            "db_type": db_type,
            "db_type_label": DB_TYPE_LABELS[db_type],
            "valid": bool(execution_units),
            "executable_item_count": len(matched_indices),
            "execution_unit_count": len(execution_units),
            "script_section_count": sum(unit.startswith("script:") for unit in execution_units),
            "mapped_health_query_count": sum(unit.startswith("query:") for unit in execution_units),
            "health_item_count": sum(custom_metric_service.metric_source_key(items[index]).startswith(oracle_script_checks.QUERY_SOURCE_PREFIX) for index in matched_indices),
            "custom_sql_count": custom_count,
            "matched_items": matched_items,
        }

    unmapped_items = [
        _item_identity(item, index)
        for index, item in enabled_items
        if index not in matched_any
    ]
    errors: list[str] = list(profile_errors)
    if not normalized_db_types:
        errors.append("模板必须至少选择一个适用数据库或主机类型")
    if unsupported_db_types:
        errors.append(
            "模板包含不支持的适用类型：" + "、".join(unsupported_db_types)
        )
    if not enabled_items:
        errors.append("模板没有启用的检查项")
    for db_type in valid_db_types:
        if not by_db_type[db_type]["valid"]:
            errors.append(
                f"{DB_TYPE_LABELS[db_type]} 没有可执行检查项"
            )
    if unmapped_items:
        labels = "、".join(
            f"{item['name']}（{item['source_key'] or item['key']}）"
            for item in unmapped_items[:8]
        )
        if len(unmapped_items) > 8:
            labels += f" 等 {len(unmapped_items)} 项"
        errors.append(f"存在未绑定采集器的检查项：{labels}")

    return {
        "valid": not errors,
        "configured_item_count": len(items),
        "enabled_item_count": len(enabled_items),
        "executable_item_count": len(matched_any),
        "execution_unit_count": sum(
            item["execution_unit_count"] for item in by_db_type.values()
        ),
        "db_types": normalized_db_types,
        "by_db_type": by_db_type,
        "unmapped_items": unmapped_items,
        "errors": errors,
    }


def require_template_runtime(
    db_types: list[str] | None,
    item_config: list[dict] | None,
    report_config: dict | None = None,
) -> dict:
    summary = inspect_template_runtime(db_types, item_config, report_config)
    if not summary["valid"]:
        raise TemplateRuntimeError(
            "template_not_executable",
            "模板运行校验失败：" + "；".join(summary["errors"]),
            summary,
        )
    return summary
