"""统一优化建议聚合服务。

把各性能模块已持久化的产出（AWR 建议 / 慢SQL / 容量告警 / 巡检 findings）
归一化写入 db_suggestion，并据此计算单实例画像与跨实例总览。

设计原则：
- 只读现有源表，不侵入各模块的生成逻辑。
- rebuild 幂等：巡检保留历史行并按源键去重，其他来源替换 open 行；
  已被人工置为 acked/converted/closed 的同主键建议予以保留、不重复插入。
"""
import logging
import re
from collections import defaultdict

from sqlalchemy import func, or_, tuple_
from sqlalchemy.orm import Session

from app.analyzers.ai_diagnosis import metric_state
from app.analyzers.mysql import mysql_diagnosis_engine as mysql_diagnosis
from app.models.db_instance import DBInstance
from app.models.suggestion import DBSuggestion
from app.models.oracle_awr import AwrReport
from app.models.slow_sql import DBSlowSQL
from app.models.capacity_policy import DBCapacityAlert
from app.models.alert_event import DBAlertEvent
from app.models.data_governance import DBArchiveJob
from app.models.inspection_diagnosis import DBDiagnosisFinding, DBInspectionModuleResult, DBInspectionTask
from app.models.check_result import DBCheckResult
from app.models.diagnosis_result import DiagnosisResult
from app.models.ai_report import AIReport
from app.models.sql_profile import DBSqlProfile
from app.models.index_finding import DBIndexFinding
from app.models.table_profile import DBTableProfile
from app.models.external_monitoring import (
    PrometheusAlertCache, ZabbixProblemCache, ZabbixHostMapping,
)

logger = logging.getLogger("app.services.suggestion_aggregator")

SOURCES = ("awr", "slow_sql", "capacity", "inspection", "diagnosis", "alert", "ai", "sql", "index", "table", "governance")

_INDEX_TYPE_LABEL = {
    "unused": "未使用索引", "redundant": "冗余索引", "duplicate": "重复索引",
    "invalid": "失效索引", "low_efficiency": "低效索引", "missing": "缺失索引",
}


def _norm_severity(value) -> str:
    v = str(value or "").strip().lower()
    if v in ("high", "critical", "severe", "严重", "高"):
        return "high"
    if v in ("medium", "warning", "warn", "中", "中危"):
        return "medium"
    if v in ("low", "info", "minor", "低", "提示"):
        return "low"
    return "medium"


def _grade(score) -> str:
    if score is None:
        return "-"
    s = float(score)
    if s >= 90:
        return "A"
    if s >= 80:
        return "B"
    if s >= 70:
        return "C"
    return "D"


def _join_recs(recs) -> str:
    if isinstance(recs, (list, tuple)):
        return "\n".join(f"- {x}" for x in recs if x)
    return str(recs or "")


def _suggestions_text(value) -> str:
    """巡检 finding 的 suggestions 字段可能是 list[str] / list[dict] / str。"""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(item.get("suggestion") or item.get("title") or item.get("text") or str(item))
            else:
                parts.append(str(item))
        return "\n".join(f"- {p}" for p in parts if p)
    if isinstance(value, dict):
        return value.get("suggestion") or value.get("text") or str(value)
    return str(value or "")


def _unknown_inspection_evidence(reason):
    return {"execution_status": "unknown", "data_quality": "missing", "risk_level": "unknown",
            "evidence_basis": reason}


def inspection_finding_evidence(db: Session, findings) -> dict:
    """Batch source proof, keyed by finding id; never trust a materialized suggestion's extra."""
    keys = {(row.instance_id, row.task_id) for row in findings}
    if not keys:
        return {}
    tasks = {(row.instance_id, row.task_id): row for row in db.query(DBInspectionTask).filter(
        tuple_(DBInspectionTask.instance_id, DBInspectionTask.task_id).in_(keys),
    ).all()}
    mysql_keys = [key for key, task in tasks.items() if task.db_type == "mysql"]
    modules = defaultdict(list)
    if mysql_keys:
        for row in db.query(DBInspectionModuleResult).filter(
            tuple_(DBInspectionModuleResult.instance_id, DBInspectionModuleResult.task_id).in_(mysql_keys),
        ).order_by(DBInspectionModuleResult.id).all():
            modules[(row.instance_id, row.task_id)].append(row)
    views = {}
    for key in mysql_keys:
        by_key = {}
        for module in mysql_diagnosis.project_mysql_diagnosis_evidence(tasks[key], modules[key])["modules"]:
            name = module["module_key"]
            by_key[name] = module if name not in by_key else None
        views[key] = by_key
    result = {}
    for finding in findings:
        key = (finding.instance_id, finding.task_id)
        task = tasks.get(key)
        value = _unknown_inspection_evidence("source_task_missing")
        if task is not None and task.db_type == finding.db_type:
            raw = finding.raw_data if isinstance(finding.raw_data, dict) else {}
            risk, state = metric_state({"check_status": {"high": "critical", "medium": "warning"}.get(finding.severity, finding.severity),
                                        "raw_data": raw})
            module = views.get(key, {}).get(finding.module_key)
            if task.db_type == "mysql" and module is None:
                value = _unknown_inspection_evidence("source_module_missing_or_duplicate")
            elif task.db_type == "mysql" and module["execution_status"] not in {"success", "partial"}:
                value = {**_unknown_inspection_evidence("source_module_unverified"), "execution_status": module["execution_status"]}
            elif state == "failed":
                value = {**_unknown_inspection_evidence("source_finding_failed"), "execution_status": "failed"}
            elif (raw.get("executed") is False or raw.get("data_quality") in ("missing", "unknown", "not_collected")
                  or any(raw.get(field) in {"unknown", "not_collected", "not_executed", "not_supported", "not_applicable", "skipped", "plan_only"}
                         for field in ("execution_status", "status", "state") if isinstance(raw.get(field), str))):
                value = _unknown_inspection_evidence("source_finding_unverified")
            else:
                state = "partial" if module and module["execution_status"] == "partial" and state == "success" else state
                value = {"execution_status": state, "data_quality": "good" if state == "success" else "partial" if state == "partial" else "missing",
                         "risk_level": risk, "evidence_basis": "source_finding_module" if module else "source_finding"}
        result[finding.id] = value
    return result


def inspection_suggestion_evidence(db: Session, suggestions) -> dict:
    """Revalidate inspection provenance in bulk for both AI and performance readers.

    Returns evidence keyed by suggestion id. Other sources retain their existing rules.
    Missing, foreign, malformed or deleted sources are unknown even if extra says success.
    """
    rows = [row for row in suggestions if row.source == "inspection"]
    refs = {}
    for row in rows:
        match = re.fullmatch(r"(finding|check):([1-9][0-9]{0,18})", row.source_ref or "")
        refs[row.id] = (match[1], int(match[2])) if match and int(match[2]) <= 2**63 - 1 else None
    finding_ids = {ref[1] for ref in refs.values() if ref and ref[0] == "finding"}
    check_ids = {ref[1] for ref in refs.values() if ref and ref[0] == "check"}
    findings = {row.id: row for row in db.query(DBDiagnosisFinding).filter(DBDiagnosisFinding.id.in_(finding_ids)).all()} if finding_ids else {}
    checks = {row.id: row for row in db.query(DBCheckResult).filter(DBCheckResult.id.in_(check_ids)).all()} if check_ids else {}
    proof = inspection_finding_evidence(db, list(findings.values()))
    result = {}
    for row in rows:
        ref = refs[row.id]
        value = _unknown_inspection_evidence("source_reference_missing")
        source = (findings if ref[0] == "finding" else checks).get(ref[1]) if ref else None
        if source is not None and source.instance_id == row.instance_id and (not row.db_type or row.db_type == source.db_type):
            if ref[0] == "finding":
                value = proof[source.id]
            else:
                risk, state = metric_state({"check_status": source.check_status, "raw_data": source.raw_data,
                                            "metric_name": source.metric_name, "metric_value": source.metric_value})
                value = {"execution_status": state, "data_quality": "good" if state == "success" else "partial" if state == "partial" else "missing",
                         "risk_level": risk, "evidence_basis": "source_check"}
        result[row.id] = value
    return result


def _suggestion_risk_view(row, evidence):
    if row.source != "inspection":
        return {"severity": row.severity}
    proof = evidence.get(row.id, _unknown_inspection_evidence("source_unverified"))
    confirmed_risk = proof["execution_status"] in {"success", "partial"} and proof["risk_level"] in {"critical", "warning", "info", "normal"}
    return {**proof, "severity": ("low" if proof["risk_level"] == "normal" else _norm_severity(proof["risk_level"])) if confirmed_risk else "unknown"}


# ---------------------------------------------------------------------------
# 各来源 → 归一化建议项 (dict)。每项含 dedup 主键 source_ref/problem_type。
# ---------------------------------------------------------------------------

def _collect_awr(db: Session, inst: DBInstance) -> list[dict]:
    """取该实例最新一份已分析 AWR 的 priority_recommendations。"""
    report = (
        db.query(AwrReport)
        .filter(AwrReport.instance_id == inst.id, AwrReport.recommendations.isnot(None))
        .order_by(AwrReport.id.desc())
        .first()
    )
    if not report or not report.recommendations:
        return []
    recs = report.recommendations or {}
    prio = recs.get("priority_recommendations") if isinstance(recs, dict) else None
    if not isinstance(prio, list):
        return []
    items = []
    for idx, p in enumerate(prio):
        if not isinstance(p, dict):
            continue
        category = p.get("category") or p.get("title") or f"建议{idx + 1}"
        items.append({
            "source": "awr",
            "source_ref": f"{report.id}:{idx}",
            "problem_type": category,
            "object_name": None,
            "severity": _norm_severity(p.get("severity")),
            "impact": p.get("impact"),
            "suggestion": "\n".join(filter(None, [
                p.get("title"),
                (f"发现：{p.get('finding')}" if p.get("finding") else None),
                _join_recs(p.get("recommendations")),
            ])),
            "extra": {"report_id": report.id, "category": category},
        })
    return items


def _collect_slow_sql(db: Session, inst: DBInstance) -> list[dict]:
    rows = (
        db.query(DBSlowSQL)
        .filter(DBSlowSQL.instance_id == inst.id)
        .order_by(DBSlowSQL.id.desc())
        .limit(50)
        .all()
    )
    items = []
    for r in rows:
        avg = float(r.avg_elapsed_ms or 0)
        if avg < 3000:
            continue
        items.append({
            "source": "slow_sql",
            "source_ref": r.sql_id or str(r.id),
            "problem_type": "慢SQL",
            "object_name": r.sql_id,
            "severity": _norm_severity(r.risk_level) if r.risk_level else ("high" if avg >= 10000 else "medium"),
            "impact": f"平均耗时 {avg:.0f}ms，执行 {r.executions or 0} 次",
            "suggestion": r.optimize_suggestion or r.ai_summary or (r.sql_text or "")[:500],
            "extra": {"slow_sql_id": r.id, "avg_elapsed_ms": avg},
        })
    return items


def _collect_capacity(db: Session, inst: DBInstance) -> list[dict]:
    rows = (
        db.query(DBCapacityAlert)
        .filter(DBCapacityAlert.instance_id == inst.id, DBCapacityAlert.status == "open")
        .order_by(DBCapacityAlert.id.desc())
        .limit(100)
        .all()
    )
    items = []
    for a in rows:
        items.append({
            "source": "capacity",
            "source_ref": str(a.id),
            "problem_type": a.alert_type or "容量风险",
            "object_name": a.object_name,
            "severity": _norm_severity(a.level),
            "impact": a.message,
            "suggestion": f"{a.object_name}：{a.message}（建议评估扩容/归档/清理）",
            "extra": {"alert_id": a.id, "object_type": a.object_type},
        })
    return items


def _collect_inspection(db: Session, inst: DBInstance) -> list[dict]:
    """巡检来源：合并新版 AI 诊断 findings 与传统 dbcheck 巡检结果。"""
    items = []

    # 1) 新版诊断 findings (db_diagnosis_finding)
    findings = (
        db.query(DBDiagnosisFinding)
        .filter(
            DBDiagnosisFinding.instance_id == inst.id,
            DBDiagnosisFinding.severity.in_(["critical", "warning", "high", "medium"]),
        )
        .order_by(DBDiagnosisFinding.id.desc())
        .limit(100)
        .all()
    )
    finding_proof = inspection_finding_evidence(db, findings)
    for f in findings:
        proof = finding_proof[f.id]
        measured = proof["execution_status"] in {"success", "partial"} and proof["risk_level"] != "unknown"
        items.append({
            "source": "inspection",
            "source_ref": f"finding:{f.id}",
            "problem_type": f.check_item or f.module_key or "巡检发现",
            "object_name": None,
            "severity": _norm_severity(proof["risk_level"]) if measured else "unknown",
            "impact": (f.finding or "")[:500],
            "suggestion": _suggestions_text(f.suggestions) or (f.finding or ""),
            "extra": {"finding_id": f.id, "module_key": f.module_key, "source_evidence": proof},
        })

    # 2) 传统 dbcheck 巡检（db_check_result），仅取最新一次任务的异常项
    latest = (
        db.query(DBCheckResult)
        .filter(DBCheckResult.instance_id == inst.id)
        .order_by(DBCheckResult.id.desc())
        .first()
    )
    if latest:
        rows = (
            db.query(DBCheckResult)
            .filter(
                DBCheckResult.instance_id == inst.id,
                DBCheckResult.task_id == latest.task_id,
                DBCheckResult.check_status.in_(["critical", "warning"]),
            )
            .all()
        )
        for c in rows:
            items.append({
                "source": "inspection",
                "source_ref": f"check:{c.id}",
                "problem_type": c.check_item or c.check_category or "巡检项",
                "object_name": c.metric_name,
                "severity": _norm_severity(c.check_status),
                "impact": (c.finding or "")[:500],
                "suggestion": c.suggestion or c.finding or "",
                "extra": {"check_id": c.id, "category": c.check_category,
                          "metric_value": c.metric_value, "threshold": c.threshold_value},
            })
    return items


def _collect_diagnosis(db: Session, inst: DBInstance) -> list[dict]:
    """规则诊断中心 (diagnosis_result)。"""
    rows = (
        db.query(DiagnosisResult)
        .filter(DiagnosisResult.instance_id == inst.id, DiagnosisResult.status == "open")
        .order_by(DiagnosisResult.id.desc())
        .limit(100)
        .all()
    )
    items = []
    for d in rows:
        items.append({
            "source": "diagnosis",
            "source_ref": str(d.id),
            "problem_type": d.rule_name or d.category or d.rule_code,
            "object_name": d.object_name,
            "severity": _norm_severity(d.severity),
            "impact": d.impact or d.root_cause,
            "suggestion": d.suggestion or "",
            "extra": {"diagnosis_id": d.id, "rule_code": d.rule_code, "confidence": d.confidence},
        })
    return items


def _collect_alert(db: Session, inst: DBInstance) -> list[dict]:
    """告警中心：Prometheus / Zabbix 当前活跃告警（映射到该实例的）。"""
    items = []

    prom = (
        db.query(PrometheusAlertCache)
        .filter(
            PrometheusAlertCache.db_instance_id == inst.id,
            PrometheusAlertCache.alert_status == "firing",
        )
        .all()
    )
    for a in prom:
        items.append({
            "source": "alert",
            "source_ref": f"prom:{a.id}",
            "problem_type": a.alert_name or "Prometheus 告警",
            "object_name": a.instance_name,
            "severity": _norm_severity(a.severity),
            "impact": "Prometheus 当前 firing 告警",
            "suggestion": a.alert_name or "请查看 Prometheus/Alertmanager 详情",
            "extra": {"prom_alert_id": a.id},
        })

    hostids = [
        m.zabbix_hostid for m in
        db.query(ZabbixHostMapping).filter(ZabbixHostMapping.db_instance_id == inst.id).all()
    ]
    if hostids:
        zbx = (
            db.query(ZabbixProblemCache)
            .filter(
                ZabbixProblemCache.hostid.in_(hostids),
                ZabbixProblemCache.status.in_(["firing", "active", "problem"]),
            )
            .all()
        )
        for z in zbx:
            sev = z.severity_name or (
                "high" if (z.severity or 0) >= 4 else "medium" if (z.severity or 0) >= 2 else "low"
            )
            items.append({
                "source": "alert",
                "source_ref": f"zbx:{z.id}",
                "problem_type": z.problem_name or "Zabbix 告警",
                "object_name": z.host_name,
                "severity": _norm_severity(sev),
                "impact": "Zabbix 当前活跃问题",
                "suggestion": z.problem_name or "请查看 Zabbix 详情",
                "extra": {"zbx_problem_id": z.id},
            })
    unified = (
        db.query(DBAlertEvent)
        .filter(DBAlertEvent.instance_id == inst.id, DBAlertEvent.status.in_(["firing", "suppressed"]))
        .order_by(DBAlertEvent.id.desc())
        .limit(100)
        .all()
    )
    for event in unified:
        items.append({
            "source": "alert",
            "source_ref": f"event:{event.id}",
            "problem_type": event.title or event.metric_name or "统一告警事件",
            "object_name": event.object_name,
            "severity": _norm_severity(event.severity),
            "impact": event.message,
            "suggestion": f"确认告警 {event.event_uid} 的状态、影响范围和处置责任人；必要时转工单闭环。",
            "extra": {"alert_event_id": event.id, "status": event.status, "source": event.source},
        })
    return items


def _collect_governance(db: Session, inst: DBInstance) -> list[dict]:
    rows = (
        db.query(DBArchiveJob)
        .filter(DBArchiveJob.instance_id == inst.id, DBArchiveJob.status.in_(["plan_only", "pending", "failed"]))
        .order_by(DBArchiveJob.id.desc())
        .limit(100)
        .all()
    )
    items = []
    for row in rows:
        items.append({
            "source": "governance",
            "source_ref": str(row.id),
            "problem_type": row.job_type or "数据治理方案",
            "object_name": ".".join(filter(None, [row.schema_name, row.table_name])),
            "severity": "medium" if row.status == "failed" else "low",
            "impact": f"{row.job_name} 当前状态 {row.status}",
            "suggestion": row.archive_condition or "请按 plan_only 脚本评审归档、迁移、导出或分区治理方案。",
            "script": row.plan_script,
            "extra": {"archive_job_id": row.id, "target": ".".join(filter(None, [row.target_schema, row.target_table]))},
        })
    return items


def _collect_ai(db: Session, inst: DBInstance) -> list[dict]:
    """AI 智能诊断报告 (db_ai_report)。"""
    rows = (
        db.query(AIReport)
        .filter(AIReport.instance_id == inst.id)
        .order_by(AIReport.id.desc())
        .limit(10)
        .all()
    )
    items = []
    for r in rows:
        items.append({
            "source": "ai",
            "source_ref": str(r.id),
            "problem_type": r.title or "AI 诊断",
            "object_name": None,
            "severity": _norm_severity(r.risk_level) if r.risk_level else "medium",
            "impact": r.user_question,
            "suggestion": r.summary or "",
            "extra": {"ai_report_id": r.id},
        })
    return items


def _collect_sql(db: Session, inst: DBInstance) -> list[dict]:
    """SQL 画像中识别出问题的 SQL → 建议。"""
    rows = (
        db.query(DBSqlProfile)
        .filter(DBSqlProfile.instance_id == inst.id)
        .order_by(DBSqlProfile.avg_elapsed_ms.desc())
        .limit(100)
        .all()
    )
    items = []
    for p in rows:
        issues = p.issues if isinstance(p.issues, list) else []
        if not issues:
            continue
        order = {"high": 0, "medium": 1, "low": 2}
        top = sorted(issues, key=lambda x: order.get(_norm_severity(x.get("severity")), 1))[0]
        items.append({
            "source": "sql",
            "source_ref": p.sql_id,
            "problem_type": top.get("type") or "SQL 问题",
            "object_name": p.sql_id,
            "severity": _norm_severity(top.get("severity")),
            "impact": "；".join(f"{i.get('type')}: {i.get('detail')}" for i in issues),
            "suggestion": "\n".join(filter(None, [
                "；".join(f"{i.get('type')}（{i.get('detail')}）" for i in issues),
                (p.sql_text or "")[:300],
            ])),
            "extra": {"sql_profile_id": p.id, "issues": issues},
        })
    return items


def _collect_index(db: Session, inst: DBInstance) -> list[dict]:
    """索引分析发现 → 建议（DDL 写入 script）。"""
    rows = (
        db.query(DBIndexFinding)
        .filter(DBIndexFinding.instance_id == inst.id)
        .order_by(DBIndexFinding.id.desc())
        .limit(300)
        .all()
    )
    items = []
    for f in rows:
        obj = ".".join(filter(None, [f.schema_name, f.table_name, f.index_name]))
        items.append({
            "source": "index",
            "source_ref": str(f.id),
            "problem_type": _INDEX_TYPE_LABEL.get(f.finding_type, f.finding_type),
            "object_name": obj,
            "severity": _norm_severity(f.severity),
            "impact": f.detail,
            "suggestion": f.suggestion,
            "script": f.ddl,
            "extra": {"index_finding_id": f.id, "finding_type": f.finding_type},
        })
    return items


def _collect_table(db: Session, inst: DBInstance) -> list[dict]:
    """表画像中有问题（大表/全表扫描多）的表 → 建议。"""
    rows = (
        db.query(DBTableProfile)
        .filter(DBTableProfile.instance_id == inst.id)
        .order_by(DBTableProfile.size_mb.desc())
        .limit(200)
        .all()
    )
    items = []
    for t in rows:
        issues = t.issues if isinstance(t.issues, list) else []
        if not issues:
            continue
        order = {"high": 0, "medium": 1, "low": 2}
        top = sorted(issues, key=lambda x: order.get(_norm_severity(x.get("severity")), 1))[0]
        obj = ".".join(filter(None, [t.schema_name, t.table_name]))
        items.append({
            "source": "table",
            "source_ref": obj,
            "problem_type": top.get("type") or "表问题",
            "object_name": obj,
            "severity": _norm_severity(top.get("severity")),
            "impact": "；".join(f"{i.get('type')}: {i.get('detail')}" for i in issues),
            "suggestion": "；".join(f"{i.get('type')}（{i.get('detail')}）" for i in issues),
            "extra": {"table_profile_id": t.id, "size_mb": float(t.size_mb) if t.size_mb is not None else None},
        })
    return items


_COLLECTORS = {
    "awr": _collect_awr,
    "slow_sql": _collect_slow_sql,
    "capacity": _collect_capacity,
    "inspection": _collect_inspection,
    "diagnosis": _collect_diagnosis,
    "alert": _collect_alert,
    "ai": _collect_ai,
    "sql": _collect_sql,
    "index": _collect_index,
    "table": _collect_table,
    "governance": _collect_governance,
}


def rebuild_suggestions(db: Session, instance_id: int | None = None) -> int:
    """从各源表重建建议。返回写入条数。"""
    if instance_id is not None:
        instances = db.query(DBInstance).filter(DBInstance.id == instance_id).all()
    else:
        instances = db.query(DBInstance).filter(DBInstance.enabled == 1).all()

    written = 0
    for inst in instances:
        for source, collector in _COLLECTORS.items():
            try:
                items = collector(db, inst)
            except Exception as e:
                logger.warning("collect %s for instance %s failed: %s", source, inst.id, e)
                continue

            # 保留该实例该来源下已被人工处理(非 open)的建议主键
            preserved = {
                (s.source_ref, s.problem_type)
                for s in db.query(DBSuggestion).filter(
                    DBSuggestion.instance_id == inst.id,
                    DBSuggestion.source == source,
                    DBSuggestion.status != "open",
                ).all()
            }
            if source == "inspection":
                # Keep historical source records, including invalid/broken ones. Readers
                # revalidate provenance, so keeping them cannot manufacture evidence.
                preserved.update((row.source_ref, row.problem_type) for row in db.query(DBSuggestion).filter(
                    DBSuggestion.instance_id == inst.id, DBSuggestion.source == source, DBSuggestion.status == "open",
                ).all())
            else:
                db.query(DBSuggestion).filter(
                    DBSuggestion.instance_id == inst.id,
                    DBSuggestion.source == source,
                    DBSuggestion.status == "open",
                ).delete(synchronize_session=False)

            for it in items:
                if (it["source_ref"], it["problem_type"]) in preserved:
                    continue
                db.add(DBSuggestion(
                    instance_id=inst.id,
                    db_type=inst.db_type,
                    source=it["source"],
                    source_ref=it.get("source_ref"),
                    problem_type=it.get("problem_type"),
                    object_name=it.get("object_name"),
                    severity=it.get("severity", "medium"),
                    impact=it.get("impact"),
                    suggestion=it.get("suggestion"),
                    script=it.get("script"),
                    status="open",
                    extra=it.get("extra"),
                ))
                written += 1
        db.commit()
    return written


# ---------------------------------------------------------------------------
# 画像 / 总览
# ---------------------------------------------------------------------------

def _latest_score(db: Session, instance_id: int):
    value = _latest_score_views(db, [instance_id])[instance_id]
    return value["score"], value["score_source"]


def _latest_score_views(db: Session, instance_ids) -> dict:
    ids = set(instance_ids)
    if not ids:
        return {}
    awr_ids = db.query(func.max(AwrReport.id).label("id")).filter(
        AwrReport.instance_id.in_(ids), AwrReport.performance_score.isnot(None),
    ).group_by(AwrReport.instance_id).subquery()
    reports = {row.instance_id: row for row in db.query(AwrReport).join(awr_ids, AwrReport.id == awr_ids.c.id).all()}
    task_ids = db.query(func.max(DBInspectionTask.id).label("id")).filter(
        DBInspectionTask.instance_id.in_(ids),
        # A missing latest MySQL score is a result, not permission to reuse older health.
        or_(DBInspectionTask.db_type == "mysql", DBInspectionTask.overall_score.isnot(None)),
    ).group_by(DBInspectionTask.instance_id).subquery()
    tasks = db.query(DBInspectionTask).join(task_ids, DBInspectionTask.id == task_ids.c.id).all()
    keys = [(row.instance_id, row.task_id) for row in tasks if row.db_type == "mysql"]
    modules = defaultdict(list)
    if keys:
        for row in db.query(DBInspectionModuleResult).filter(
            tuple_(DBInspectionModuleResult.instance_id, DBInspectionModuleResult.task_id).in_(keys),
        ).order_by(DBInspectionModuleResult.id).all():
            modules[(row.instance_id, row.task_id)].append(row)
    values = {identity: {"score": None, "score_source": None, "inspection_risk": None} for identity in ids}
    for task in tasks:
        view = mysql_diagnosis.project_mysql_diagnosis_evidence(task, modules[(task.instance_id, task.task_id)])["task"] if task.db_type == "mysql" else None
        score = view["overall_score"] if view else task.overall_score
        values[task.instance_id].update(
            score=float(score) if score is not None else None,
            score_source="inspection" if score is not None else None,
            inspection_risk=view["risk_level"] if view else task.risk_level,
        )
    # Preserve the existing measured AWR score precedence, including a real zero.
    for identity, report in reports.items():
        values[identity].update(score=float(report.performance_score), score_source="awr")
    return values


def compute_profile(db: Session, instance: DBInstance) -> dict:
    value = _latest_score_views(db, [instance.id])[instance.id]
    sugg = db.query(DBSuggestion).filter(
        DBSuggestion.instance_id == instance.id,
        DBSuggestion.status == "open",
    ).all()

    capacity_critical = db.query(DBCapacityAlert).filter(
        DBCapacityAlert.instance_id == instance.id,
        DBCapacityAlert.status == "open",
        DBCapacityAlert.level == "critical",
    ).count()
    return _profile_view(instance, value, sugg, capacity_critical, inspection_suggestion_evidence(db, sugg))


def _profile_view(instance, value, suggestions, capacity_critical, inspection_evidence=None):
    score, score_source = value["score"], value["score_source"]
    by_sev = {"high": 0, "medium": 0, "low": 0}
    by_source: dict[str, int] = {}
    for suggestion in suggestions:
        severity = _suggestion_risk_view(suggestion, inspection_evidence or {})["severity"]
        by_sev[severity] = by_sev.get(severity, 0) + 1
        by_source[suggestion.source] = by_source.get(suggestion.source, 0) + 1
    known_risk = value["inspection_risk"]
    risk_level = ("high" if by_sev["high"] or capacity_critical or known_risk == "critical" or (score is not None and score < 70)
                  else "medium" if by_sev["medium"] or known_risk == "warning" or (score is not None and score < 80)
                  else "low" if sum(by_sev[key] for key in ("high", "medium", "low")) or score is not None or known_risk in {"normal", "info"} else "unknown")

    return {
        "instance_id": instance.id,
        "instance_name": instance.instance_name,
        "db_type": instance.db_type,
        "host": instance.host,
        "env_type": instance.env_type,
        "score": round(score, 1) if score is not None else None,
        "score_source": score_source,
        "grade": _grade(score),
        "risk_level": risk_level,
        "open_suggestions": len(suggestions),
        "suggestions_by_severity": by_sev,
        "suggestions_by_source": by_source,
        "slow_sql_cnt": by_source.get("slow_sql", 0),
        "capacity_critical": capacity_critical,
        # Phase 2/3 summary fields for optional trend metrics.
        "unused_index_cnt": None,
        "archive_growth": None,
    }


def compute_overview(db: Session) -> dict:
    instances = db.query(DBInstance).filter(DBInstance.enabled == 1).all()
    instance_map = {inst.id: inst for inst in instances}
    score_views = _latest_score_views(db, instance_map)
    open_suggestions = db.query(DBSuggestion).filter(DBSuggestion.status == "open").all()
    inspection_evidence = inspection_suggestion_evidence(db, open_suggestions)
    grouped = defaultdict(list)
    for suggestion in open_suggestions:
        grouped[suggestion.instance_id].append(suggestion)
    capacity_counts = dict(db.query(DBCapacityAlert.instance_id, func.count(DBCapacityAlert.id)).filter(
        DBCapacityAlert.instance_id.in_(instance_map), DBCapacityAlert.status == "open", DBCapacityAlert.level == "critical",
    ).group_by(DBCapacityAlert.instance_id).all()) if instance_map else {}
    profiles = [_profile_view(inst, score_views[inst.id], grouped[inst.id], capacity_counts.get(inst.id, 0), inspection_evidence) for inst in instances]

    graded = [p for p in profiles if p["grade"] != "-"]
    healthy = [p for p in graded if p["grade"] in ("A", "B")]
    risk = [p for p in graded if p["grade"] == "C"]
    critical = [p for p in graded if p["grade"] == "D"]

    score_ranking = sorted(
        [p for p in profiles if p["score"] is not None],
        key=lambda p: p["score"], reverse=True,
    )
    risk_ranking = sorted(
        [p for p in profiles if p["risk_level"] == "high"],
        key=lambda p: p["suggestions_by_severity"].get("high", 0), reverse=True,
    )

    # 问题统计：按 source 聚合所有 open 建议
    problem_stats: dict[str, int] = {}
    for s in open_suggestions:
        problem_stats[s.source] = problem_stats.get(s.source, 0) + 1

    severity_weight = {"high": 3, "medium": 2, "low": 1}
    top_issues = []
    for s in sorted(
        open_suggestions,
        key=lambda item: (severity_weight.get(_suggestion_risk_view(item, inspection_evidence)["severity"], 0), item.id or 0),
        reverse=True,
    )[:10]:
        inst = instance_map.get(s.instance_id)
        top_issues.append({
            "id": s.id,
            "instance_id": s.instance_id,
            "instance_name": inst.instance_name if inst else str(s.instance_id),
            "db_type": inst.db_type if inst else s.db_type,
            "source": s.source,
            **_suggestion_risk_view(s, inspection_evidence),
            "problem_type": s.problem_type,
            "object_name": s.object_name,
            "impact": s.impact,
            "suggestion": s.suggestion,
        })

    return {
        "totals": {
            "total": len(instances),
            "graded": len(graded),
            "healthy": len(healthy),
            "risk": len(risk),
            "critical": len(critical),
            "ungraded": len(instances) - len(graded),
        },
        "score_ranking": score_ranking,
        "risk_ranking": risk_ranking,
        "top_issues": top_issues,
        "problem_stats": problem_stats,
        "profiles": profiles,
    }
