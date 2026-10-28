"""Generate evidence-backed unified operations / after-sales reports.

Facts and numbers are assembled deterministically; an optional AI pass may only add
narrative and must cite evidence keys that already exist. AI failure degrades to rules.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.alert_event import DBAlertEvent
from app.models.capacity_policy import DBCapacityAlert
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.services import report_center
from app.services.ai_config import call_ai_provider
from app.models.ai_provider_config import AIProviderConfig

SCHEMA = "report.document/v1"
RESULT_SCHEMA = "report.document_result/v1"
PROMPT_VERSION = "report.document/v1"
REPORT_TYPES = {"unified": "统一运维报告", "after_sales": "售后分析报告"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "normal": 3}
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(mysql|postgresql|oracle)://[^\s]+"),
]


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _safe_text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def _evidence(key: str, kind: str, ref: Any, observed_at: datetime | None = None) -> dict:
    item = {"key": key, "type": kind, "ref": str(ref)}
    if observed_at is not None:
        item["observed_at"] = observed_at.replace(microsecond=0).isoformat() + "Z"
    return item


def collect_evidence(db: Session, instance: DBInstance, *, window_hours: int) -> dict:
    end = _now()
    start = end - timedelta(hours=max(1, min(int(window_hours), 720)))
    evidence: list[dict] = []
    job = (
        db.query(DBInspectionJob)
        .filter(DBInspectionJob.instance_id == instance.id, DBInspectionJob.stage == "completed",
                DBInspectionJob.finished_at >= start, DBInspectionJob.finished_at <= end)
        .order_by(DBInspectionJob.finished_at.desc(), DBInspectionJob.created_at.desc())
        .first()
    )
    results = []
    if job is not None:
        results = (
            db.query(DBCheckResult)
            .filter(DBCheckResult.task_id == job.id, DBCheckResult.instance_id == instance.id)
            .all()
        )
        evidence.append(_evidence(f"inspection:job:{job.id}", "inspection_job", job.id, job.finished_at))
    severity = {"critical": 0, "warning": 0, "info": 0, "normal": 0}
    findings = []
    for row in results:
        level = str(getattr(row, "check_status", "") or "").lower()
        if level not in severity:
            level = "warning" if getattr(row, "finding", None) else "normal"
        severity[level] += 1
        if level in {"critical", "warning"}:
            findings.append({
                "id": row.id,
                "item": _safe_text(row.check_item, 200),
                "severity": level,
                "finding": _safe_text(row.finding),
                "suggestion": _safe_text(row.suggestion),
            })
            evidence.append(_evidence(f"check:{row.id}", "check_result", row.id))
    findings.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 9), item["id"]))
    alerts = (
        db.query(DBAlertEvent)
        .filter(DBAlertEvent.instance_id == instance.id,
                DBAlertEvent.last_seen >= start, DBAlertEvent.last_seen <= end)
        .order_by(DBAlertEvent.last_seen.desc())
        .all()
    )
    alert_counts = {"critical": 0, "warning": 0, "info": 0}
    for row in alerts:
        level = str(row.severity or "warning").lower()
        alert_counts[level if level in alert_counts else "warning"] += 1
        evidence.append(_evidence(f"alert:{row.id}", "alert_event", row.id, row.last_seen))
    capacity = (
        db.query(DBCapacityAlert)
        .filter(DBCapacityAlert.instance_id == instance.id, DBCapacityAlert.status == "open",
                DBCapacityAlert.last_seen >= start, DBCapacityAlert.last_seen <= end)
        .order_by(DBCapacityAlert.last_seen.desc(), DBCapacityAlert.id.desc())
        .limit(50)
        .all()
    )
    for row in capacity:
        evidence.append(_evidence(f"capacity:{row.id}", "capacity_alert", row.id, row.last_seen))
    latest_artifact = (
        db.query(ReportArtifact)
        .filter(ReportArtifact.source_key == f"source:inspection:{job.id}")
        .first()
        if job is not None else None
    )
    if latest_artifact is not None:
        evidence.append(_evidence(f"artifact:{latest_artifact.id}", "report_artifact", latest_artifact.id))
    return {
        "window": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "hours": int(window_hours)},
        "instance": {
            "id": instance.id,
            "name": instance.instance_name,
            "db_type": instance.db_type,
            "version": instance.db_version,
            "host": instance.host,
            "port": instance.port,
            "system_name": instance.system_name,
        },
        "inspection": None if job is None else {
            "job_id": job.id,
            "status": job.status,
            "finished_at": job.finished_at.isoformat() + "Z" if job.finished_at else None,
            "score": None,
            "summary": _safe_text(job.failure_reason),
            "counts": severity,
            "findings": findings[:8],
        },
        "alerts": {"counts": alert_counts, "total": len(alerts),
                   "top": [{"id": row.id, "title": _safe_text(row.title, 200), "severity": row.severity}
                           for row in alerts[:8]]},
        "capacity": {"total": len(capacity),
                     "top": [{"id": row.id, "object": _safe_text(row.object_name, 200),
                              "level": row.level, "message": _safe_text(row.message, 300)}
                             for row in capacity[:8]]},
        "artifact_id": latest_artifact.id if latest_artifact else None,
        "evidence": evidence,
        "start": start,
        "end": end,
    }


def _rule_analysis(evidence: dict, report_type: str) -> dict:
    inspection = evidence.get("inspection") or {}
    counts = inspection.get("counts") or {}
    alert_counts = (evidence.get("alerts") or {}).get("counts") or {}
    capacity_total = (evidence.get("capacity") or {}).get("total") or 0
    critical = int(counts.get("critical") or 0) + int(alert_counts.get("critical") or 0)
    warning = int(counts.get("warning") or 0) + int(alert_counts.get("warning") or 0)
    risk = "critical" if critical else "warning" if warning or capacity_total else "normal"
    findings = inspection.get("findings") or []
    recommendations = []
    for item in findings[:5]:
        text = item.get("suggestion") or f"处理 {item.get('item') or '检查项'}"
        recommendations.append({"text": _safe_text(text, 300), "evidence_keys": [f"check:{item['id']}"]})
    if alert_counts.get("critical"):
        recommendations.append({"text": "优先处置严重告警并确认影响范围。", "evidence_keys": []})
    summary = (
        f"{'售后分析' if report_type == 'after_sales' else '统一运维'}报告：实例 {evidence['instance']['name']}，"
        f"最近巡检 {'有' if inspection else '无'}，严重 {critical}、警告 {warning}、容量告警 {capacity_total}。"
    )
    return {"summary": summary, "risks": [], "recommendations": recommendations, "root_causes": [], "risk": risk}


def _parse_ai(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("not_object")
    for key in ("summary", "risks", "recommendations", "root_causes"):
        if key not in value:
            raise ValueError(f"missing_{key}")
    if not isinstance(value["summary"], str) or not isinstance(value["risks"], list) \
            or not isinstance(value["recommendations"], list) or not isinstance(value["root_causes"], list):
        raise ValueError("bad_types")
    return value


def _validate_ai(value: dict, evidence_keys: set[str], evidence_numbers: set[str]) -> dict:
    def check_numbers(text: str):
        for token in re.findall(r"\d+(?:\.\d+)?", str(text)):
            if token not in evidence_numbers:
                raise ValueError(f"invented_number:{token}")

    def check_list(items, fields):
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("bad_item")
            keys = item.get("evidence_keys") or []
            if not isinstance(keys, list) or any(not isinstance(key, str) or key not in evidence_keys for key in keys):
                raise ValueError("unknown_evidence")
            check_numbers(item.get("text") or "")
            for field in fields:
                if field not in item:
                    raise ValueError(f"missing_{field}")
    check_numbers(value.get("summary") or "")
    check_list(value["risks"], ["severity"])
    check_list(value["recommendations"], [])
    check_list(value["root_causes"], ["confidence"])
    return value


def _ai_analysis(db: Session, evidence: dict, report_type: str) -> tuple[dict | None, dict | None, str | None]:
    configs = (
        db.query(AIProviderConfig)
        .filter(AIProviderConfig.enabled == 1)
        .order_by(AIProviderConfig.is_default.desc(), AIProviderConfig.id)
        .all()
    )
    if not configs:
        return None, None, "no_enabled_provider"
    # Never send hostnames, ports or credentials to an external model. Keep only the
    # customer-readable facts and the evidence keys the model may cite.
    inspection = evidence.get("inspection") or {}
    ai_evidence = {
        "instance": {key: evidence["instance"].get(key) for key in ("name", "db_type", "version")},
        "window": evidence["window"],
        "inspection": None if not inspection else {
            "status": inspection.get("status"),
            "counts": inspection.get("counts"),
            "findings": [{key: item.get(key) for key in ("item", "severity", "finding", "suggestion", "id")}
                         for item in inspection.get("findings") or []],
        },
        "alerts": evidence.get("alerts"),
        "capacity": evidence.get("capacity"),
        "evidence_keys": [item["key"] for item in evidence["evidence"]],
    }
    prompt = (
        f"你是数据库运维报告分析器。报告类型：{report_type}。"
        "只根据给定 evidence 输出 JSON，禁止编造数字；每条风险/建议/根因必须给出 evidence_keys，"
        "且只能使用 evidence 中存在的 key。输出键：summary, risks[{text,severity,evidence_keys}],"
        "recommendations[{text,evidence_keys}], root_causes[{text,confidence,evidence_keys}]。\n"
        + json.dumps(ai_evidence, ensure_ascii=False, default=str)[:60000]
    )
    errors: list[str] = []
    for config in configs:
        for _attempt in range(2):
            try:
                raw = call_ai_provider(config, prompt, json_mode=True)
                numbers = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(ai_evidence, ensure_ascii=False, default=str)))
                value = _validate_ai(_parse_ai(raw), {item["key"] for item in evidence["evidence"]}, numbers)
                return value, {"provider": config.provider_name, "model": config.model_name}, None
            except Exception as exc:
                errors.append(f"{config.provider_name}: {type(exc).__name__}: {exc}")
    return None, None, " | ".join(errors)[:1000]


def _sections(evidence: dict, report_type: str, analysis: dict, source: str, ai_meta: dict | None, ai_error: str | None) -> list[dict]:
    inspection = evidence.get("inspection")
    alerts = evidence.get("alerts") or {}
    capacity = evidence.get("capacity") or {}
    sections = [
        {"key": "summary", "title": "执行摘要", "body": analysis["summary"]},
        {"key": "scope", "title": "范围与时间窗",
         "body": f"实例 {evidence['instance']['name']}（{evidence['instance']['db_type']}），"
                 f"{evidence['window']['start']} 至 {evidence['window']['end']}。"},
    ]
    if inspection:
        counts = inspection.get("counts") or {}
        sections.append({"key": "inspection", "title": "巡检结论",
                         "body": f"任务 {inspection['job_id']}，状态 {inspection['status']}，"
                                 f"评分 {inspection.get('score') if inspection.get('score') is not None else '无'}；"
                                 f"严重 {counts.get('critical', 0)}、警告 {counts.get('warning', 0)}、"
                                 f"提示 {counts.get('info', 0)}、正常 {counts.get('normal', 0)}。",
                         "items": inspection.get("findings") or []})
    else:
        sections.append({"key": "inspection", "title": "巡检结论", "body": "时间窗内没有已完成的巡检任务。"})
    sections.append({"key": "alerts", "title": "告警态势",
                     "body": f"共 {alerts.get('total', 0)} 条；严重 {alerts.get('counts', {}).get('critical', 0)}、"
                             f"警告 {alerts.get('counts', {}).get('warning', 0)}、提示 {alerts.get('counts', {}).get('info', 0)}。",
                     "items": alerts.get("top") or []})
    sections.append({"key": "capacity", "title": "容量风险",
                     "body": f"未关闭容量告警 {capacity.get('total', 0)} 条。",
                     "items": capacity.get("top") or []})
    if analysis.get("risks"):
        sections.append({"key": "risks", "title": "风险",
                         "items": analysis.get("risks") or [],
                         "body": "风险按证据列出；未引用的叙述不作为事实。"})
    if report_type == "after_sales" and analysis.get("root_causes"):
        sections.append({"key": "root_causes", "title": "根因候选",
                         "items": analysis.get("root_causes") or [],
                         "body": "根因候选为分析建议，置信度不是事实结论。"})
    sections.append({"key": "recommendations", "title": "建议与下一步",
                     "items": analysis.get("recommendations") or [],
                     "body": "以下建议由证据驱动；AI 生成的叙述不代表事实来源。"})
    sections.append({"key": "evidence", "title": "证据清单", "items": evidence["evidence"]})
    sections.append({"key": "generation", "title": "生成信息",
                     "body": f"分析来源：{source}；模型：{(ai_meta or {}).get('model') or '未使用'}；"
                             f"提示版本：{PROMPT_VERSION}；AI 失败原因：{ai_error or '无'}。"})
    return sections


def _markdown(title: str, sections: list[dict], evidence: dict) -> str:
    lines = [f"# {title}", "", f"> 生成时间：{_now().isoformat()}Z", ""]
    for section in sections:
        lines += [f"## {section['title']}", "", section.get("body") or ""]
        for item in section.get("items") or []:
            lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
        lines.append("")
    lines += ["---", "事实与数字以证据清单为准；本报告不执行任何数据库变更。"]
    return "\n".join(lines)


def _html(sections: list[dict]) -> str:
    parts = ["<article>"]
    for section in sections:
        parts.append(f"<h2>{section['title']}</h2>")
        parts.append(f"<p>{section.get('body') or ''}</p>")
        items = section.get("items") or []
        if items:
            parts.append("<ul>" + "".join(f"<li><code>{json.dumps(item, ensure_ascii=False)}</code></li>" for item in items) + "</ul>")
    parts.append("</article>")
    return "".join(parts)


def generate(db: Session, instance: DBInstance, *, report_type: str, window_hours: int = 168,
             format: str = "markdown", include_ai: bool = True, execution_id: str, actor: str,
             commit: bool = True) -> dict:
    if report_type not in REPORT_TYPES:
        raise ValueError("invalid_report_type")
    evidence = collect_evidence(db, instance, window_hours=window_hours)
    has_evidence = bool(evidence["inspection"] or evidence["alerts"]["total"] or evidence["capacity"]["total"])
    if not has_evidence:
        return {"schema": RESULT_SCHEMA, "outcome": "partial", "reason_code": "no_evidence",
                "artifact_id": None, "artifact_uid": None, "sha256": None,
                "instance_id": instance.id, "report_type": report_type, "format": format,
                "window_hours": int(window_hours), "include_ai": bool(include_ai),
                "evidence_count": 0, "download_url": None}
    rule_analysis = _rule_analysis(evidence, report_type)
    analysis = rule_analysis
    source, ai_meta, ai_error = "rule_engine", None, None
    if include_ai:
        ai_value, ai_meta, ai_error = _ai_analysis(db, evidence, report_type)
        if ai_value is not None:
            # Risk is derived from evidence, never from model self-assessment.
            analysis = {**rule_analysis, **ai_value, "risk": rule_analysis["risk"]}
            source = "ai"
    sections = _sections(evidence, report_type, analysis, source, ai_meta, ai_error)
    title = f"{REPORT_TYPES[report_type]} · {instance.instance_name}"
    markdown = _markdown(title, sections, evidence)
    payload = {"schema": SCHEMA, "report_type": report_type, "audience": "after_sales" if report_type == "after_sales" else "ops",
               "analysis_source": source, "model": (ai_meta or {}).get("model"), "prompt_version": PROMPT_VERSION,
               "ai_error": ai_error, "window": evidence["window"], "instance": evidence["instance"],
               "sections": sections, "evidence": evidence["evidence"]}
    artifact = report_center.create_document_artifact(
        db, source_key=f"report:document:{execution_id}", title=title,
        report_type=f"report_{report_type}", content_markdown=markdown,
        content_html=_html(sections), content_json=payload, evidence_manifest=evidence["evidence"],
        instance_id=instance.id, instance_name=instance.instance_name, db_type=instance.db_type,
        summary=analysis["summary"], risk_level=analysis.get("risk") or ("warning" if ai_error else "normal"),
        actor=actor, commit=commit,
    )
    return {"schema": RESULT_SCHEMA, "outcome": "success", "artifact_id": artifact.id,
            "artifact_uid": artifact.artifact_uid, "instance_id": instance.id,
            "sha256": artifact.content_sha256, "report_type": report_type, "format": format,
            "window_hours": int(window_hours), "include_ai": bool(include_ai),
            "analysis_source": source, "model": (ai_meta or {}).get("model"),
            "evidence_count": len(evidence["evidence"]), "generated_at": _now().isoformat() + "Z",
            "download_url": f"/api/v1/reports/catalog/{artifact.id}/download"}
