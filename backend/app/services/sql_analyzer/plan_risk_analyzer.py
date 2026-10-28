from typing import Any


def _walk(node: dict[str, Any], level: int = 0):
    yield node, level
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _walk(child, level + 1)


def _risk(risk_type: str, severity: str, node: dict[str, Any], reason: str, impact: str, suggestion: str) -> dict[str, Any]:
    return {
        "risk_type": risk_type,
        "severity": severity,
        "operation": node.get("operation") or node.get("title"),
        "object_name": node.get("object_name") or "",
        "reason": reason,
        "impact": impact,
        "suggestion": suggestion,
        "evidence": {"cost": node.get("cost"), "rows": node.get("rows")},
    }


def analyze_plan_risks(plan: dict[str, Any], profile: Any | None = None) -> list[dict[str, Any]]:
    tree = plan.get("plan_tree") if isinstance(plan, dict) else plan
    if not isinstance(tree, dict):
        return []
    risks: list[dict[str, Any]] = []
    for node, level in _walk(tree):
        operation = f"{node.get('operation') or ''} {node.get('title') or ''}".upper()
        cost = int(node.get("cost") or 0)
        rows = int(node.get("rows") or 0)
        if any(token in operation for token in ("TABLE ACCESS FULL", "FULL TABLE SCAN", "SEQ SCAN")):
            risks.append(_risk(
                "full_table_scan",
                "high" if cost > 1000 or rows > 100000 else "medium",
                node,
                "执行计划出现全表扫描或顺序扫描。",
                "可能导致大量逻辑读、物理读和响应时间抖动。",
                "检查过滤条件选择性，评估组合索引、分区裁剪或统计信息刷新。",
            ))
        if "INDEX SKIP SCAN" in operation:
            risks.append(_risk(
                "index_skip_scan",
                "medium",
                node,
                "执行计划使用 Index Skip Scan。",
                "索引前导列缺失时可能放大扫描范围。",
                "评估是否需要按谓词顺序创建更匹配的组合索引。",
            ))
        if "CARTESIAN" in operation:
            risks.append(_risk(
                "cartesian_join",
                "critical",
                node,
                "执行计划存在笛卡尔连接。",
                "结果集可能指数级放大，严重拖慢 SQL。",
                "检查关联条件是否缺失，补充正确 join 条件。",
            ))
        if "NESTED LOOPS" in operation and rows > 100000:
            risks.append(_risk(
                "nested_loop_large_rows",
                "medium",
                node,
                "大结果集使用 Nested Loops。",
                "内层访问次数过多，容易造成随机 IO 放大。",
                "评估 Hash Join、索引选择性和驱动表顺序。",
            ))
        if any(token in operation for token in ("SORT", "TEMP", "FILESORT")):
            risks.append(_risk(
                "sort_temp",
                "medium",
                node,
                "执行计划包含排序或临时空间消耗。",
                "可能造成 TEMP、work_mem 或 tempdb 压力。",
                "评估排序字段索引、分页方式和临时空间容量。",
            ))
        if level >= 6:
            risks.append(_risk(
                "deep_plan",
                "low",
                node,
                "执行计划层级较深。",
                "复杂访问路径会增加优化器误判概率。",
                "拆解复杂 SQL，检查视图、子查询和统计信息。",
            ))
    if profile is not None:
        issues = getattr(profile, "issues", None) if not isinstance(profile, dict) else profile.get("issues")
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    risks.append({
                        "risk_type": issue.get("type") or "profile_issue",
                        "severity": issue.get("severity") or "medium",
                        "operation": "",
                        "object_name": "",
                        "reason": issue.get("detail") or issue.get("message") or "SQL 画像采集阶段识别到问题。",
                        "impact": "该问题已进入 SQL 画像风险集合。",
                        "suggestion": issue.get("suggestion") or "结合执行计划和业务访问路径继续确认。",
                        "evidence": issue,
                    })
    return risks
