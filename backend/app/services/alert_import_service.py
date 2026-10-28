"""Import cached external alerts into the unified event ledger.

Extracted from the HTTP route so a capability step can call the same implementation without a
Request object, and so the caller can prove success by the event ids this run touched.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.alert_event import DBAlertEvent
from app.models.db_instance import DBInstance
from app.models.external_monitoring import PrometheusAlertCache, ZabbixHostMapping, ZabbixProblemCache
from app.models.observability import ObservabilityAlertEvent
from app.services import alert_lifecycle
from app.services.observability.metric_normalizer import infer_metric_from_text, normalize_metric_name


def _external_metric_name(source_metric: Any, *parts: Any) -> str:
    inferred = infer_metric_from_text(source_metric, *parts)
    if inferred:
        return inferred
    return normalize_metric_name(str(source_metric or ""))


def _instances(db: Session) -> dict[int, DBInstance]:
    return {row.id: row for row in db.query(DBInstance).all()}


def import_external_alerts(db: Session, *, allowed_instance_ids: frozenset[int] | None = None) -> dict:
    instances = _instances(db)
    allowed = allowed_instance_ids
    event_ids: list[int] = []
    imported = 0
    prom_query = db.query(PrometheusAlertCache)
    if allowed is not None:
        prom_query = prom_query.filter(PrometheusAlertCache.db_instance_id.in_(allowed))
    prom_rows = (
        prom_query
        .filter(PrometheusAlertCache.alert_status == "firing")
        .order_by(PrometheusAlertCache.update_time.desc(), PrometheusAlertCache.id.desc())
        .limit(500)
        .all()
    )
    for item in prom_rows:
        instance = instances.get(item.db_instance_id) if item.db_instance_id else None
        labels = item.labels_json if isinstance(item.labels_json, dict) else {}
        annotations = item.annotations_json if isinstance(item.annotations_json, dict) else {}
        message = annotations.get("description") or annotations.get("summary")
        source_metric = labels.get("metric") or labels.get("__name__") or labels.get("alertname") or item.alert_name
        row = alert_lifecycle.upsert_event(db, {
            "event_uid": f"prometheus:{item.id}",
            "source": "prometheus",
            "source_ref": str(item.id),
            "instance_id": item.db_instance_id,
            "db_type": instance.db_type if instance else None,
            "severity": item.severity or "warning",
            "status": "firing",
            "title": item.alert_name or "Prometheus alert",
            "message": message,
            "object_type": "prometheus_alert",
            "object_name": item.instance_name,
            "metric_name": _external_metric_name(source_metric, item.alert_name, message, labels, annotations),
            "labels": labels,
            "annotations": annotations,
        })
        event_ids.append(row.id)
        imported += 1

    host_instance = {
        row.zabbix_hostid: row.db_instance_id
        for row in db.query(ZabbixHostMapping).filter(ZabbixHostMapping.db_instance_id.is_not(None)).all()
        if allowed is None or row.db_instance_id in allowed
    }
    zbx_rows = (
        db.query(ZabbixProblemCache)
        .filter(ZabbixProblemCache.status.in_(["firing", "active", "problem"]))
        .order_by(ZabbixProblemCache.sync_time.desc(), ZabbixProblemCache.id.desc())
        .limit(500)
        .all()
    )
    for item in zbx_rows:
        instance_id = host_instance.get(item.hostid)
        if allowed is not None and instance_id not in allowed:
            continue
        instance = instances.get(instance_id) if instance_id else None
        raw = item.raw_data if isinstance(item.raw_data, dict) else {}
        source_metric = raw.get("item_key") or raw.get("metric_name") or raw.get("key") or raw.get("key_") or item.objectid
        row = alert_lifecycle.upsert_event(db, {
            "event_uid": f"zabbix:{item.eventid or item.id}",
            "source": "zabbix",
            "source_ref": item.eventid or str(item.id),
            "instance_id": instance_id,
            "db_type": instance.db_type if instance else None,
            "severity": item.severity_name or str(item.severity or "warning"),
            "status": "firing",
            "title": item.problem_name or "Zabbix problem",
            "message": item.problem_name,
            "object_type": "zabbix_host",
            "object_name": item.host_name,
            "metric_name": _external_metric_name(source_metric, item.problem_name, raw.get("opdata"), raw.get("name"), raw),
            "labels": {"hostid": item.hostid, "eventid": item.eventid, "severity": item.severity},
            "annotations": raw,
        })
        event_ids.append(row.id)
        imported += 1

    obs_query = db.query(ObservabilityAlertEvent)
    if allowed is not None:
        obs_query = obs_query.filter(ObservabilityAlertEvent.instance_id.in_(allowed))
    obs_rows = (
        obs_query
        .filter(ObservabilityAlertEvent.status.in_(["firing", "active", "problem"]))
        .order_by(ObservabilityAlertEvent.last_time.desc(), ObservabilityAlertEvent.id.desc())
        .limit(500)
        .all()
    )
    for item in obs_rows:
        instance = instances.get(item.instance_id) if item.instance_id else None
        row = alert_lifecycle.upsert_event(db, {
            "event_uid": f"observability:{item.source_type}:{item.alert_key}",
            "source": item.source_type,
            "source_ref": str(item.id),
            "instance_id": item.instance_id,
            "db_type": instance.db_type if instance else None,
            "severity": item.severity or "warning",
            "status": "firing",
            "title": item.title or "Observability alert",
            "message": item.content,
            "object_type": "observability",
            "object_name": item.host_id,
            "metric_name": item.metric_name,
            "metric_value": item.metric_value,
            "labels": (item.raw_data or {}).get("labels") if isinstance(item.raw_data, dict) else None,
            "annotations": {
                "source_id": item.source_id,
                "observability_alert_id": item.id,
                "raw_data": item.raw_data,
            },
        })
        event_ids.append(row.id)
        imported += 1

    db.commit()
    return {"imported": imported, "prometheus": len(prom_rows), "zabbix": len(zbx_rows),
            "observability": len(obs_rows), "event_ids": sorted(event_ids)}
