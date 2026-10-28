import logging

from sqlalchemy.orm import Session

from app.analyzers.ai_diagnosis import diagnose, metric_state
from app.models.ai_report import AIReport
from app.models.db_instance import DBInstance
from app.schemas.ai import AIDiagnosisRequest
from app.services.ai_config import get_default_ai_config
from app.services.ai_degradation import AI_NOT_CONFIGURED, degradation, from_error_code, manual_for
from app.services.ai_diagnosis_contract import inspection_row_to_dict, metric_evidence, model_diagnosis
from app.services.inspection import dispatch as inspection_dispatch
from app.services.reports import generate_markdown_report

logger = logging.getLogger("app.services.operation_ai")


def inspect_and_ai_analyze(db: Session, instance: DBInstance) -> dict:
    task_id, rows = inspection_dispatch.run_collecting_rows(
        db, instance, trigger_type="operation_ai",
    )

    total = len(rows)
    warning = len([r for r in rows if r.check_status == "warning"])
    critical = len([r for r in rows if r.check_status == "critical"])

    markdown_report = generate_markdown_report(db, task_id, instance.id)

    local_ai_result = _local_diagnose(instance, total, warning, critical, rows)

    ai_config = get_default_ai_config(db)
    ai_configured = ai_config is not None
    ai_model_result = None
    ai_error = None
    ai_inference = None

    if ai_config:
        prompt = _build_prompt(instance, markdown_report, local_ai_result)
        evidence = metric_evidence({"inspection": [inspection_row_to_dict(row) for row in rows]})
        ai_model_result, ai_error, ai_inference = model_diagnosis(ai_config, prompt, evidence, local_ai_result)

    execution_status = local_ai_result["execution_status"]
    status = "success" if execution_status == "success" else "partial" if execution_status == "partial" else "failed"
    if ai_error and status == "success":
        status = "partial"
    ai_state = (from_error_code(ai_error, local_result_available=True) if ai_configured
                else degradation(AI_NOT_CONFIGURED, local_result_available=True))

    final_diagnosis = {
        "status": status,
        "execution_status": execution_status,
        "analysis_status": "failed" if ai_error else "completed" if ai_configured else "not_configured",
        "ai_inference": ai_inference,
        "local_rule_result": local_ai_result,
        "ai_model_result": ai_model_result,
        "ai_error": ai_error,
        "ai_configured": ai_configured,
        "ai_provider": ai_config.provider_name if ai_config else None,
        "model_name": ai_config.model_name if ai_config else None,
    }

    ai_report = AIReport(
        instance_id=instance.id,
        db_type=instance.db_type,
        title=f"一键巡检AI分析 - {instance.instance_name} - {task_id}",
        user_question="请基于本次一键巡检报告，分析数据库风险、根因、业务影响和处理建议。",
        summary=local_ai_result.get("summary"),
        risk_level=local_ai_result.get("risk_level"),
        diagnosis=final_diagnosis,
    )
    db.add(ai_report)
    db.commit()
    db.refresh(ai_report)

    return {
        "status": status,
        "execution_status": execution_status,
        "analysis_status": final_diagnosis["analysis_status"],
        "title": "一键巡检 + AI分析",
        "task_id": task_id,
        "instance": {
            "id": instance.id,
            "instance_name": instance.instance_name,
            "db_type": instance.db_type,
            "host": instance.host,
            "port": instance.port,
            "account_type": instance.account_type,
        },
        "inspection_summary": {"total": total, "warning": warning, "critical": critical},
        "ai_report_id": ai_report.id,
        "ai_configured": ai_configured,
        "ai_provider": ai_config.provider_name if ai_config else None,
        "model_name": ai_config.model_name if ai_config else None,
        "ai_error": ai_error,
        "ai_inference": ai_inference,
        "degradation": ai_state,
        "manual": manual_for(ai_state, "operations.capability_matrix"),
        "local_ai_result": local_ai_result,
        "ai_model_result": ai_model_result,
        "markdown_report": markdown_report,
        "results": [
            {
                "check_category": r.check_category,
                "check_item": r.check_item,
                "check_status": r.check_status,
                "metric_name": r.metric_name,
                "metric_value": r.metric_value,
                "threshold_value": r.threshold_value,
                "finding": r.finding,
                "suggestion": r.suggestion,
            }
            for r in rows
        ],
    }


def _local_diagnose(instance, total, warning, critical, rows):
    mapped = [inspection_row_to_dict(row) for row in rows]
    truth = diagnose(AIDiagnosisRequest(db_type=instance.db_type, instance=instance.instance_name,
                                      metrics={"inspection": mapped})).model_dump()
    if truth["data_quality"] == "missing":
        return truth
    abnormal = [row for row, value in zip(rows, mapped) if metric_state(value)[0] in {"warning", "critical"}]
    critical_items = [r for r in abnormal if r.check_status == "critical"]
    warning_items = [r for r in abnormal if r.check_status == "warning"]

    risk_level = truth["risk_level"]
    warning, critical = len(warning_items), len(critical_items)

    root_causes = []
    evidences = []
    suggestions = []
    business_impact = []

    for item in abnormal:
        root_causes.append(f"{item.check_category} / {item.check_item} 异常：{item.finding}")
        evidences.append(f"{item.check_item} 当前值 {item.metric_value}，阈值 {item.threshold_value}")
        if item.suggestion:
            suggestions.append(item.suggestion)
        cat = str(item.check_category or "")
        if "表空间" in cat:
            business_impact.append("表空间不足可能导致业务写入失败、批处理失败或应用报错。")
        elif "FRA" in cat or "归档" in cat:
            business_impact.append("归档或 FRA 空间不足可能导致数据库挂起，影响业务连续性。")
        elif "锁" in cat:
            business_impact.append("锁等待可能导致 MES 过站、报工、接口调用卡顿。")
        elif "慢 SQL" in cat or "SQL" in cat:
            business_impact.append("慢 SQL 可能导致页面响应慢、连接堆积和接口超时。")

    if not abnormal:
        root_causes = ["本次巡检未发现 warning 或 critical 项。"]
        evidences = [f"巡检总项 {total}，Warning {warning}，Critical {critical}。"]
        suggestions = ["保持当前巡检频率。", "建议在业务高峰期再次执行巡检。"]

    suggestions = list(dict.fromkeys(suggestions))
    business_impact = list(dict.fromkeys(business_impact))

    return {
        **truth,
        "summary": f"{instance.instance_name} 本次巡检共发现 {warning} 个 warning，{critical} 个 critical，综合风险等级为 {risk_level}。"
                   + ("存在缺失或采集异常，不能确认整体健康。" if truth["data_quality"] != "good" else ""),
        "risk_level": risk_level,
        "root_causes": root_causes,
        "evidences": evidences,
        "business_impact": business_impact,
        "suggestions": suggestions,
        "sql_optimization": ["对高耗时 SQL 结合执行计划、索引选择性、扫描行数和绑定变量情况进一步分析。"],
        "operation_steps": [
            "先导出本次巡检报告。",
            "优先处理 critical 项，再处理 warning 项。",
            "确认异常是否与业务发布、批处理、备份等任务重合。",
            "涉及空间、参数、索引的变更，先生成脚本并人工确认。",
            "在维护窗口执行高风险操作。",
        ],
        "rollback_plan": [
            "保留变更前参数、索引、统计信息和执行脚本。",
            "涉及扩容时，确认 ASM/文件系统剩余空间和数据文件路径。",
            "涉及归档清理时，确认备份有效后再执行。",
            "涉及 SQL 优化时，保留原执行计划和原索引定义。",
        ],
    }


def _build_prompt(instance, markdown_report, local_ai_result):
    return f"""你是一个资深数据库运维专家，熟悉 Oracle、MySQL、MES 系统数据库巡检、性能优化和故障诊断。

请基于以下数据库一键巡检报告，输出专业诊断分析。

【数据库实例】
实例名称：{instance.instance_name}
数据库类型：{instance.db_type}
主机：{instance.host}
端口：{instance.port}

【本地规则诊断结果】
{local_ai_result}

【Markdown巡检报告】
{markdown_report}

请按以下结构输出：

1. 总体结论
2. 风险等级
3. 主要问题列表
4. 根因分析
5. 对MES业务的影响
6. 优先处理顺序
7. 具体处理建议
8. 需要人工确认的风险点
9. 回滚方案
10. 后续优化建议

要求：
- 不要编造报告中不存在的数据。
- 如果证据不足，请明确说明需要补充哪些信息。
- 所有建议必须适合生产数据库运维场景。
- 高风险操作只生成建议，不要要求系统自动执行。"""
