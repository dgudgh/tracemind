"""Build installed Oracle templates from the bundled, version-specific SQL source."""
from copy import deepcopy
from hashlib import sha256
import json

from app.collectors.oracle.oracle_health_collector import ORACLE_QUERY_ITEM_KEYS
from app.collectors.oracle.oracle_inspection_profile import SCRIPT_CATALOG, SCRIPT_DIR
from .oracle_script_checks import (
    QUERY_SOURCE_PREFIX, SCRIPT_SOURCE_PREFIX, parse_script_sections,
)


CATALOG_REVISION = "2026-09-08.2"


def item_digest(items):
    from .custom_metric_service import normalize_item_configs

    content = json.dumps(normalize_item_configs(items), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(content.encode()).hexdigest()


def oracle_native_items(profile, base_items):
    health_keys = set().union(*ORACLE_QUERY_ITEM_KEYS.values())
    items = []
    for item in base_items:
        if item["key"] in health_keys:
            health = deepcopy(item)
            health["source_key"] = QUERY_SOURCE_PREFIX + item["key"]
            items.append(health)
    script_name = SCRIPT_CATALOG[profile]["script_name"]
    path = SCRIPT_DIR / script_name
    if not path.is_file():
        raise ValueError(f"内置巡检脚本缺失：{script_name}")
    sections = parse_script_sections(path)
    if not sections or any(not section.item_key for section in sections):
        raise ValueError(f"内置巡检脚本章节未完整绑定：{script_name}")
    metadata = {item["key"]: item for item in base_items}
    for section in sections:
        base = metadata.get(section.item_key, {})
        source = SCRIPT_SOURCE_PREFIX + section.key
        items.append({
            "key": source, "source_key": source, "name": section.title,
            "category": base.get("category", "basic"), "enabled": True,
            "severity": base.get("severity", "info"),
            "collect_method": "内置脚本 SQL 章节",
            "description": f"{script_name} · {section.title}",
            "advice": base.get("advice", "结合原始 SQL 和查询结果复核。"),
            "sql": section.sql, "script_name": script_name,
            "script_section": section.key, "script_line": section.line_number,
        })
    return items
