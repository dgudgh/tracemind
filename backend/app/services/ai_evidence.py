from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.analyzers.ai_diagnosis import metric_state
from app.analyzers.mysql import mysql_diagnosis_engine as mysql_diagnosis
from app.itsm.models import ItsmTicket
from app.models.alert_event import DBAlertEvent
from app.models.check_result import DBCheckResult
from app.models.data_governance import DBArchiveJob
from app.models.db_instance import DBInstance
from app.models.index_finding import DBIndexFinding
from app.models.inspection_diagnosis import DBDiagnosisFinding, DBInspectionModuleResult, DBInspectionTask
from app.models.oracle_awr import AwrReport
from app.models.slow_sql import DBSlowSQL
from app.models.sql_profile import DBSqlProfile
from app.models.suggestion import DBSuggestion
from app.models.table_profile import DBTableProfile
from app.services.awr_evidence import MAX_CURRENT_AWR_AGE, awr_window_quality, observation_window
from app.services.suggestion_aggregator import inspection_finding_evidence, inspection_suggestion_evidence


def evidence_state(item: dict) -> tuple[str, str]:
    raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else item.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    if "manual_symptom" in {item.get("source"), raw.get("source")} or any(
        value and value != "supporting" for value in (item.get("polarity"), raw.get("polarity"))
    ):
        return "unknown", "skipped"
    severity = item.get("severity") or item.get("check_status") or "info"
    severity = {"high": "critical", "red": "critical", "medium": "warning", "orange": "warning"}.get(severity, severity)
    return metric_state({
        **item, "check_status": severity, "raw_data": raw,
        "metric_name": item.get("metric_name") or item.get("metric"),
        "metric_value": item.get("metric_value", item.get("value")),
    })


def is_supporting_evidence(item: dict) -> bool:
    return evidence_state(item)[1] == "success" and bool(
        item.get("summary") or item.get("evidence") or item.get("phenomenon")
    )


def _unverified_inspection(item):
    return item.get("execution_status_source") in {"mysql.diagnosis_collection/v1", "inspection.source_finding_module/v1"} and not is_supporting_evidence(item)


def build_evidence_chain(db: Session, instance_id: int, limit: int = 20) -> dict:
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        return {}

    sections = [
        _inspection_evidence(db, instance, limit),
        _awr_evidence(db, instance, limit),
        _slow_sql_evidence(db, instance, limit),
        _profile_evidence(db, instance, limit),
        _alert_evidence(db, instance, limit),
        _ticket_evidence(db, instance, limit),
        _governance_evidence(db, instance, limit),
        _suggestion_evidence(db, instance, limit),
    ]
    sections = [section for section in sections if section["items"]]
    all_items = [item for section in sections for item in section["items"]]
    severity_counts = Counter(item.get("severity") or "info" for item in all_items)

    return {
        "instance": {
            "id": instance.id,
            "instance_name": instance.instance_name,
            "db_type": instance.db_type,
            "host": instance.host,
            "port": instance.port,
            "env_type": instance.env_type,
        },
        "summary": {
            "section_count": len(sections),
            "evidence_count": len(all_items),
            "severity_counts": dict(severity_counts),
            "source_counts": {section["key"]: len(section["items"]) for section in sections},
            "generated_at": datetime.now().isoformat(sep=" "),
        },
        "sections": sections,
        "prompt_context": _prompt_context(instance, sections),
    }


def build_local_conclusion(chain: dict, user_question: str | None = None) -> dict:
    summary = chain.get("summary") or {}
    states = [evidence_state(item) for section in chain.get("sections") or []
              for item in section.get("items") or [] if isinstance(item, dict) and not item.get("context_only")]
    risks = {risk for risk, _ in states}
    incomplete = not states or any(execution != "success" for _, execution in states)
    usable = any(risk != "unknown" for risk, _ in states)
    risk_level = ("critical" if "critical" in risks else "warning" if "warning" in risks
                  else "unknown" if incomplete else "normal")
    execution_status = ("not_collected" if not states else "partial" if incomplete and usable
                        else "failed" if any(execution == "failed" for _, execution in states) and not usable
                        else "unknown" if incomplete else "success")

    evidences = []
    suggestions = []
    for section in chain.get("sections") or []:
        for item in section.get("items", [])[:5]:
            if _unverified_inspection(item):
                continue
            title = item.get("title") or item.get("object_name") or section["title"]
            evidences.append(f"{section['title']} / {title}: {item.get('summary') or '-'}")
            if item.get("suggestion"):
                suggestions.append(str(item["suggestion"]))

    instance = chain.get("instance") or {}
    return {
        "summary": f"{instance.get('instance_name', '实例')} 汇聚 {summary.get('evidence_count', 0)} 条证据，综合风险为 {risk_level}。"
                   + ("存在缺失或采集异常，不能确认整体健康。" if incomplete else ""),
        "risk_level": risk_level,
        "execution_status": execution_status,
        "data_quality": "partial" if incomplete and usable else "missing" if incomplete else "good",
        "limitations": ["仅判断所提供证据；未验证完整覆盖范围、时效或根因因果关系。"],
        "user_question": user_question,
        "evidences": evidences[:30],
        "root_causes": _top_titles(chain.get("sections") or [], ("inspection", "awr", "slow_sql", "profiles")),
        "business_impact": _top_titles(chain.get("sections") or [], ("alert", "ticket", "slow_sql")),
        "suggestions": list(dict.fromkeys(suggestions))[:30],
        "operation_steps": [
            "确认证据链中 high/critical 项是否仍处于活动状态。",
            "优先处理影响业务连续性的告警、工单和容量风险。",
            "对涉及 SQL、索引、分区、归档的动作生成 plan_only 脚本并人工复核。",
            "在维护窗口执行变更，并把处置结果回写工单或建议状态。",
        ],
        "rollback_plan": [
            "保留变更前 AWR、巡检、执行计划、索引定义和工单记录。",
            "所有 DDL/DML 变更先准备反向脚本或恢复步骤。",
            "变更后重新采集证据链并对比关键指标。",
        ],
    }


def build_ai_prompt(chain: dict, local_conclusion: dict, user_question: str | None = None) -> str:
    return f"""你是资深数据库运维专家，熟悉 Oracle、MySQL、PostgreSQL、SQL Server 和 MES 业务场景。
请只基于以下结构化证据链输出诊断，不能编造证据中不存在的数据。

【用户问题】
{user_question or "请基于多源证据链诊断数据库风险并给出处置建议。"}

【本地规则结论】
{local_conclusion}

【证据链】
{chain.get("prompt_context")}

请按以下结构输出：总体结论、风险等级、关键证据链、根因分析、对 MES 业务影响、优先处置顺序、具体处置建议、需要补充确认的信息、回滚方案。
高风险操作只能给出建议和验证步骤，不要要求系统自动执行。"""


def _top_titles(sections: list[dict], keys: tuple[str, ...]) -> list[str]:
    rows = []
    for section in sections:
        if section["key"] not in keys:
            continue
        rows.extend(item.get("title") or item.get("summary") for item in section.get("items", [])[:4]
                    if is_supporting_evidence(item))
    return [str(item) for item in rows if item][:12]


def _dt(value):
    return value.isoformat(sep=" ") if value else None


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _section(key: str, title: str, items: list[dict]) -> dict:
    return {"key": key, "title": title, "items": items}


def _inspection_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    items = []
    task = (
        db.query(DBInspectionTask)
        .filter(DBInspectionTask.instance_id == instance.id)
        .order_by(DBInspectionTask.id.desc())
        .first()
    )
    if task:
        projected = None
        if task.db_type == "mysql":
            # One module read for both the task and all its findings, with the same
            # public pure projection used by the ordinary diagnosis readback.
            module_rows = db.query(DBInspectionModuleResult).filter(
                DBInspectionModuleResult.instance_id == task.instance_id,
                DBInspectionModuleResult.task_id == task.task_id,
            ).order_by(DBInspectionModuleResult.id).all()
            evidence_view = mysql_diagnosis.project_mysql_diagnosis_evidence(task, module_rows)
            projected = evidence_view["task"]
        complete = task.overall_score is not None and task.risk_level in {"normal", "info", "warning", "critical"}
        score = projected["overall_score"] if projected else task.overall_score
        risk = projected["risk_level"] if projected else task.risk_level or "info"
        items.append({
            "source": "inspection",
            "severity": risk,
            "title": f"巡检任务 {task.task_id}",
            "summary": projected["summary"] if projected and score is None else f"评分 {score}，严重 {task.critical_count}，告警 {task.warning_count}",
            "execution_status": projected["execution_status"] if projected else "success" if complete else "unknown",
            "data_quality": projected["data_quality"] if projected else "good" if complete else "partial" if task.risk_level in {"warning", "critical"} else "missing",
            "execution_status_source": "mysql.diagnosis_collection/v1" if projected else "inspection_task.overall_score/risk_level",
            "raw_data": projected if projected else {"task_id": task.task_id, "overall_score": score,
                                                     "risk_level": task.risk_level, "summary": task.summary},
            "create_time": _dt(task.create_time),
        })
        findings = (
            db.query(DBDiagnosisFinding)
            .filter(DBDiagnosisFinding.instance_id == instance.id, DBDiagnosisFinding.task_id == task.task_id)
            .order_by(DBDiagnosisFinding.id.desc())
            .limit(limit)
            .all()
        )
        finding_proof = inspection_finding_evidence(db, findings) if projected is not None else {}
        for row in findings:
            item = {
                "source": "inspection",
                "severity": row.severity or "warning",
                "title": row.check_item or row.module_key or "巡检发现",
                "summary": row.finding,
                "raw_data": row.raw_data,
                "finding_id": row.id,
                "suggestion": row.suggestions,
                "create_time": _dt(row.create_time),
            }
            if projected is not None:
                proof = finding_proof[row.id]
                item.update(execution_status=proof["execution_status"], data_quality=proof["data_quality"],
                            severity=proof["risk_level"],
                            execution_status_source="mysql.diagnosis_collection/v1")
            items.append(item)
    latest = (
        db.query(DBCheckResult)
        .filter(DBCheckResult.instance_id == instance.id, DBCheckResult.check_status.in_(["critical", "warning"]))
        .order_by(DBCheckResult.id.desc())
        .limit(limit)
        .all()
    )
    for row in latest:
        items.append({
            "source": "inspection",
            "severity": row.check_status,
            "title": row.check_item,
            "summary": row.finding,
            "raw_data": row.raw_data,
            "check_result_id": row.id,
            "metric": row.metric_name,
            "value": row.metric_value,
            "threshold": row.threshold_value,
            "suggestion": row.suggestion,
            "create_time": _dt(row.create_time),
        })
    return _section("inspection", "巡检证据", items[:limit])


def _awr_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    as_of = datetime.now()
    query = db.query(AwrReport).filter(AwrReport.instance_id == instance.id)
    rows = query.filter(
        AwrReport.begin_time.isnot(None),
        AwrReport.begin_time <= AwrReport.end_time,
        AwrReport.end_time >= as_of - MAX_CURRENT_AWR_AGE,
        AwrReport.end_time <= as_of,
        AwrReport.performance_score.isnot(None),
        AwrReport.status.notin_(["failed", "error"]),
    ).order_by(AwrReport.end_time.desc(), AwrReport.id.desc()).limit(limit).all()
    # History shares the original limit, without displacing current observations.
    remaining = limit if limit < 0 else limit - len(rows)
    if remaining:
        rows.extend(query.filter(AwrReport.id.notin_([row.id for row in rows]))
                    .order_by(AwrReport.id.desc()).limit(remaining).all())
    items = []
    for row in rows:
        recs = row.recommendations if isinstance(row.recommendations, dict) else {}
        quality = awr_window_quality(row, as_of)
        measured = row.performance_score is not None and row.status not in {"failed", "error"}
        severity = "warning" if row.performance_score is not None and row.performance_score < 75 else "info"
        items.append({
            "source": "awr",
            "severity": severity if quality == "good" and measured else "unknown",
            "historical_severity": severity,
            "context_only": quality != "good",
            "title": row.file_name or f"AWR {row.begin_snap}-{row.end_snap}",
            "summary": f"性能评分 {row.performance_score}，CPU {row.cpu_score}，IO {row.io_score}",
            "suggestion": recs.get("priority_recommendations") if recs else None,
            "create_time": _dt(row.create_time),
            "raw_data": {"awr_report_id": row.id, "data_quality": quality,
                         "execution_status": "success" if measured and quality == "good" else "not_collected",
                         "observation_window": observation_window(row)},
        })
    return _section("awr", "AWR 证据", items)


def _slow_sql_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    rows = db.query(DBSlowSQL).filter(DBSlowSQL.instance_id == instance.id).order_by(DBSlowSQL.id.desc()).limit(limit).all()
    return _section("slow_sql", "慢 SQL 证据", [{
        "source": "slow_sql",
        "severity": row.risk_level or "warning",
        "title": row.sql_id or f"SQL {row.id}",
        "summary": f"平均耗时 {_num(row.avg_elapsed_ms)}ms，执行 {row.executions or 0} 次",
        "suggestion": row.optimize_suggestion or row.ai_summary,
        "create_time": _dt(row.capture_time or row.create_time),
    } for row in rows])


def _profile_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    items = []
    for row in db.query(DBSqlProfile).filter(DBSqlProfile.instance_id == instance.id).order_by(DBSqlProfile.id.desc()).limit(limit).all():
        if row.issues:
            items.append({
                "source": "sql_profile",
                "severity": _max_issue_severity(row.issues),
                "title": row.sql_id,
                "summary": f"SQL 画像问题: {row.issues}",
                "suggestion": (row.sql_text or "")[:300],
                "create_time": _dt(row.capture_time or row.create_time),
            })
    for row in db.query(DBTableProfile).filter(DBTableProfile.instance_id == instance.id).order_by(DBTableProfile.id.desc()).limit(limit).all():
        if row.issues:
            items.append({
                "source": "table_profile",
                "severity": _max_issue_severity(row.issues),
                "title": ".".join(filter(None, [row.schema_name, row.table_name])),
                "summary": f"表大小 {_num(row.size_mb)}MB，行数 {row.row_count or '-'}，问题 {row.issues}",
                "create_time": _dt(row.capture_time or row.create_time),
            })
    for row in db.query(DBIndexFinding).filter(DBIndexFinding.instance_id == instance.id).order_by(DBIndexFinding.id.desc()).limit(limit).all():
        items.append({
            "source": "index_profile",
            "severity": row.severity,
            "title": ".".join(filter(None, [row.schema_name, row.table_name, row.index_name])),
            "summary": row.detail,
            "suggestion": row.suggestion,
            "script": row.ddl,
            "create_time": _dt(row.capture_time or row.create_time),
        })
    return _section("profiles", "SQL/表/索引画像证据", items[:limit])


def _alert_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    rows = db.query(DBAlertEvent).filter(DBAlertEvent.instance_id == instance.id).order_by(DBAlertEvent.last_seen.desc(), DBAlertEvent.id.desc()).limit(limit).all()
    return _section("alert", "告警证据", [{
        "source": "alert", "severity": row.severity, "title": row.title, "summary": row.message,
        "status": row.status, "create_time": _dt(row.last_seen),
    } for row in rows])


def _ticket_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    rows = db.query(ItsmTicket).filter(ItsmTicket.instance_id == instance.id).order_by(ItsmTicket.id.desc()).limit(limit).all()
    return _section("ticket", "工单证据", [{
        "source": "ticket", "severity": row.severity or row.priority or "info", "title": row.title,
        "summary": row.description, "status": row.mesguard_status, "create_time": _dt(row.create_time),
    } for row in rows])


def _governance_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    rows = db.query(DBArchiveJob).filter(DBArchiveJob.instance_id == instance.id).order_by(DBArchiveJob.id.desc()).limit(limit).all()
    return _section("governance", "数据治理证据", [{
        "source": "governance", "severity": "info", "title": row.job_name,
        "summary": f"{row.job_type}: {row.schema_name}.{row.table_name}，状态 {row.status}",
        "suggestion": row.plan_script, "create_time": _dt(row.create_time),
    } for row in rows])


def _suggestion_evidence(db: Session, instance: DBInstance, limit: int) -> dict:
    rows = db.query(DBSuggestion).filter(DBSuggestion.instance_id == instance.id).order_by(DBSuggestion.id.desc()).limit(limit).all()
    proof = inspection_suggestion_evidence(db, rows)
    items = []
    for row in rows:
        item = {"source": "suggestion", "severity": row.severity, "title": row.problem_type or row.object_name or f"建议 {row.id}",
                "summary": row.impact, "suggestion": row.suggestion, "status": row.status, "create_time": _dt(row.create_time)}
        if row.source == "inspection":
            value = proof[row.id]
            item.update(execution_status=value["execution_status"], data_quality=value["data_quality"],
                        severity=value["risk_level"], evidence_basis=value["evidence_basis"],
                        execution_status_source="inspection.source_finding_module/v1")
        items.append(item)
    return _section("suggestion", "优化建议证据", items)


def _max_issue_severity(issues: Any) -> str:
    if not isinstance(issues, list):
        return "warning"
    values = [str(item.get("severity", "")).lower() for item in issues if isinstance(item, dict)]
    if any(v in ("critical", "high") for v in values):
        return "high"
    if any(v in ("warning", "medium") for v in values):
        return "warning"
    return "info"


def _prompt_context(instance: DBInstance, sections: list[dict]) -> str:
    lines = [f"实例: {instance.instance_name} ({instance.db_type}) {instance.host}:{instance.port}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section.get("items", [])[:20]:
            if _unverified_inspection(item):
                lines.append(f"- 巡检来源证据未确认（{item.get('execution_status', 'unknown')}），不得作为已确认根因或健康依据。")
                continue
            lines.append(f"- [{item.get('severity') or 'info'}] {item.get('title') or '-'} | {item.get('summary') or '-'}")
            if item.get("suggestion"):
                lines.append(f"  建议: {str(item.get('suggestion'))[:800]}")
        lines.append("")
    return "\n".join(lines)
