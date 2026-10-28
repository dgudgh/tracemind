from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.automation.models import ApprovalRequest, AutomationPlan
from app.models.ai_report import AIReport
from app.models.alert_event import DBAlertEvent
from app.models.backup_operation import BackupOperation
from app.models.db_instance import DBInstance
from app.models.dbcheck_report import DBCheckReport
from app.models.inspection_diagnosis import DBInspectionTask
from app.models.check_result import DBCheckResult
from app.services.inspection import result_writer
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL


ACTIVE_ALERT_STATUSES = {"firing", "acknowledged", "assigned", "suppressed"}
CRITICAL_SEVERITIES = {"critical", "disaster", "high", "serious", "严重"}
WARNING_SEVERITIES = {"warning", "average", "medium", "告警", "警告"}
INSPECTION_FRESHNESS_HOURS = 24


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _scope_rows(rows: Iterable[Any], allowed_instance_ids: set[int] | frozenset[int] | None) -> list[Any]:
    values = list(rows)
    if allowed_instance_ids is None:
        return values
    return [row for row in values if getattr(row, "instance_id", None) in allowed_instance_ids]


def _round_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator * 100 / denominator, 1)


def _average_minutes(pairs: Iterable[tuple[datetime | None, datetime | None]]) -> float | None:
    values = [
        max(0.0, (end - start).total_seconds() / 60)
        for start, end in pairs
        if start is not None and end is not None and end >= start
    ]
    return round(sum(values) / len(values), 1) if values else None


def _inspection_records(db: Session, instance_ids: set[int]) -> list:
    # Aggregate native tasks without loading every historical SQL/result payload.
    native = [SimpleNamespace(
        id=row.latest_id, task_id=row.task_id, instance_id=row.instance_id,
        create_time=row.create_time, overall_score=None, execution_status="unknown",
        source="native",
    ) for row in db.query(
        DBCheckResult.task_id, DBCheckResult.instance_id,
        func.max(DBCheckResult.id).label("latest_id"),
        func.max(DBCheckResult.create_time).label("create_time"),
    ).filter(DBCheckResult.instance_id.in_(instance_ids)).group_by(
        DBCheckResult.task_id, DBCheckResult.instance_id,
    ).all()]
    native_keys = {(row.instance_id, row.task_id) for row in native}
    legacy = [SimpleNamespace(
        id=row.id, task_id=row.task_id, instance_id=row.instance_id,
        create_time=row.finished_at or row.create_time, overall_score=None,
        execution_status="unknown", source="legacy",
    ) for row in db.query(DBInspectionTask).filter(
        DBInspectionTask.instance_id.in_(instance_ids),
    ).all() if (row.instance_id, row.task_id) not in native_keys]
    records = native + legacy
    latest = {}
    # Legacy attempts contribute to history only, never replace native evidence.
    for row in sorted(native, key=lambda value: (value.create_time, value.id), reverse=True):
        latest.setdefault(row.instance_id, row)
    keys = [(row.instance_id, row.task_id) for row in latest.values()]
    grouped = defaultdict(list)
    if keys:
        for row in db.query(DBCheckResult).filter(
            tuple_(DBCheckResult.instance_id, DBCheckResult.task_id).in_(keys),
        ).order_by(DBCheckResult.id).all():
            grouped[(row.instance_id, row.task_id)].append(row)
    for key, rows in grouped.items():
        summary = result_writer._task_summary(key[1], rows, None)
        latest[key[0]].overall_score = summary["overall_score"]
        latest[key[0]].execution_status = summary["task_status"]
    return records


def _slo_card(
    *,
    key: str,
    label: str,
    actual: float | None,
    target: float,
    sample_count: int,
    unit: str = "%",
    lower_is_better: bool = False,
    detail_page_key: str,
) -> dict[str, Any]:
    if actual is None or sample_count <= 0:
        status = "unknown"
    elif (actual <= target) if lower_is_better else (actual >= target):
        status = "met"
    else:
        status = "breached"
    return {
        "key": key,
        "label": label,
        "actual": actual,
        "target": target,
        "sample_count": sample_count,
        "unit": unit,
        "lower_is_better": lower_is_better,
        "status": status,
        "detail_page_key": detail_page_key,
    }


def build_operations_dashboard(
    db: Session,
    *,
    allowed_instance_ids: set[int] | frozenset[int] | None = None,
    username: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or utcnow()
    recent_7d = current_time - timedelta(days=7)
    recent_30d = current_time - timedelta(days=30)

    instance_query = db.query(DBInstance).order_by(DBInstance.id.asc())
    if allowed_instance_ids is not None:
        instance_query = instance_query.filter(DBInstance.id.in_(allowed_instance_ids or {-1}))
    instances = instance_query.all()
    instance_ids = {row.id for row in instances}

    alerts = _scope_rows(db.query(DBAlertEvent).all(), allowed_instance_ids)
    inspections = _inspection_records(db, instance_ids)
    backups = _scope_rows(db.query(BackupOperation).all(), allowed_instance_ids)
    slow_sql = _scope_rows(
        db.query(DBSlowSQL).filter(DBSlowSQL.avg_elapsed_ms >= 3000).all(),
        allowed_instance_ids,
    )
    check_reports = _scope_rows(db.query(DBCheckReport).all(), allowed_instance_ids)
    ai_reports = _scope_rows(db.query(AIReport).all(), allowed_instance_ids)
    report_artifacts = _scope_rows(db.query(ReportArtifact).all(), allowed_instance_ids)

    active_alerts = [row for row in alerts if row.status in ACTIVE_ALERT_STATUSES]
    critical_alerts = [row for row in active_alerts if str(row.severity or "").lower() in CRITICAL_SEVERITIES]
    warning_alerts = [row for row in active_alerts if str(row.severity or "").lower() in WARNING_SEVERITIES]
    breached_alerts = [row for row in active_alerts if row.sla_breached_at is not None]
    recent_alerts = [row for row in alerts if (row.first_seen or row.create_time) >= recent_30d]
    acknowledged_recent = [row for row in recent_alerts if row.acknowledged_at is not None]
    terminal_recent = [
        row for row in recent_alerts
        if row.status in {"resolved", "closed"} and (row.resolved_at or row.close_time) is not None
    ]
    sla_compliant_recent = [row for row in terminal_recent if row.sla_breached_at is None]

    recent_inspections = [row for row in inspections if recent_7d <= row.create_time <= current_time]
    inspected_instance_ids = {row.instance_id for row in recent_inspections}
    recent_backups = [
        row for row in backups
        if row.category == "backup" and row.status in {"success", "failed"}
        and (row.finished_at or row.create_time) >= recent_30d
    ]
    successful_backups = [row for row in recent_backups if row.status == "success"]
    failed_backups = [row for row in recent_backups if row.status == "failed"]

    db_type_counts = defaultdict(int)
    for row in instances:
        db_type_counts[str(row.db_type or "unknown").lower()] += 1

    risk_by_instance: dict[int, dict[str, Any]] = {}
    for row in instances:
        risk_by_instance[row.id] = {
            "instance_id": row.id,
            "instance_name": row.instance_name,
            "db_type": row.db_type,
            "host": row.host,
            "env_type": row.env_type,
            "critical_alerts": 0,
            "warning_alerts": 0,
            "sla_breaches": 0,
            "slow_sql": 0,
            "failed_backups": 0,
            "latest_inspection_score": None,
            "inspection_status": "not_collected",
            "latest_inspection_at": None,
        }
    for row in active_alerts:
        item = risk_by_instance.get(row.instance_id)
        if not item:
            continue
        severity = str(row.severity or "").lower()
        if severity in CRITICAL_SEVERITIES:
            item["critical_alerts"] += 1
        elif severity in WARNING_SEVERITIES:
            item["warning_alerts"] += 1
        if row.sla_breached_at is not None:
            item["sla_breaches"] += 1
    for row in slow_sql:
        if row.instance_id in risk_by_instance:
            risk_by_instance[row.instance_id]["slow_sql"] += 1
    for row in failed_backups:
        if row.instance_id in risk_by_instance:
            risk_by_instance[row.instance_id]["failed_backups"] += 1
    latest_inspection = {}
    for row in sorted(inspections, key=lambda item: (item.create_time, item.id), reverse=True):
        if row.source == "native":
            latest_inspection.setdefault(row.instance_id, row)
    for instance_id, row in latest_inspection.items():
        if instance_id in risk_by_instance:
            item = risk_by_instance[instance_id]
            fresh = current_time - timedelta(hours=INSPECTION_FRESHNESS_HOURS) <= row.create_time <= current_time
            item["latest_inspection_at"] = row.create_time
            item["inspection_status"] = (
                "future" if row.create_time > current_time else row.execution_status if fresh else "stale"
            )
            item["latest_inspection_score"] = row.overall_score if fresh else None

    risk_ranking = []
    for item in risk_by_instance.values():
        health_penalty = max(0, 100 - int(item["latest_inspection_score"])) if item["latest_inspection_score"] is not None else 0
        score = min(
            100,
            item["critical_alerts"] * 35
            + item["warning_alerts"] * 15
            + item["sla_breaches"] * 20
            + item["failed_backups"] * 25
            + min(item["slow_sql"] * 3, 15)
            + round(health_penalty * 0.5),
        )
        item["risk_score"] = score
        confirmed = item["inspection_status"] == "success" and item["latest_inspection_score"] is not None
        item["risk_level"] = (
            "critical" if score >= 50 else "warning" if score >= 15
            else "unknown" if not confirmed else "healthy"
        )
        item["detail_page_key"] = "instances"
        risk_ranking.append(item)
    risk_ranking.sort(key=lambda item: (-item["risk_score"], item["instance_name"]))

    pending_approvals = db.query(ApprovalRequest).filter(ApprovalRequest.status == "pending").all()
    if username:
        pending_approvals = [row for row in pending_approvals if not row.approver_id or row.approver_id == username]
    if allowed_instance_ids is not None and pending_approvals:
        allowed_targets = {
            str(value).strip().lower()
            for instance in instances
            for value in (instance.host, instance.ssh_host)
            if value
        }
        plan_ids = {row.plan_id for row in pending_approvals}
        scoped_plan_ids = {
            row.id
            for row in db.query(AutomationPlan).filter(AutomationPlan.id.in_(plan_ids)).all()
            if str(row.target_host or "").strip().lower() in allowed_targets
        }
        pending_approvals = [row for row in pending_approvals if row.plan_id in scoped_plan_ids]
    assigned_alerts = [
        row for row in active_alerts
        if (username and row.assigned_to == username)
        or (not username and not row.assigned_to and row in critical_alerts)
    ]
    work_queue = [
        {
            "key": f"alert:{row.id}",
            "category": "alert",
            "title": row.title,
            "description": f"{row.severity} · {row.status}",
            "priority": "critical" if row in critical_alerts else "warning",
            "due_at": row.sla_due_at,
            "detail_page_key": "unifiedAlerts",
            "entity_id": row.id,
        }
        for row in assigned_alerts[:10]
    ]
    work_queue.extend(
        {
            "key": f"approval:{row.id}",
            "category": "approval",
            "title": f"自动化审批 #{row.id}",
            "description": f"计划 {row.plan_id} · 等待审批",
            "priority": "warning",
            "due_at": row.expires_at,
            "detail_page_key": "automationCenter",
            "entity_id": row.id,
        }
        for row in pending_approvals[:10]
    )
    work_queue.extend(
        {
            "key": f"backup:{row.id}",
            "category": "backup",
            "title": f"备份失败 · {row.operation_no}",
            "description": row.error_message or row.validation_message or "需要检查执行日志",
            "priority": "critical",
            "due_at": None,
            "detail_page_key": "backupOverview",
            "entity_id": row.id,
        }
        for row in failed_backups[:10]
    )
    work_queue.sort(key=lambda item: (0 if item["priority"] == "critical" else 1, str(item["due_at"] or "")))

    instance_count = len(instances)
    enabled_instance_ids = {row.id for row in instances if row.enabled}
    enabled_count = len(enabled_instance_ids)
    covered_instance_ids = inspected_instance_ids & enabled_instance_ids
    inspection_coverage = _round_pct(len(covered_instance_ids), enabled_count)
    confirmed_instance_ids = {
        item["instance_id"] for item in risk_ranking
        if item["inspection_status"] == "success" and item["latest_inspection_score"] is not None
    }
    inspection_health_status = (
        "unknown" if not enabled_count else "healthy"
        if enabled_instance_ids <= confirmed_instance_ids else "warning"
    )
    backup_success_pct = _round_pct(len(successful_backups), len(recent_backups))
    alert_ack_pct = _round_pct(len(acknowledged_recent), len(recent_alerts))
    alert_sla_pct = _round_pct(len(sla_compliant_recent), len(terminal_recent))
    mtta_minutes = _average_minutes((row.first_seen, row.acknowledged_at) for row in acknowledged_recent)
    mttr_minutes = _average_minutes((row.first_seen, row.resolved_at or row.close_time) for row in terminal_recent)

    slo_cards = [
        _slo_card(key="alert_sla", label="告警 SLA 达标率", actual=alert_sla_pct, target=95, sample_count=len(terminal_recent), detail_page_key="unifiedAlerts"),
        _slo_card(key="alert_ack", label="告警确认率", actual=alert_ack_pct, target=95, sample_count=len(recent_alerts), detail_page_key="unifiedAlerts"),
        _slo_card(key="backup_success", label="备份成功率", actual=backup_success_pct, target=98, sample_count=len(recent_backups), detail_page_key="backupOverview"),
        _slo_card(key="inspection_coverage", label="7 天巡检覆盖率", actual=inspection_coverage, target=100, sample_count=enabled_count, detail_page_key="inspectionOverview"),
        _slo_card(key="mtta", label="平均确认时间", actual=mtta_minutes, target=15, sample_count=len(acknowledged_recent), unit="分钟", lower_is_better=True, detail_page_key="unifiedAlerts"),
        _slo_card(key="mttr", label="平均恢复时间", actual=mttr_minutes, target=60, sample_count=len(terminal_recent), unit="分钟", lower_is_better=True, detail_page_key="unifiedAlerts"),
    ]

    unknown_slos = len([item for item in slo_cards if item["status"] == "unknown"])
    breached_slos = len([item for item in slo_cards if item["status"] == "breached"])
    instance_risk_levels = {item["risk_level"] for item in risk_ranking}
    platform_status = (
        "critical" if critical_alerts or breached_slos >= 2 or "critical" in instance_risk_levels
        else "warning" if active_alerts or breached_slos or unknown_slos or instance_risk_levels - {"healthy"}
        else "healthy"
    )
    trace_base = {"scope_instance_ids": sorted(instance_ids), "generated_at": current_time}
    kpis = [
        {"key": "instances", "label": "数据库实例", "value": instance_count, "secondary": f"启用 {enabled_count}", "status": "healthy" if instance_count else "unknown", "detail_page_key": "instances", "trace": {**trace_base, "model": "db_instance"}},
        {"key": "alerts", "label": "活动告警", "value": len(active_alerts), "secondary": f"严重 {len(critical_alerts)} · SLA 超时 {len(breached_alerts)}", "status": "critical" if critical_alerts else "warning" if active_alerts else "healthy", "detail_page_key": "unifiedAlerts", "trace": {**trace_base, "model": "db_alert_event", "statuses": sorted(ACTIVE_ALERT_STATUSES)}},
        {"key": "inspections", "label": "7 天巡检", "value": len(recent_inspections), "secondary": f"覆盖 {len(covered_instance_ids)}/{enabled_count}", "status": inspection_health_status, "detail_page_key": "inspectionOverview", "trace": {**trace_base, "model": "db_check_result", "legacy_model": "db_inspection_task", "window_days": 7, "coverage_semantics": "attempted_tasks", "health_freshness_hours": INSPECTION_FRESHNESS_HOURS}},
        {"key": "backups", "label": "30 天备份", "value": len(recent_backups), "secondary": f"成功 {len(successful_backups)} · 失败 {len(failed_backups)}", "status": "unknown" if not recent_backups else "critical" if failed_backups else "healthy", "detail_page_key": "backupOverview", "trace": {**trace_base, "model": "backup_operation", "window_days": 30}},
        {"key": "reports", "label": "统一报告", "value": len(report_artifacts), "secondary": f"巡检原始报告 {len(check_reports)}", "status": "healthy" if report_artifacts or check_reports else "unknown", "detail_page_key": "unifiedReports", "trace": {**trace_base, "model": "report_artifact"}},
        {"key": "work_queue", "label": "个人待办", "value": len(work_queue), "secondary": f"审批 {len(pending_approvals)} · 失败备份 {len(failed_backups)}", "status": "critical" if failed_backups else "warning" if work_queue else "healthy", "detail_page_key": work_queue[0]["detail_page_key"] if work_queue else "unifiedAlerts", "trace": {**trace_base, "username": username}},
    ]

    return {
        "generated_at": current_time,
        "platform_status": platform_status,
        "scope": {"restricted": allowed_instance_ids is not None, "instance_ids": sorted(instance_ids)},
        "instance_count": instance_count,
        "enabled_instance_count": enabled_count,
        "oracle_count": db_type_counts["oracle"],
        "mysql_count": db_type_counts["mysql"],
        "postgresql_count": db_type_counts["postgresql"],
        "sqlserver_count": db_type_counts["sqlserver"],
        "slow_sql_count": len(slow_sql),
        "ai_report_count": len(ai_reports),
        "check_report_count": len(check_reports),
        "report_artifact_count": len(report_artifacts),
        "alert_count": len(active_alerts),
        "critical_count": len(critical_alerts),
        "warning_count": len(warning_alerts),
        "sla_breached_count": len(breached_alerts),
        "failed_backup_count": len(failed_backups),
        "kpis": kpis,
        "slo_cards": slo_cards,
        "work_queue": work_queue[:20],
        "risk_ranking": risk_ranking[:10],
    }
