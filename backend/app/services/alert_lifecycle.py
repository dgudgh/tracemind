from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.alert_event import (
    DBAlertActionLog,
    DBAlertEvent,
    DBAlertLifecycleLog,
    DBAlertMaintenanceWindow,
    DBAlertSourceLink,
)


ACTIVE_STATUSES = {"firing", "acknowledged", "assigned", "suppressed"}
TERMINAL_STATUSES = {"resolved", "closed"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
SLA_MINUTES = {
    "critical": 15,
    "disaster": 15,
    "high": 15,
    "严重": 15,
    "warning": 60,
    "average": 60,
    "medium": 60,
    "警告": 60,
    "告警": 60,
    "info": 240,
    "information": 240,
    "low": 240,
    "提示": 240,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def decimal_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None


def normalize_status(value: Any) -> str:
    status = str(value or "firing").strip().lower()
    aliases = {
        "active": "firing",
        "problem": "firing",
        "open": "firing",
        "ok": "resolved",
        "recovered": "resolved",
        "recovery": "resolved",
        "ack": "acknowledged",
        "isolated": "suppressed",
    }
    normalized = aliases.get(status, status)
    return normalized if normalized in ALL_STATUSES else "firing"


def build_fingerprint(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("fingerprint") or "").strip()
    if explicit:
        return explicit[:64]
    title = re.sub(r"\d+(?:\.\d+)*", "#", str(payload.get("title") or "").lower())
    title = re.sub(r"\s+", " ", title).strip()
    canonical = "|".join(
        str(value or "").strip().lower()
        for value in (
            payload.get("instance_id"),
            payload.get("db_type"),
            payload.get("object_type"),
            payload.get("object_name"),
            payload.get("metric_name"),
            title if not payload.get("metric_name") else "",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _severity_sla_minutes(severity: Any) -> int:
    return SLA_MINUTES.get(str(severity or "warning").strip().lower(), 60)


def _matching_maintenance_window(
    db: Session,
    payload: dict[str, Any],
    now: datetime,
) -> DBAlertMaintenanceWindow | None:
    rows = (
        db.query(DBAlertMaintenanceWindow)
        .filter(
            DBAlertMaintenanceWindow.enabled == 1,
            DBAlertMaintenanceWindow.starts_at <= now,
            DBAlertMaintenanceWindow.ends_at > now,
        )
        .order_by(DBAlertMaintenanceWindow.starts_at.desc())
        .all()
    )
    for row in rows:
        if row.instance_id is not None and row.instance_id != payload.get("instance_id"):
            continue
        if row.source and row.source.lower() != str(payload.get("source") or "").lower():
            continue
        if row.object_type and row.object_type.lower() != str(payload.get("object_type") or "").lower():
            continue
        if row.object_name_pattern and not fnmatch.fnmatch(
            str(payload.get("object_name") or ""), row.object_name_pattern
        ):
            continue
        if row.metric_name_pattern and not fnmatch.fnmatch(
            str(payload.get("metric_name") or ""), row.metric_name_pattern
        ):
            continue
        return row
    return None


def _record_lifecycle(
    db: Session,
    event: DBAlertEvent,
    *,
    action: str,
    from_status: str | None,
    to_status: str,
    operator: str | None = None,
    reason: str | None = None,
    detail: Any = None,
) -> None:
    db.add(
        DBAlertLifecycleLog(
            event_id=event.id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            operator=operator,
            reason=reason,
            detail_json=json_dumps(detail),
        )
    )


def _find_existing_event(
    db: Session,
    *,
    source: str,
    source_event_uid: str,
    fingerprint: str,
    incoming_status: str,
    now: datetime,
    dedup_window_minutes: int,
) -> DBAlertEvent | None:
    link = (
        db.query(DBAlertSourceLink)
        .filter(
            DBAlertSourceLink.source == source,
            DBAlertSourceLink.source_event_uid == source_event_uid,
        )
        .first()
    )
    if link:
        return db.query(DBAlertEvent).filter(DBAlertEvent.id == link.event_id).first()

    legacy = db.query(DBAlertEvent).filter(DBAlertEvent.event_uid == source_event_uid).first()
    if legacy:
        return legacy

    candidates = db.query(DBAlertEvent).filter(
        DBAlertEvent.fingerprint == fingerprint,
        DBAlertEvent.status.in_(ACTIVE_STATUSES),
    )
    if incoming_status in ACTIVE_STATUSES:
        candidates = candidates.filter(
            DBAlertEvent.last_seen >= now - timedelta(minutes=max(1, dedup_window_minutes))
        )
    return candidates.order_by(DBAlertEvent.last_seen.desc(), DBAlertEvent.id.desc()).first()


def upsert_event(
    db: Session,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    dedup_window_minutes: int = 10,
) -> DBAlertEvent:
    now = now or utcnow()
    source = str(payload.get("source") or "manual")[:50]
    source_event_uid = str(payload.get("event_uid") or "").strip()
    if not source_event_uid:
        raise ValueError("event_uid is required")
    incoming_status = normalize_status(payload.get("status"))
    fingerprint = build_fingerprint(payload)
    event = _find_existing_event(
        db,
        source=source,
        source_event_uid=source_event_uid,
        fingerprint=fingerprint,
        incoming_status=incoming_status,
        now=now,
        dedup_window_minutes=dedup_window_minutes,
    )
    created = event is None
    if created:
        first_seen = payload.get("first_seen") if isinstance(payload.get("first_seen"), datetime) else now
        last_seen = payload.get("last_seen") if isinstance(payload.get("last_seen"), datetime) else now
        event = DBAlertEvent(
            event_uid=source_event_uid,
            fingerprint=fingerprint,
            correlation_key=(payload.get("correlation_key") or fingerprint)[:200],
            source=source,
            source_ref=payload.get("source_ref"),
            title=str(payload.get("title") or source_event_uid)[:500],
            first_seen=first_seen,
            last_seen=last_seen,
            occurrence_count=1,
            source_count=1,
        )
        db.add(event)
        db.flush()
    else:
        event.occurrence_count = int(event.occurrence_count or 0) + (
            1 if incoming_status in ACTIVE_STATUSES else 0
        )

    previous_status = event.status if not created else None
    for key in (
        "source_ref",
        "instance_id",
        "db_type",
        "severity",
        "title",
        "message",
        "object_type",
        "object_name",
        "metric_name",
        "tenant_id",
        "organization_id",
    ):
        if key in payload and payload.get(key) is not None:
            setattr(event, key, payload.get(key))
    event.source = event.source or source
    event.fingerprint = fingerprint
    event.correlation_key = str(payload.get("correlation_key") or event.correlation_key or fingerprint)[:200]
    if "metric_value" in payload:
        event.metric_value = decimal_or_none(payload.get("metric_value"))
    if "threshold_value" in payload:
        event.threshold_value = decimal_or_none(payload.get("threshold_value"))
    if "labels" in payload:
        event.labels_json = json_dumps(payload.get("labels"))
    if "annotations" in payload:
        event.annotations_json = json_dumps(payload.get("annotations"))
    if "impact" in payload:
        event.impact_json = json_dumps(payload.get("impact"))
    event.last_seen = payload.get("last_seen") if isinstance(payload.get("last_seen"), datetime) else now

    maintenance = _matching_maintenance_window(db, payload, now)
    if incoming_status in TERMINAL_STATUSES:
        target_status = incoming_status
    elif maintenance:
        target_status = "suppressed"
        event.maintenance_window_id = maintenance.id
        event.suppressed_until = maintenance.ends_at
        event.suppress_reason = maintenance.reason or f"maintenance window: {maintenance.name}"
    elif event.suppressed_until and event.suppressed_until > now:
        target_status = "suppressed"
    elif previous_status in {"acknowledged", "assigned"} and incoming_status == "firing":
        target_status = previous_status
    else:
        target_status = incoming_status

    if previous_status in TERMINAL_STATUSES and target_status in ACTIVE_STATUSES:
        event.resolved_at = None
        event.close_time = None
        event.close_reason = None
        event.acknowledged_at = None
        event.acknowledged_by = None
        event.assigned_at = None
        event.assigned_to = None

    event.status = target_status
    if event.status in TERMINAL_STATUSES:
        event.resolved_at = event.resolved_at or now
    if event.status == "closed":
        event.close_time = event.close_time or now
    if not event.sla_due_at or created:
        event.sla_due_at = event.first_seen + timedelta(minutes=_severity_sla_minutes(event.severity))

    link = (
        db.query(DBAlertSourceLink)
        .filter(
            DBAlertSourceLink.source == source,
            DBAlertSourceLink.source_event_uid == source_event_uid,
        )
        .first()
    )
    if not link:
        link = DBAlertSourceLink(
            event_id=event.id,
            source=source,
            source_event_uid=source_event_uid,
            source_ref=payload.get("source_ref"),
            first_seen=now,
            last_seen=now,
            occurrence_count=1,
        )
        db.add(link)
    else:
        link.last_seen = now
        link.source_ref = payload.get("source_ref") or link.source_ref
        if incoming_status in ACTIVE_STATUSES:
            link.occurrence_count = int(link.occurrence_count or 0) + 1
    link.raw_json = json_dumps({"labels": payload.get("labels"), "annotations": payload.get("annotations")})
    db.flush()
    event.source_count = (
        db.query(func.count(DBAlertSourceLink.id))
        .filter(DBAlertSourceLink.event_id == event.id)
        .scalar()
        or 1
    )

    if created or previous_status != target_status:
        _record_lifecycle(
            db,
            event,
            action="created" if created else ("recovered" if target_status == "resolved" else "status_changed"),
            from_status=previous_status,
            to_status=target_status,
            reason=event.suppress_reason if target_status == "suppressed" else None,
            detail={"source": source, "source_event_uid": source_event_uid},
        )
    return event


def transition_event(
    db: Session,
    event: DBAlertEvent,
    *,
    action: str,
    operator: str | None = None,
    reason: str | None = None,
    assignee: str | None = None,
    suppressed_until: datetime | None = None,
    now: datetime | None = None,
) -> DBAlertEvent:
    now = now or utcnow()
    action = action.strip().lower()
    target_by_action = {
        "acknowledge": "acknowledged",
        "assign": "assigned",
        "suppress": "suppressed",
        "recover": "resolved",
        "resolve": "resolved",
        "close": "closed",
        "reopen": "firing",
    }
    if action not in target_by_action:
        raise ValueError(f"unsupported alert action: {action}")
    if event.status == "closed" and action not in {"reopen", "close"}:
        raise ValueError("closed event must be reopened before other transitions")
    if action == "assign" and not assignee:
        raise ValueError("assignee is required")
    if action == "suppress" and (not suppressed_until or suppressed_until <= now):
        raise ValueError("suppressed_until must be in the future")

    previous = event.status
    target = target_by_action[action]
    event.status = target
    if action == "acknowledge":
        event.acknowledged_at = now
        event.acknowledged_by = operator
    elif action == "assign":
        event.assigned_at = now
        event.assigned_to = assignee
        if not event.acknowledged_at:
            event.acknowledged_at = now
            event.acknowledged_by = operator
    elif action == "suppress":
        event.suppressed_until = suppressed_until
        event.suppress_reason = reason
    elif action in {"recover", "resolve"}:
        event.resolved_at = now
    elif action == "close":
        event.resolved_at = event.resolved_at or now
        event.close_time = now
        event.close_reason = reason
    elif action == "reopen":
        event.resolved_at = None
        event.close_time = None
        event.close_reason = None
        event.suppressed_until = None
        event.suppress_reason = None
        event.sla_due_at = now + timedelta(minutes=_severity_sla_minutes(event.severity))
        event.sla_breached_at = None

    _record_lifecycle(
        db,
        event,
        action=action,
        from_status=previous,
        to_status=target,
        operator=operator,
        reason=reason,
        detail={"assignee": assignee, "suppressed_until": suppressed_until},
    )
    return event


def refresh_runtime_states(db: Session, now: datetime | None = None) -> None:
    now = now or utcnow()
    expired = db.query(DBAlertEvent).filter(
        DBAlertEvent.status == "suppressed",
        DBAlertEvent.suppressed_until.is_not(None),
        DBAlertEvent.suppressed_until <= now,
        DBAlertEvent.resolved_at.is_(None),
    ).all()
    for event in expired:
        previous = event.status
        event.status = "firing"
        event.suppressed_until = None
        event.suppress_reason = None
        event.maintenance_window_id = None
        _record_lifecycle(
            db,
            event,
            action="suppression_expired",
            from_status=previous,
            to_status="firing",
        )

    breached = db.query(DBAlertEvent).filter(
        DBAlertEvent.status.in_(ACTIVE_STATUSES),
        DBAlertEvent.sla_due_at.is_not(None),
        DBAlertEvent.sla_due_at <= now,
        DBAlertEvent.sla_breached_at.is_(None),
    ).all()
    for event in breached:
        event.sla_breached_at = now
        _record_lifecycle(
            db,
            event,
            action="sla_breached",
            from_status=event.status,
            to_status=event.status,
        )


def event_to_dict(row: DBAlertEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_uid": row.event_uid,
        "fingerprint": row.fingerprint,
        "correlation_key": row.correlation_key,
        "source": row.source,
        "source_ref": row.source_ref,
        "source_count": row.source_count or 1,
        "occurrence_count": row.occurrence_count or 1,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "message": row.message,
        "object_type": row.object_type,
        "object_name": row.object_name,
        "metric_name": row.metric_name,
        "metric_value": decimal_value(row.metric_value),
        "threshold_value": decimal_value(row.threshold_value),
        "labels": json_loads(row.labels_json),
        "annotations": json_loads(row.annotations_json),
        "impact": json_loads(row.impact_json),
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
        "acknowledged_at": row.acknowledged_at,
        "acknowledged_by": row.acknowledged_by,
        "assigned_at": row.assigned_at,
        "assigned_to": row.assigned_to,
        "suppressed_until": row.suppressed_until,
        "suppress_reason": row.suppress_reason,
        "maintenance_window_id": row.maintenance_window_id,
        "sla_due_at": row.sla_due_at,
        "sla_breached_at": row.sla_breached_at,
        "sla_status": "breached" if row.sla_breached_at else ("active" if row.status in ACTIVE_STATUSES else "stopped"),
        "resolved_at": row.resolved_at,
        "close_time": row.close_time,
        "close_reason": row.close_reason,
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def lifecycle_to_dict(row: DBAlertLifecycleLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "action": row.action,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "operator": row.operator,
        "reason": row.reason,
        "detail": json_loads(row.detail_json),
        "create_time": row.create_time,
    }


def source_link_to_dict(row: DBAlertSourceLink) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "source": row.source,
        "source_event_uid": row.source_event_uid,
        "source_ref": row.source_ref,
        "occurrence_count": row.occurrence_count,
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
    }


def maintenance_window_to_dict(row: DBAlertMaintenanceWindow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "starts_at": row.starts_at,
        "ends_at": row.ends_at,
        "timezone": row.timezone,
        "instance_id": row.instance_id,
        "source": row.source,
        "object_type": row.object_type,
        "object_name_pattern": row.object_name_pattern,
        "metric_name_pattern": row.metric_name_pattern,
        "reason": row.reason,
        "enabled": bool(row.enabled),
        "created_by": row.created_by,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


ACTION_RETRY_BATCH_SIZE = 200


def retry_due_actions(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = ACTION_RETRY_BATCH_SIZE,
) -> dict[str, Any]:
    """Re-arm failed alert actions whose retry delay has elapsed.

    Execution of an alert action belongs to an external worker: it claims rows in
    ``pending`` and reports the outcome back through the action-result endpoint,
    which is the only writer of ``next_retry_time``. This consumer exists because
    that field was previously written and never read - a scheduled retry that
    nobody picks up.

    Re-arming deliberately does **not** invent an outcome: it only moves the row
    back to ``pending`` and clears the due stamp so the worker can claim it again.
    Rows in ``dead_letter`` stay untouched - those exhausted their budget and need
    an operator decision, not another automatic attempt.
    """
    current = now or utcnow()
    limit = max(0, min(int(limit), ACTION_RETRY_BATCH_SIZE))
    due = db.query(DBAlertActionLog.id).filter(
        DBAlertActionLog.action_status == "failed",
        DBAlertActionLog.next_retry_time.isnot(None),
        DBAlertActionLog.next_retry_time <= current,
        DBAlertActionLog.attempt_count < DBAlertActionLog.max_retries,
        DBAlertActionLog.dead_letter_at.is_(None),
    ).order_by(DBAlertActionLog.next_retry_time, DBAlertActionLog.id).limit(limit).all()
    retried: list[int] = []
    for (action_id,) in due:
        # Re-assert the due predicate inside UPDATE: another consumer instance may
        # have re-armed the row between our snapshot and this statement.
        updated = db.query(DBAlertActionLog).filter(
            DBAlertActionLog.id == action_id,
            DBAlertActionLog.action_status == "failed",
            DBAlertActionLog.next_retry_time.isnot(None),
            DBAlertActionLog.next_retry_time <= current,
            DBAlertActionLog.attempt_count < DBAlertActionLog.max_retries,
        ).update(
            {
                DBAlertActionLog.action_status: "pending",
                DBAlertActionLog.next_retry_time: None,
                DBAlertActionLog.update_time: current,
            },
            synchronize_session=False,
        )
        if updated:
            retried.append(action_id)
    if retried:
        db.commit()
    return {"scanned": len(due), "retried": retried, "rearmed": len(retried)}
