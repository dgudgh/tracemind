from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import (
    ObservabilityCurrentMetric,
    ObservabilityMetricHistory,
    ObservabilityMetricMapping,
)
from app.services.observability import binding_service
from app.services.observability.metric_normalizer import (
    STANDARD_METRICS,
    normalize_metric_name,
    normalize_metric_unit,
    transform_metric_value,
)


def mapping_to_dict(row: ObservabilityMetricMapping) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_metric": row.source_metric,
        "standard_metric": row.standard_metric,
        "metric_unit": row.metric_unit,
        "description": row.description,
        "enabled": row.enabled,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def list_mappings(db: Session, source_type: str | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilityMetricMapping)
    if source_type:
        query = query.filter(ObservabilityMetricMapping.source_type == source_type)
    return [mapping_to_dict(row) for row in query.order_by(ObservabilityMetricMapping.id.desc()).all()]


def create_mapping(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    row = ObservabilityMetricMapping(
        source_type=data.get("source_type") or "custom",
        source_metric=data["source_metric"],
        standard_metric=data["standard_metric"],
        metric_unit=data.get("metric_unit"),
        description=data.get("description"),
        enabled=data.get("enabled", 1),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return mapping_to_dict(row)


def standard_metrics() -> list[dict[str, str]]:
    return [{"standard_metric": item} for item in STANDARD_METRICS]


def metric_to_dict(row: ObservabilityCurrentMetric | ObservabilityMetricHistory) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "instance_id": row.instance_id,
        "host_id": row.host_id,
        "standard_metric": row.standard_metric,
        "metric_value": float(row.metric_value) if row.metric_value is not None else None,
        "metric_unit": row.metric_unit,
        "metric_time": getattr(row, "metric_time", None) or getattr(row, "collect_time", None),
        "collect_time": getattr(row, "collect_time", None),
        "status": getattr(row, "status", None),
    }


def list_current_metrics(db: Session, source_type: str | None = None, standard_metric: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query = db.query(ObservabilityCurrentMetric)
    if source_type:
        query = query.filter(ObservabilityCurrentMetric.source_type == source_type)
    if standard_metric:
        query = query.filter(ObservabilityCurrentMetric.standard_metric == standard_metric)
    rows = query.order_by(ObservabilityCurrentMetric.collect_time.desc(), ObservabilityCurrentMetric.id.desc()).limit(limit).all()
    return [metric_to_dict(row) for row in rows]


def list_metric_history(db: Session, standard_metric: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    query = db.query(ObservabilityMetricHistory)
    if standard_metric:
        query = query.filter(ObservabilityMetricHistory.standard_metric == standard_metric)
    rows = query.order_by(ObservabilityMetricHistory.metric_time.desc(), ObservabilityMetricHistory.id.desc()).limit(limit).all()
    return [metric_to_dict(row) for row in rows]


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _parse_time(value: Any) -> datetime:
    if not value:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        numeric = float(value)
        if numeric > 1000000000:
            return datetime.utcfromtimestamp(numeric)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def ingest_metric(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    mappings = db.query(ObservabilityMetricMapping).filter(
        ObservabilityMetricMapping.source_type.in_([data.get("source_type") or "custom", "builtin"])
    ).all()
    source_metric = data.get("source_metric") or data.get("metric_name") or data.get("standard_metric")
    standard_metric = data.get("standard_metric") or normalize_metric_name(source_metric, mappings)
    metric_unit = normalize_metric_unit(source_metric, standard_metric, mappings) or data.get("metric_unit")
    metric_time = _parse_time(data.get("metric_time") or data.get("collect_time"))
    raw_value = data.get("metric_value") if "metric_value" in data else data.get("value")
    metric_value = _to_decimal(transform_metric_value(source_metric, standard_metric, raw_value))
    source_type = data.get("source_type") or "custom"
    source_id = data.get("source_id")
    instance_id = data.get("instance_id")
    host_id = data.get("host_id")
    if not instance_id:
        resolved = binding_service.resolve_instance(
            db,
            source_type=source_type,
            host_id=str(host_id or ""),
            labels=data.get("labels") if isinstance(data.get("labels"), dict) else None,
            raw_data=data,
        )
        instance_id = resolved.get("instance_id")

    current = db.query(ObservabilityCurrentMetric).filter(
        ObservabilityCurrentMetric.source_type == source_type,
        ObservabilityCurrentMetric.source_id == source_id,
        ObservabilityCurrentMetric.instance_id == instance_id,
        ObservabilityCurrentMetric.host_id == host_id,
        ObservabilityCurrentMetric.standard_metric == standard_metric,
    ).first()
    if not current:
        current = ObservabilityCurrentMetric(
            source_type=source_type,
            source_id=source_id,
            instance_id=instance_id,
            host_id=host_id,
            standard_metric=standard_metric,
        )
        db.add(current)
    current.metric_value = metric_value
    current.metric_unit = metric_unit
    current.collect_time = metric_time
    current.status = data.get("status") or "ok"

    history = ObservabilityMetricHistory(
        source_type=source_type,
        source_id=source_id,
        instance_id=instance_id,
        host_id=host_id,
        standard_metric=standard_metric,
        metric_value=metric_value,
        metric_unit=metric_unit,
        metric_time=metric_time,
    )
    db.add(history)
    db.commit()
    db.refresh(current)
    return metric_to_dict(current)


def ingest_metrics(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        result.append(ingest_metric(db, item))
    return result
