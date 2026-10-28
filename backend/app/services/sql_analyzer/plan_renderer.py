from typing import Any


def _render_node(node: dict[str, Any], prefix: str = "") -> list[str]:
    operation = node.get("operation") or node.get("title") or "OPERATION"
    object_name = node.get("object_name") or ""
    cost = node.get("cost")
    rows = node.get("rows")
    suffix = []
    if object_name:
        suffix.append(str(object_name))
    if cost is not None:
        suffix.append(f"cost={cost}")
    if rows is not None:
        suffix.append(f"rows={rows}")
    line = f"{prefix}{operation}"
    if suffix:
        line = f"{line} ({', '.join(suffix)})"
    children = node.get("children") or []
    lines = [line]
    for idx, child in enumerate(children):
        if isinstance(child, dict):
            branch = "└─ " if idx == len(children) - 1 else "├─ "
            child_prefix = prefix + branch
            lines.extend(_render_node(child, child_prefix))
    return lines


def render_plan_tree(plan_tree: dict[str, Any] | None) -> str:
    if not isinstance(plan_tree, dict):
        return ""
    return "\n".join(_render_node(plan_tree))
