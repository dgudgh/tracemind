from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.observability import ObservabilityAlertEvent
from app.services import alert_lifecycle
from app.services.observability import binding_service
from app.services.observability.metric_normalizer import STANDARD_METRICS, infer_metric_from_text, normalize_metric_name


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def alert_to_dict(row: ObservabilityAlertEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "alert_key": row.alert_key,
        "instance_id": row.instance_id,
        "host_id": row.host_id,
        "severity": row.severity,
        "title": row.title,
        "content": row.content,
        "metric_name": row.metric_name,
        "metric_value": row.metric_value,
        "status": row.status,
        "first_time": row.first_time,
        "last_time": row.last_time,
        "recover_time": row.recover_time,
        "raw_data": row.raw_data,
    }


def list_alerts(db: Session, status: str = "all", limit: int = 200, source_type: str | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilityAlertEvent)
    if source_type:
        query = query.filter(ObservabilityAlertEvent.source_type == source_type)
    if status != "all":
        query = query.filter(ObservabilityAlertEvent.status == status)
    rows = query.order_by(ObservabilityAlertEvent.last_time.desc(), ObservabilityAlertEvent.id.desc()).limit(limit).all()
    return [alert_to_dict(row) for row in rows]


def ingest_alert(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    alert_key = (
        payload.get("alert_key")
        or payload.get("alert_id")
        or payload.get("fingerprint")
        or f"{payload.get('source_type', 'webhook')}:{payload.get('title', 'alert')}"
    )
    source_type = payload.get("source_type") or payload.get("source") or "webhook"
    status = payload.get("status") or "firing"
    labels = _extract_dict(payload.get("labels") or payload.get("tags") or (payload.get("raw_data") or {}).get("labels"))
    annotations = _extract_dict(payload.get("annotations") or (payload.get("raw_data") or {}).get("annotations"))
    host_id = payload.get("host_id") or labels.get("instance") or labels.get("host") or labels.get("hostid")
    metric_name = payload.get("metric_name") or labels.get("metric") or labels.get("__name__") or labels.get("alertname")
    standard_metric = normalize_metric_name(metric_name)
    inferred_metric = infer_metric_from_text(
        payload.get("title"),
        payload.get("content"),
        payload.get("message"),
        labels.get("alertname"),
        annotations.get("summary"),
        annotations.get("description"),
        (payload.get("raw_data") or {}).get("name") if isinstance(payload.get("raw_data"), dict) else None,
        (payload.get("raw_data") or {}).get("opdata") if isinstance(payload.get("raw_data"), dict) else None,
    )
    if inferred_metric and (standard_metric not in STANDARD_METRICS or str(metric_name or "").isdigit()):
        standard_metric = inferred_metric
    instance_id = payload.get("instance_id")
    resolved: dict[str, Any] = {}
    if not instance_id:
        resolved = binding_service.resolve_instance(
            db,
            source_type=source_type,
            host_id=str(host_id or ""),
            labels=labels,
            raw_data=payload.get("raw_data") if isinstance(payload.get("raw_data"), dict) else payload,
        )
        instance_id = resolved.get("instance_id")
    row = db.query(ObservabilityAlertEvent).filter(
        ObservabilityAlertEvent.source_type == source_type,
        ObservabilityAlertEvent.alert_key == str(alert_key),
    ).first()
    now = datetime.utcnow()
    if not row:
        row = ObservabilityAlertEvent(
            source_type=source_type,
            alert_key=str(alert_key),
            first_time=_parse_time(payload.get("first_time")) or now,
        )
        db.add(row)
    row.source_id = payload.get("source_id")
    row.instance_id = instance_id
    row.host_id = str(host_id or "") or None
    row.severity = payload.get("severity") or "warning"
    row.title = payload.get("title") or payload.get("alert_name") or "Observability Alert"
    row.content = payload.get("content") or payload.get("message") or payload.get("description")
    row.metric_name = standard_metric
    row.metric_value = str(payload.get("metric_value")) if payload.get("metric_value") is not None else None
    row.status = status
    row.last_time = _parse_time(payload.get("last_time")) or now
    row.recover_time = _parse_time(payload.get("recover_time")) if status in {"resolved", "closed", "recovered"} else None
    row.raw_data = {
        **(payload.get("raw_data") if isinstance(payload.get("raw_data"), dict) else payload),
        "labels": labels,
        "annotations": annotations,
        "standard_metric": standard_metric,
        "binding": resolved,
    }
    db.commit()
    db.refresh(row)
    _mirror_unified_alert(db, row)
    return alert_to_dict(row)


def ingest_alertmanager(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    alerts = payload.get("alerts") or []
    count = 0
    for item in alerts:
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        alert_name = labels.get("alertname") or "Alertmanager Alert"
        host_id = labels.get("instance") or labels.get("host") or labels.get("job")
        ingest_alert(
            db,
            {
                "source_type": "prometheus",
                "alert_key": item.get("fingerprint") or f"{alert_name}:{host_id or item.get('generatorURL')}",
                "host_id": host_id,
                "severity": labels.get("severity") or "warning",
                "title": alert_name,
                "content": annotations.get("description") or annotations.get("summary"),
                "metric_name": labels.get("metric") or labels.get("__name__"),
                "status": item.get("status") or payload.get("status") or "firing",
                "first_time": item.get("startsAt"),
                "last_time": item.get("endsAt") if item.get("status") == "resolved" else None,
                "labels": labels,
                "annotations": annotations,
                "raw_data": item,
            },
        )
        count += 1
    return {"received_count": count}


def _mirror_unified_alert(db: Session, row: ObservabilityAlertEvent) -> None:
    event_uid = f"observability:{row.source_type}:{row.alert_key}"
    instance = db.query(DBInstance).filter(DBInstance.id == row.instance_id).first() if row.instance_id else None
    raw = row.raw_data if isinstance(row.raw_data, dict) else {}
    alert_lifecycle.upsert_event(db, {
        "event_uid": event_uid,
        "source": row.source_type,
        "source_ref": str(row.id),
        "instance_id": row.instance_id,
        "db_type": instance.db_type if instance else None,
        "severity": row.severity,
        "status": "resolved" if row.status in {"resolved", "closed", "recovered"} else "firing",
        "title": row.title,
        "message": row.content,
        "object_type": "observability",
        "object_name": row.host_id,
        "metric_name": row.metric_name,
        "metric_value": row.metric_value,
        "first_seen": row.first_time,
        "last_seen": row.last_time,
        "labels": {
            **(raw.get("labels") or {}),
            "host_id": row.host_id,
            "business_system": (raw.get("binding") or {}).get("business_system"),
            "binding_id": (raw.get("binding") or {}).get("binding_id"),
        },
        "annotations": {
            **(raw.get("annotations") or {}),
            "observability_alert_id": row.id,
            "source_id": row.source_id,
        },
    })
    db.commit()


def _extract_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    import json
    return json.dumps(value, ensure_ascii=False, default=str)


def _to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
