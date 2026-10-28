from typing import Any


def _db_type(profile: Any) -> str:
    value = profile.get("db_type") if isinstance(profile, dict) else getattr(profile, "db_type", "")
    return str(value or "").lower()


def _first_table(related_objects: dict[str, Any]) -> str:
    tables = related_objects.get("tables") or []
    if tables:
        first = tables[0]
        return str(first.get("name") if isinstance(first, dict) else first)
    return "TARGET_TABLE"


def _fallback_stats_script(db_type: str, table_name: str) -> str:
    if db_type == "oracle":
        return f"BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => USER, tabname => '{table_name.upper()}'); END;\n/"
    if db_type == "mysql":
        return f"ANALYZE TABLE {table_name};"
    if db_type in {"postgresql", "postgres"}:
        return f"ANALYZE VERBOSE {table_name};"
    if db_type in {"sqlserver", "sql_server", "mssql"}:
        return f"UPDATE STATISTICS {table_name} WITH FULLSCAN;"
    return f"-- plan_only: refresh statistics for {table_name}"


def _statistics_advice(profile: Any, risks: list[dict[str, Any]], related_objects: dict[str, Any]) -> dict[str, Any]:
    db_type = _db_type(profile)
    table_name = _first_table(related_objects)
    stats = related_objects.get("statistics") or []
    summary = related_objects.get("statistics_summary") or {}
    needs_update = [item for item in stats if item.get("should_update")]
    unknown = [item for item in stats if item.get("status") == "unknown"]

    if not stats:
        return {
            "advice_type": "statistics",
            "severity": "medium" if risks else "low",
            "title": "检查统计信息采集时间",
            "detail": "未解析到关联表统计信息。建议先确认 SQL 涉及对象，再在低峰期补采统计信息并复核执行计划。",
            "script": _fallback_stats_script(db_type, table_name),
            "status": "plan_only",
        }

    lines = []
    for item in stats[:8]:
        last_time = item.get("last_analyzed") or "未知"
        status = item.get("status_label") or item.get("status") or "未知"
        action = "建议更新" if item.get("should_update") else "暂不更新"
        lines.append(f"{item.get('name')}: 统计时间={last_time}, 状态={status}, 判断={action}")

    if needs_update:
        severity = "high" if risks or unknown else "medium"
        title = "统计信息可能过期，建议更新后复核执行计划"
        detail = (
            f"关联对象 {summary.get('total', len(stats))} 个，"
            f"建议更新 {summary.get('should_update', len(needs_update))} 个。"
            + "；".join(lines)
        )
        script = "\n\n".join(item.get("script") or "" for item in needs_update if item.get("script"))
    else:
        severity = "low"
        title = "统计信息较新，暂不需要更新"
        detail = "关联对象统计信息未发现明显过期。建议继续观察 SQL 基线和执行计划漂移；" + "；".join(lines)
        script = "-- plan_only: statistics are fresh, no gather/analyze action is required now."

    return {
        "advice_type": "statistics",
        "severity": severity,
        "title": title,
        "detail": detail,
        "script": script or _fallback_stats_script(db_type, table_name),
        "status": "plan_only",
    }


def build_sql_advice(profile: Any, risks: list[dict[str, Any]], related_objects: dict[str, Any]) -> list[dict[str, Any]]:
    table_name = _first_table(related_objects)
    advice: list[dict[str, Any]] = []
    risk_types = {risk.get("risk_type") for risk in risks}

    if "full_table_scan" in risk_types:
        advice.append(
            {
                "advice_type": "missing_index",
                "severity": "high",
                "title": "评估缺失索引或索引顺序",
                "detail": "当前计划存在全表扫描，建议结合 WHERE、JOIN、ORDER BY 列确认是否需要组合索引。",
                "script": f"CREATE INDEX IDX_{table_name.replace('.', '_')}_CANDIDATE ON {table_name}(<filter_column>);",
                "status": "plan_only",
            }
        )
        advice.append(
            {
                "advice_type": "partition",
                "severity": "medium",
                "title": "评估分区裁剪",
                "detail": "如果对象是大表或历史表，建议检查时间、组织、工厂等维度是否适合分区治理。",
                "script": f"-- plan_only: review partition pruning for {table_name}",
                "status": "plan_only",
            }
        )

    if "sort_temp" in risk_types:
        advice.append(
            {
                "advice_type": "sort_rewrite",
                "severity": "medium",
                "title": "降低排序和临时空间消耗",
                "detail": "检查 ORDER BY、GROUP BY、DISTINCT 是否可以利用索引，分页 SQL 是否可改为 keyset 模式。",
                "script": "-- plan_only: review ORDER BY/GROUP BY columns and pagination predicate",
                "status": "plan_only",
            }
        )

    if "cartesian_join" in risk_types:
        advice.append(
            {
                "advice_type": "sql_rewrite",
                "severity": "critical",
                "title": "修正关联条件",
                "detail": "执行计划存在笛卡尔连接，优先检查 SQL 是否遗漏 join 条件或过滤条件。",
                "script": "-- plan_only: add missing join predicate before release",
                "status": "plan_only",
            }
        )

    advice.append(_statistics_advice(profile, risks, related_objects))
    advice.append(
        {
            "advice_type": "baseline",
            "severity": "low",
            "title": "纳入 SQL 基线跟踪",
            "detail": "将当前 SQL_ID、Plan Hash、资源消耗和风险项纳入历史基线，用于识别计划漂移。",
            "script": "-- plan_only: register SQL baseline after manual review",
            "status": "plan_only",
        }
    )
    return advice
