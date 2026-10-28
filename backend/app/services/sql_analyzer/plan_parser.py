import hashlib
import re
from decimal import Decimal
from typing import Any


def _number(value: Any, default: float = 0) -> float:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(_number(value, default))
    except (TypeError, ValueError):
        return default


def _profile_value(profile: Any, key: str, default: Any = None) -> Any:
    if isinstance(profile, dict):
        return profile.get(key, default)
    return getattr(profile, key, default)


def _hash_sql(sql_text: str) -> str:
    return hashlib.md5((sql_text or "unknown-sql").encode("utf-8")).hexdigest()[:12]


def _extract_objects(sql_text: str, involved_objects: str | None = None) -> list[str]:
    objects: list[str] = []
    if involved_objects:
        raw = re.split(r"[,;\s]+", involved_objects)
        objects.extend([item.strip().strip('"') for item in raw if item.strip()])
    pattern = re.compile(r"\b(?:from|join|update|into)\s+([a-zA-Z0-9_.$#\[\]]+)", re.IGNORECASE)
    for match in pattern.finditer(sql_text or ""):
        objects.append(match.group(1).strip().strip('"[]'))
    seen: set[str] = set()
    result: list[str] = []
    for item in objects:
        key = item.upper()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result[:20]


def _issue_types(profile: Any) -> set[str]:
    issues = _profile_value(profile, "issues", []) or []
    if not isinstance(issues, list):
        return set()
    return {str(item.get("type") or item.get("risk_type") or "").lower() for item in issues if isinstance(item, dict)}


def _scan_operation(profile: Any, index: int) -> str:
    issues = _issue_types(profile)
    sql_text = str(_profile_value(profile, "sql_text", "") or "").lower()
    if "full_scan" in issues or "full table scan" in sql_text or (index == 0 and _number(_profile_value(profile, "disk_reads")) > 10000):
        return "TABLE ACCESS FULL"
    if "missing_index" in issues or "where" in sql_text:
        return "INDEX RANGE SCAN"
    return "TABLE ACCESS BY INDEX ROWID"


def _parse_plan_text(plan_text: str) -> dict[str, Any]:
    root = {
        "key": "0",
        "title": "SELECT STATEMENT",
        "operation": "SELECT STATEMENT",
        "object_name": "",
        "cost": None,
        "rows": None,
        "children": [],
    }
    lines = [line.strip() for line in (plan_text or "").splitlines() if line.strip()]
    for idx, line in enumerate(lines[:120], start=1):
        if set(line) <= {"-", "+", "|", " "}:
            continue
        cols = [col.strip() for col in line.strip("|").split("|") if col.strip()]
        text = " ".join(cols) if cols else line
        upper = text.upper()
        if any(token in upper for token in ("SELECT STATEMENT", "TABLE ACCESS", "INDEX", "SEQ SCAN", "SCAN", "JOIN", "SORT", "FILTER")):
            operation = text
            object_name = ""
            if len(cols) >= 3:
                operation = cols[1] if cols[0].isdigit() else cols[0]
                object_name = cols[2] if len(cols) > 2 else ""
            root["children"].append({
                "key": str(idx),
                "title": f"{operation} {object_name}".strip(),
                "operation": operation,
                "object_name": object_name,
                "cost": None,
                "rows": None,
                "children": [],
            })
    return root


def normalize_plan(plan: Any) -> dict[str, Any]:
    if isinstance(plan, dict) and isinstance(plan.get("plan_tree"), dict):
        tree = plan["plan_tree"]
        return {
            "plan_hash": plan.get("plan_hash") or _hash_sql(str(tree)),
            "source": plan.get("source") or "generated",
            "plan_tree": tree,
            "summary": plan.get("summary") or plan.get("plan_summary") or {},
            "plan_text": plan.get("plan_text") or "",
        }
    if isinstance(plan, dict):
        return {
            "plan_hash": plan.get("plan_hash") or _hash_sql(str(plan)),
            "source": plan.get("source") or "generated",
            "plan_tree": plan,
            "summary": plan.get("summary") or {},
            "plan_text": plan.get("plan_text") or "",
        }
    return {
        "plan_hash": _hash_sql(str(plan)),
        "source": "generated",
        "plan_tree": _parse_plan_text(str(plan)),
        "summary": {},
        "plan_text": str(plan),
    }


def build_plan_from_profile(profile: Any, plan_text: str | None = None) -> dict[str, Any]:
    sql_text = str(_profile_value(profile, "sql_text", "") or "")
    plan_hash = str(_profile_value(profile, "plan_hash", "") or "") or _hash_sql(sql_text)
    objects = _extract_objects(sql_text, _profile_value(profile, "involved_objects"))
    db_type = str(_profile_value(profile, "db_type", "") or "").lower()
    if plan_text:
        tree = _parse_plan_text(plan_text)
    else:
        root_children = []
        if len(objects) >= 2:
            root_children.append({
                "key": "1",
                "title": "HASH JOIN",
                "operation": "HASH JOIN",
                "object_name": "",
                "cost": max(_int(_profile_value(profile, "buffer_gets")) // 100, 1),
                "rows": _int(_profile_value(profile, "rows_processed")),
                "children": [],
            })
            parent = root_children[0]["children"]
        else:
            parent = root_children
        for idx, object_name in enumerate(objects or ["<UNKNOWN_TABLE>"], start=1):
            operation = _scan_operation(profile, idx - 1)
            parent.append({
                "key": f"1-{idx}",
                "title": f"{operation} {object_name}",
                "operation": operation,
                "object_name": object_name,
                "cost": max(_int(_profile_value(profile, "disk_reads")) // max(idx, 1), 1),
                "rows": _int(_profile_value(profile, "rows_processed")),
                "children": [],
            })
        tree = {
            "key": "0",
            "title": "SELECT STATEMENT",
            "operation": "SELECT STATEMENT",
            "object_name": "",
            "cost": max(_int(_profile_value(profile, "buffer_gets")) // 100, 1),
            "rows": _int(_profile_value(profile, "rows_processed")),
            "children": root_children,
        }
    summary = {
        "db_type": db_type or "unknown",
        "sql_id": _profile_value(profile, "sql_id"),
        "plan_hash": plan_hash,
        "executions": _int(_profile_value(profile, "executions")),
        "avg_elapsed_ms": _number(_profile_value(profile, "avg_elapsed_ms")),
        "cpu_ms": _number(_profile_value(profile, "cpu_ms")),
        "buffer_gets": _int(_profile_value(profile, "buffer_gets")),
        "disk_reads": _int(_profile_value(profile, "disk_reads")),
        "rows_processed": _int(_profile_value(profile, "rows_processed")),
    }
    return {
        "plan_hash": plan_hash,
        "source": _profile_value(profile, "source") or "profile",
        "plan_tree": tree,
        "summary": summary,
        "plan_text": plan_text or "",
    }
