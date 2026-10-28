import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.alert_event import (
    DBAlertActionLog,
    DBAlertEvent,
    DBAlertLifecycleLog,
    DBAlertMaintenanceWindow,
    DBAlertSourceLink,
)
from app.models.capacity_policy import DBCapacityAlert
from app.models.db_instance import DBInstance
from app.models.observability import ObservabilityAlertEvent
from app.models.external_monitoring import PrometheusAlertCache, ZabbixHostMapping, ZabbixProblemCache
from app.services.observability import alert_diagnosis_service
from app.services.observability.metric_normalizer import infer_metric_from_text, normalize_metric_name
from app.services import alert_import_service, alert_lifecycle
from app.services.access_control import resolve_scope

router = APIRouter(prefix="/alert-events", tags=["统一告警事件"])


class AlertEventPayload(BaseModel):
    event_uid: str = Field(..., max_length=200)
    fingerprint: str | None = Field(None, max_length=64)
    correlation_key: str | None = Field(None, max_length=200)
    source: str = Field("manual", max_length=50)
    source_ref: str | None = None
    instance_id: int | None = None
    db_type: str | None = None
    severity: str = "warning"
    status: str = "firing"
    title: str = Field(..., max_length=500)
    message: str | None = None
    object_type: str | None = None
    object_name: str | None = None
    metric_name: str | None = None
    metric_value: Decimal | None = None
    threshold_value: Decimal | None = None
    labels: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None
    tenant_id: int | None = None
    organization_id: int | None = None


class AlertEventStatusPayload(BaseModel):
    status: str = "closed"
    reason: str | None = None


class AlertActionPayload(BaseModel):
    action_type: str = "notify"
    target: str | None = None
    request: dict[str, Any] | None = None
    operator: str | None = None
    plan_only: bool = True
    idempotency_key: str | None = Field(None, max_length=160)
    max_retries: int = Field(3, ge=0, le=20)


class AlertTransitionPayload(BaseModel):
    action: str = Field(..., pattern="^(acknowledge|assign|suppress|recover|resolve|close|reopen)$")
    reason: str | None = Field(None, max_length=500)
    assignee: str | None = Field(None, max_length=100)
    suppressed_until: datetime | None = None
    suppress_minutes: int | None = Field(None, ge=1, le=525600)
    operator: str | None = Field(None, max_length=100)


class MaintenanceWindowPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime
    timezone: str = Field("Asia/Shanghai", max_length=50)
    instance_id: int | None = None
    source: str | None = Field(None, max_length=50)
    object_type: str | None = Field(None, max_length=100)
    object_name_pattern: str | None = Field(None, max_length=300)
    metric_name_pattern: str | None = Field(None, max_length=200)
    reason: str | None = Field(None, max_length=500)
    enabled: bool = True


class AlertActionResultPayload(BaseModel):
    success: bool
    response: dict[str, Any] | None = None
    error_message: str | None = Field(None, max_length=2000)
    retry_delay_seconds: int = Field(300, ge=1, le=86400)


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _decimal_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _external_metric_name(source_metric: Any, *parts: Any) -> str:
    inferred = infer_metric_from_text(source_metric, *parts)
    if inferred:
        return inferred
    return normalize_metric_name(str(source_metric or ""))


def _instance_map(db: Session) -> dict[int, DBInstance]:
    return {row.id: row for row in db.query(DBInstance).all()}


def _event_to_dict(row: DBAlertEvent) -> dict:
    return alert_lifecycle.event_to_dict(row)


def _action_to_dict(row: DBAlertActionLog) -> dict:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "action_type": row.action_type,
        "action_status": row.action_status,
        "idempotency_key": row.idempotency_key,
        "target": row.target,
        "request": _json_loads(row.request_json),
        "response": _json_loads(row.response_json),
        "error_message": row.error_message,
        "operator": row.operator,
        "attempt_count": row.attempt_count,
        "max_retries": row.max_retries,
        "next_retry_time": row.next_retry_time,
        "dead_letter_at": row.dead_letter_at,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def _upsert_event(db: Session, payload: dict) -> DBAlertEvent:
    return alert_lifecycle.upsert_event(db, payload)


def _request_auth(request: Request) -> dict[str, Any]:
    return getattr(request.state, "auth", {}) or {}


def _request_operator(request: Request, fallback: str | None = None) -> str | None:
    auth = _request_auth(request)
    return auth.get("username") or auth.get("subject") or auth.get("role") or fallback


def _scope_native_ids(request: Request, db: Session, minimum_access: str = "read") -> frozenset[int] | None:
    auth = _request_auth(request)
    if auth.get("role") == "admin" or auth.get("source") not in {"db", "identity"}:
        return None
    user_id = int(auth.get("user_id") or 0)
    if not user_id:
        return None
    scope = resolve_scope(db, user_id, minimum_access=minimum_access)
    return scope.native_instance_ids if scope.restricted else None


def _scoped_event_query(request: Request, db: Session, minimum_access: str = "read"):
    query = db.query(DBAlertEvent)
    allowed = _scope_native_ids(request, db, minimum_access=minimum_access)
    if allowed is not None:
        query = query.filter(DBAlertEvent.instance_id.in_(allowed))
    return query


def _get_scoped_event(request: Request, db: Session, event_id: int, minimum_access: str = "read") -> DBAlertEvent:
    row = _scoped_event_query(request, db, minimum_access=minimum_access).filter(DBAlertEvent.id == event_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="告警事件不存在或不在当前数据范围内")
    return row


@router.get("")
def list_events(
    request: Request,
    status: str = Query("all"),
    source: str | None = Query(None),
    severity: str | None = Query(None),
    instance_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    alert_lifecycle.refresh_runtime_states(db)
    query = _scoped_event_query(request, db)
    if status == "active":
        query = query.filter(DBAlertEvent.status.in_(alert_lifecycle.ACTIVE_STATUSES))
    elif status != "all":
        query = query.filter(DBAlertEvent.status == status)
    if source:
        query = query.filter(DBAlertEvent.source == source)
    if severity:
        query = query.filter(DBAlertEvent.severity == severity)
    if instance_id:
        query = query.filter(DBAlertEvent.instance_id == instance_id)
    rows = query.order_by(DBAlertEvent.last_seen.desc(), DBAlertEvent.id.desc()).limit(limit).all()
    db.commit()
    return {"items": [_event_to_dict(row) for row in rows]}


@router.post("")
def upsert_event(request: Request, payload: AlertEventPayload, db: Session = Depends(get_db)):
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    if allowed is not None and payload.instance_id not in allowed:
        raise HTTPException(status_code=403, detail="目标实例不在当前用户的可操作数据范围内")
    row = _upsert_event(db, payload.model_dump())
    db.commit()
    db.refresh(row)
    return _event_to_dict(row)


@router.post("/import/capacity")
def import_capacity_alerts(request: Request, db: Session = Depends(get_db)):
    instances = _instance_map(db)
    query = db.query(DBCapacityAlert)
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    if allowed is not None:
        query = query.filter(DBCapacityAlert.instance_id.in_(allowed))
    rows = (
        query
        .filter(DBCapacityAlert.status.in_(["open", "isolated"]))
        .order_by(DBCapacityAlert.create_time.desc(), DBCapacityAlert.id.desc())
        .limit(500)
        .all()
    )
    for item in rows:
        instance = instances.get(item.instance_id)
        _upsert_event(db, {
            "event_uid": f"capacity:{item.id}",
            "source": "capacity",
            "source_ref": str(item.id),
            "instance_id": item.instance_id,
            "db_type": item.db_type or (instance.db_type if instance else None),
            "severity": item.level or "warning",
            "status": "suppressed" if item.status == "isolated" else "firing",
            "title": item.message,
            "message": item.message,
            "object_type": item.object_type,
            "object_name": item.object_name,
            "metric_name": item.alert_type,
            "metric_value": item.metric_value,
            "threshold_value": item.threshold_value,
            "labels": {"capacity_status": item.status, "instance_name": instance.instance_name if instance else None},
            "annotations": {"source_table": "db_capacity_alert", "isolate_reason": item.isolate_reason},
        })
    db.commit()
    return {"imported": len(rows)}


@router.post("/import/external")
def import_external_alerts(request: Request, db: Session = Depends(get_db)):
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    return alert_import_service.import_external_alerts(db, allowed_instance_ids=allowed)


@router.get("/maintenance-windows")
def list_maintenance_windows(
    request: Request,
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(DBAlertMaintenanceWindow)
    allowed = _scope_native_ids(request, db)
    if allowed is not None:
        query = query.filter(
            (DBAlertMaintenanceWindow.instance_id.is_(None))
            | (DBAlertMaintenanceWindow.instance_id.in_(allowed))
        )
    if active_only:
        now = alert_lifecycle.utcnow()
        query = query.filter(
            DBAlertMaintenanceWindow.enabled == 1,
            DBAlertMaintenanceWindow.starts_at <= now,
            DBAlertMaintenanceWindow.ends_at > now,
        )
    rows = query.order_by(DBAlertMaintenanceWindow.starts_at.desc()).all()
    return {"items": [alert_lifecycle.maintenance_window_to_dict(row) for row in rows]}


@router.post("/maintenance-windows")
def create_maintenance_window(
    request: Request,
    payload: MaintenanceWindowPayload,
    db: Session = Depends(get_db),
):
    if payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=400, detail="维护窗口结束时间必须晚于开始时间")
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    if allowed is not None and payload.instance_id not in allowed:
        raise HTTPException(status_code=403, detail="只能为可操作范围内的实例创建维护窗口")
    values = payload.model_dump()
    values["enabled"] = 1 if values["enabled"] else 0
    row = DBAlertMaintenanceWindow(
        **values,
        created_by=_request_operator(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return alert_lifecycle.maintenance_window_to_dict(row)


@router.put("/maintenance-windows/{window_id}")
def update_maintenance_window(
    request: Request,
    window_id: int,
    payload: MaintenanceWindowPayload,
    db: Session = Depends(get_db),
):
    row = db.query(DBAlertMaintenanceWindow).filter(DBAlertMaintenanceWindow.id == window_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    if payload.ends_at <= payload.starts_at:
        raise HTTPException(status_code=400, detail="维护窗口结束时间必须晚于开始时间")
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    if allowed is not None and (row.instance_id not in allowed or payload.instance_id not in allowed):
        raise HTTPException(status_code=403, detail="维护窗口不在当前用户的可操作范围内")
    for key, value in payload.model_dump().items():
        setattr(row, key, 1 if key == "enabled" and value else (0 if key == "enabled" else value))
    db.commit()
    db.refresh(row)
    return alert_lifecycle.maintenance_window_to_dict(row)


@router.delete("/maintenance-windows/{window_id}")
def delete_maintenance_window(request: Request, window_id: int, db: Session = Depends(get_db)):
    row = db.query(DBAlertMaintenanceWindow).filter(DBAlertMaintenanceWindow.id == window_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="维护窗口不存在")
    allowed = _scope_native_ids(request, db, minimum_access="operate")
    if allowed is not None and row.instance_id not in allowed:
        raise HTTPException(status_code=403, detail="维护窗口不在当前用户的可操作范围内")
    db.delete(row)
    db.commit()
    return {"detail": "维护窗口已删除"}


@router.post("/{event_id}/transition")
def transition_event(
    request: Request,
    event_id: int,
    payload: AlertTransitionPayload,
    db: Session = Depends(get_db),
):
    row = _get_scoped_event(request, db, event_id, minimum_access="operate")
    suppressed_until = payload.suppressed_until
    if not suppressed_until and payload.suppress_minutes:
        suppressed_until = alert_lifecycle.utcnow() + timedelta(minutes=payload.suppress_minutes)
    try:
        alert_lifecycle.transition_event(
            db,
            row,
            action=payload.action,
            operator=_request_operator(request, payload.operator),
            reason=payload.reason,
            assignee=payload.assignee,
            suppressed_until=suppressed_until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _event_to_dict(row)


@router.post("/{event_id}/close")
def close_event(request: Request, event_id: int, payload: AlertEventStatusPayload, db: Session = Depends(get_db)):
    row = _get_scoped_event(request, db, event_id, minimum_access="operate")
    action = "resolve" if payload.status == "resolved" else "close"
    try:
        alert_lifecycle.transition_event(
            db,
            row,
            action=action,
            operator=_request_operator(request),
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(DBAlertActionLog(
        event_id=row.id,
        action_type="close",
        action_status="success",
        target="alert-events",
        request_json=_json_dumps(payload.model_dump()),
        response_json=_json_dumps({"status": row.status}),
        attempt_count=1,
    ))
    db.commit()
    db.refresh(row)
    return _event_to_dict(row)


@router.get("/{event_id}/metric-trends")
def event_metric_trends(
    request: Request,
    event_id: int,
    window_minutes: int = Query(120, ge=15, le=1440),
    db: Session = Depends(get_db),
):
    row = _get_scoped_event(request, db, event_id)
    return alert_diagnosis_service.metric_trends(db, row, window_minutes)


@router.get("/{event_id}/evidence-chain")
def event_evidence_chain(
    request: Request,
    event_id: int,
    window_minutes: int = Query(120, ge=15, le=1440),
    db: Session = Depends(get_db),
):
    row = _get_scoped_event(request, db, event_id)
    return alert_diagnosis_service.build_evidence_chain(db, row, window_minutes)


@router.post("/{event_id}/diagnose")
def diagnose_event(request: Request, event_id: int, db: Session = Depends(get_db)):
    row = _get_scoped_event(request, db, event_id, minimum_access="operate")
    try:
        return alert_diagnosis_service.run_diagnosis_from_event(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{event_id}/ticket-description")
def event_ticket_description(
    request: Request,
    event_id: int,
    window_minutes: int = Query(120, ge=15, le=1440),
    db: Session = Depends(get_db),
):
    row = _get_scoped_event(request, db, event_id)
    return alert_diagnosis_service.ticket_description(db, row, window_minutes)


@router.post("/{event_id}/actions")
def create_action_log(
    request: Request,
    event_id: int,
    payload: AlertActionPayload,
    db: Session = Depends(get_db),
):
    event = _get_scoped_event(request, db, event_id, minimum_access="operate")
    idempotency_key = payload.idempotency_key or (
        f"alert-action:{event.id}:{payload.action_type}:{payload.target or '-'}"
    )
    operator = _request_operator(request, payload.operator)
    action_request = payload.request or {"event_uid": event.event_uid, "title": event.title}
    existing = db.query(DBAlertActionLog).filter(
        DBAlertActionLog.idempotency_key == idempotency_key,
    ).first()
    if existing:
        # An idempotency key is not an authorization grant to another event or actor.
        if (
            existing.event_id != event.id
            or existing.operator != operator
            or existing.action_type != payload.action_type
            or existing.target != payload.target
            or _json_loads(existing.request_json) != action_request
            or existing.max_retries != payload.max_retries
            or (existing.action_status == "plan_only") != payload.plan_only
        ):
            raise HTTPException(status_code=409, detail="幂等键已被其他告警动作或参数使用")
        _get_scoped_event(request, db, existing.event_id, minimum_access="operate")
        return _action_to_dict(existing)
    row = DBAlertActionLog(
        event_id=event.id,
        action_type=payload.action_type,
        action_status="plan_only" if payload.plan_only else "pending",
        target=payload.target,
        operator=operator,
        idempotency_key=idempotency_key,
        max_retries=payload.max_retries,
        request_json=_json_dumps(action_request),
        response_json=_json_dumps({"mode": "plan_only" if payload.plan_only else "pending"}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _action_to_dict(row)


@router.get("/actions")
def list_action_logs(
    request: Request,
    event_id: int | None = Query(None),
    action_type: str | None = Query(None),
    action_status: str | None = Query(None, description="按动作状态过滤，外部执行端用 pending 领取待执行动作"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(DBAlertActionLog).join(DBAlertEvent, DBAlertEvent.id == DBAlertActionLog.event_id)
    allowed = _scope_native_ids(request, db)
    if allowed is not None:
        query = query.filter(DBAlertEvent.instance_id.in_(allowed))
    if event_id:
        query = query.filter(DBAlertActionLog.event_id == event_id)
    if action_type:
        query = query.filter(DBAlertActionLog.action_type == action_type)
    if action_status:
        query = query.filter(DBAlertActionLog.action_status == action_status)
    rows = query.order_by(DBAlertActionLog.create_time.desc(), DBAlertActionLog.id.desc()).limit(limit).all()
    return {"items": [_action_to_dict(row) for row in rows]}


@router.post("/actions/{action_id}/result")
def record_action_result(
    request: Request,
    action_id: int,
    payload: AlertActionResultPayload,
    db: Session = Depends(get_db),
):
    row = (
        db.query(DBAlertActionLog)
        .join(DBAlertEvent, DBAlertEvent.id == DBAlertActionLog.event_id)
        .filter(DBAlertActionLog.id == action_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="告警动作不存在")
    _get_scoped_event(request, db, row.event_id, minimum_access="operate")
    now = alert_lifecycle.utcnow()
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.response_json = _json_dumps(payload.response)
    row.error_message = payload.error_message
    if payload.success:
        row.action_status = "success"
        row.next_retry_time = None
        row.dead_letter_at = None
    elif row.attempt_count >= int(row.max_retries or 0):
        row.action_status = "dead_letter"
        row.next_retry_time = None
        row.dead_letter_at = now
    else:
        row.action_status = "failed"
        row.next_retry_time = now + timedelta(seconds=payload.retry_delay_seconds)
    db.commit()
    db.refresh(row)
    return _action_to_dict(row)


@router.post("/actions/{action_id}/retry")
def retry_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="告警动作不存在")
    _get_scoped_event(request, db, row.event_id, minimum_access="operate")
    if row.action_status not in {"failed", "dead_letter"}:
        raise HTTPException(status_code=400, detail="只有失败或死信动作可以重试")
    if row.attempt_count >= row.max_retries:
        row.max_retries = row.attempt_count + 1
    row.action_status = "pending"
    row.next_retry_time = alert_lifecycle.utcnow()
    row.dead_letter_at = None
    db.commit()
    db.refresh(row)
    return _action_to_dict(row)


@router.get("/stats")
def event_stats(request: Request, db: Session = Depends(get_db)):
    alert_lifecycle.refresh_runtime_states(db)
    base = _scoped_event_query(request, db)
    by_status = dict(base.with_entities(DBAlertEvent.status, func.count(DBAlertEvent.id)).group_by(DBAlertEvent.status).all())
    by_source = dict(base.with_entities(DBAlertEvent.source, func.count(DBAlertEvent.id)).group_by(DBAlertEvent.source).all())
    by_severity = dict(base.with_entities(DBAlertEvent.severity, func.count(DBAlertEvent.id)).group_by(DBAlertEvent.severity).all())
    total = base.count()
    sla_breached = base.filter(DBAlertEvent.sla_breached_at.is_not(None), DBAlertEvent.status.in_(alert_lifecycle.ACTIVE_STATUSES)).count()
    db.commit()
    return {
        "total": total,
        "firing": by_status.get("firing", 0),
        "closed": by_status.get("closed", 0),
        "resolved": by_status.get("resolved", 0),
        "suppressed": by_status.get("suppressed", 0),
        "acknowledged": by_status.get("acknowledged", 0),
        "assigned": by_status.get("assigned", 0),
        "active": sum(by_status.get(status, 0) for status in alert_lifecycle.ACTIVE_STATUSES),
        "sla_breached": sla_breached,
        "by_status": by_status,
        "by_source": by_source,
        "by_severity": by_severity,
    }


@router.get("/{event_id}/lifecycle")
def event_lifecycle(request: Request, event_id: int, db: Session = Depends(get_db)):
    _get_scoped_event(request, db, event_id)
    rows = db.query(DBAlertLifecycleLog).filter(
        DBAlertLifecycleLog.event_id == event_id,
    ).order_by(DBAlertLifecycleLog.id.asc()).all()
    return {"items": [alert_lifecycle.lifecycle_to_dict(row) for row in rows]}


@router.get("/{event_id}/sources")
def event_sources(request: Request, event_id: int, db: Session = Depends(get_db)):
    _get_scoped_event(request, db, event_id)
    rows = db.query(DBAlertSourceLink).filter(
        DBAlertSourceLink.event_id == event_id,
    ).order_by(DBAlertSourceLink.id.asc()).all()
    return {"items": [alert_lifecycle.source_link_to_dict(row) for row in rows]}


@router.get("/{event_id}")
def event_detail(request: Request, event_id: int, db: Session = Depends(get_db)):
    row = _get_scoped_event(request, db, event_id)
    result = _event_to_dict(row)
    result["sources"] = event_sources(request, event_id, db)["items"]
    result["lifecycle"] = event_lifecycle(request, event_id, db)["items"]
    return result
