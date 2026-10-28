import re
from typing import Any


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child)


def _profile_value(profile: Any, key: str, default: Any = None) -> Any:
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _add_unique(items: list[dict[str, str]], name: str, source: str):
    cleaned = (name or "").strip().strip('"[]')
    if not cleaned or cleaned.startswith("<"):
        return
    key = cleaned.upper()
    if not any(item["name"].upper() == key for item in items):
        items.append({"name": cleaned, "source": source})


def map_related_objects(profile: Any, plan: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    tables: list[dict[str, str]] = []
    indexes: list[dict[str, str]] = []
    stats: list[dict[str, str]] = []
    sql_text = str(_profile_value(profile, "sql_text", "") or "")
    involved = str(_profile_value(profile, "involved_objects", "") or "")
    for item in re.split(r"[,;\s]+", involved):
        _add_unique(tables, item, "profile")
    for match in re.finditer(r"\b(?:from|join|update|into)\s+([a-zA-Z0-9_.$#\[\]]+)", sql_text, re.IGNORECASE):
        _add_unique(tables, match.group(1), "sql_text")
    tree = plan.get("plan_tree") or {}
    if isinstance(tree, dict):
        for node in _walk(tree):
            operation = str(node.get("operation") or "").upper()
            object_name = str(node.get("object_name") or "")
            if "INDEX" in operation:
                _add_unique(indexes, object_name, "plan")
            elif object_name:
                _add_unique(tables, object_name, "plan")
    for table in tables:
        stats.append({"name": table["name"], "source": "statistics"})
    return {"tables": tables, "indexes": indexes, "statistics": stats}
