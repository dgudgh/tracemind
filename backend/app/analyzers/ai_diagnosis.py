from app.schemas.ai import AIDiagnosisRequest, AIDiagnosisResponse


def metric_rows(metrics: dict):
    for key, value in metrics.items():
        rows = value if isinstance(value, list) and value else [value]
        for index, row in enumerate(rows):
            yield key, index, row


def metric_state(row) -> tuple[str, str]:
    if not isinstance(row, dict) or not row:
        return "unknown", "unknown"
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    status = str(row.get("check_status") or row.get("status") or "").strip().lower()
    states = {str(value or "").strip().lower() for value in (
        row.get("execution_status"), row.get("status"), raw.get("execution_status"),
        raw.get("state"), raw.get("status"), status,
    )}
    if states & {"failed", "error", "failure", "connection_failed", "collection_failed", "timed_out", "cancelled", "interrupted"} or raw.get("error") or row.get("error"):
        return "unknown", "failed"
    if states & {"not_collected", "not_executed", "not_supported", "not_applicable", "skipped", "unknown", "partial"} or raw.get("executed") is False:
        return (status if status in {"warning", "critical"} else "unknown"), "partial"
    execution_states = [row.get("execution_status"), raw.get("execution_status")]
    if any(value and str(value).lower() not in {"success", "completed"} for value in execution_states):
        return "unknown", "unknown"
    if any(value and value != "good" for value in (row.get("data_quality"), raw.get("data_quality"))):
        return (status if status in {"warning", "critical"} else "unknown"), "partial"
    # Match inspection's execution semantics before interpreting severity.
    from types import SimpleNamespace
    from app.services.inspection.result_writer import finding_execution_status

    execution = finding_execution_status(SimpleNamespace(
        raw_data=raw, check_status=status, metric_name=row.get("metric_name"),
        metric_value=row.get("metric_value"),
    ))
    if execution != "success":
        return (status if execution == "partial" and status in {"warning", "critical"} else "unknown"), execution
    risk = {"ok": "normal", "pass": "normal"}.get(status, status)
    if risk not in {"normal", "info", "warning", "critical"}:
        return "unknown", "unknown"
    return risk, "success"


def diagnose(payload: AIDiagnosisRequest) -> AIDiagnosisResponse:
    metrics = payload.metrics or {}
    states = [(key, *metric_state(row)) for key, _, row in metric_rows(metrics)]
    warning_keys = list(dict.fromkeys(key for key, risk, _ in states if risk in {"warning", "critical"}))
    risks = {risk for _, risk, _ in states}
    incomplete = not states or any(state != "success" for _, _, state in states)
    risk_level = ("critical" if "critical" in risks else "warning" if "warning" in risks
                  else "unknown" if incomplete else "normal")
    usable = any(risk != "unknown" for _, risk, _ in states)
    execution_status = ("not_collected" if not states else "partial" if incomplete and usable
                        else "failed" if any(state == "failed" for _, _, state in states) and not usable
                        else "unknown" if incomplete else "success")
    data_quality = "partial" if incomplete and usable else "missing" if incomplete else "good"
    question = payload.user_question or "数据库健康诊断"
    return AIDiagnosisResponse(
        summary=f"{payload.instance} {question}：" + (
            f"输入存在缺失或采集异常，综合风险 {risk_level}，不能据此确认整体健康。" if incomplete
            else f"已基于提供的结构化检查项生成规则诊断，风险 {risk_level}。"
        ),
        risk_level=risk_level,
        execution_status=execution_status,
        data_quality=data_quality,
        limitations=["仅判断所提供检查项；未验证采集覆盖范围、时效或根因因果关系。"],
        root_causes=warning_keys or (["证据不足，不能确认根因"] if incomplete else ["已提供检查项未见明确异常"]),
        evidences=[f"输入指标组：{', '.join(metrics.keys()) or '无'}"],
        business_impact=["可能影响 MES 过站、报工、追溯查询或报表响应时间"] if warning_keys else [],
        suggestions=[
            "优先处理 critical 项，再处理 warning 项。",
            "所有变更先在维护窗口执行，并保留回滚方案。",
        ],
        sql_optimization=["对高耗时 SQL 结合执行计划、索引选择性和扫描行数进一步分析。"],
        operation_steps=["导出巡检报告", "确认业务高峰时段", "制定人工处置步骤"],
        rollback_plan=["保留变更前配置和执行脚本", "必要时恢复统计信息、索引或参数配置"],
    )
