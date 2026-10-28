from typing import Any


def _flatten(node: dict[str, Any], path: str = "0") -> list[dict[str, Any]]:
    rows = [{
        "path": path,
        "operation": node.get("operation") or node.get("title") or "",
        "object_name": node.get("object_name") or "",
        "cost": node.get("cost"),
        "rows": node.get("rows"),
    }]
    for idx, child in enumerate(node.get("children") or []):
        if isinstance(child, dict):
            rows.extend(_flatten(child, f"{path}.{idx}"))
    return rows


def compare_plans(plan_a: dict[str, Any], plan_b: dict[str, Any]) -> dict[str, Any]:
    tree_a = plan_a.get("plan_tree") or {}
    tree_b = plan_b.get("plan_tree") or {}
    flat_a = _flatten(tree_a) if isinstance(tree_a, dict) else []
    flat_b = _flatten(tree_b) if isinstance(tree_b, dict) else []
    map_a = {row["path"]: row for row in flat_a}
    map_b = {row["path"]: row for row in flat_b}
    differences: list[dict[str, Any]] = []
    for path in sorted(set(map_a) | set(map_b)):
        a = map_a.get(path)
        b = map_b.get(path)
        if not a:
            differences.append({"type": "added", "path": path, "before": None, "after": b})
        elif not b:
            differences.append({"type": "removed", "path": path, "before": a, "after": None})
        elif (a.get("operation"), a.get("object_name")) != (b.get("operation"), b.get("object_name")):
            differences.append({"type": "changed", "path": path, "before": a, "after": b})
    return {
        "plan_hash_a": plan_a.get("plan_hash"),
        "plan_hash_b": plan_b.get("plan_hash"),
        "is_drift": (plan_a.get("plan_hash") or "") != (plan_b.get("plan_hash") or "") or bool(differences),
        "operation_count_a": len(flat_a),
        "operation_count_b": len(flat_b),
        "differences": differences[:100],
        "summary": "执行计划存在漂移" if differences else "执行计划结构一致",
    }
