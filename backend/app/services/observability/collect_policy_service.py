from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.observability import ObservabilityCollectLog, ObservabilityCollectPolicy, ObservabilityCollectTask


def policy_to_dict(row: ObservabilityCollectPolicy) -> dict[str, Any]:
    return {
        "id": row.id,
        "policy_name": row.policy_name,
        "source_type": row.source_type,
        "collect_type": row.collect_type,
        "interval_seconds": row.interval_seconds,
        "timeout_seconds": row.timeout_seconds,
        "max_concurrency": row.max_concurrency,
        "enabled": row.enabled,
        "retry_times": row.retry_times,
        "low_peak_only": row.low_peak_only,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def task_to_dict(row: ObservabilityCollectTask) -> dict[str, Any]:
    return {
        "id": row.id,
        "policy_id": row.policy_id,
        "source_id": row.source_id,
        "task_name": row.task_name,
        "collect_type": row.collect_type,
        "task_status": row.task_status,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "duration_ms": row.duration_ms,
        "success_count": row.success_count,
        "fail_count": row.fail_count,
        "error_message": row.error_message,
        "create_time": row.create_time,
    }


def list_policies(db: Session, source_type: str | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilityCollectPolicy)
    if source_type:
        query = query.filter(ObservabilityCollectPolicy.source_type == source_type)
    return [policy_to_dict(row) for row in query.order_by(ObservabilityCollectPolicy.id.desc()).all()]


def get_policy(db: Session, policy_id: int) -> ObservabilityCollectPolicy | None:
    return db.query(ObservabilityCollectPolicy).filter(ObservabilityCollectPolicy.id == policy_id).first()


def upsert_policy(db: Session, data: dict[str, Any], row: ObservabilityCollectPolicy | None = None) -> dict[str, Any]:
    policy = row or ObservabilityCollectPolicy(
        policy_name=data.get("policy_name") or "采集策略",
        source_type=data.get("source_type") or "mesguard",
        collect_type=data.get("collect_type") or "manual",
    )
    if not row:
        db.add(policy)
    for field in [
        "policy_name",
        "source_type",
        "collect_type",
        "interval_seconds",
        "timeout_seconds",
        "max_concurrency",
        "enabled",
        "retry_times",
        "low_peak_only",
    ]:
        if field in data and data[field] is not None:
            setattr(policy, field, data[field])
    db.commit()
    db.refresh(policy)
    return policy_to_dict(policy)


def list_tasks(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(ObservabilityCollectTask).order_by(ObservabilityCollectTask.id.desc()).limit(limit).all()
    return [task_to_dict(row) for row in rows]


def get_task(db: Session, task_id: int) -> dict[str, Any] | None:
    row = db.query(ObservabilityCollectTask).filter(ObservabilityCollectTask.id == task_id).first()
    return task_to_dict(row) if row else None


def list_logs(db: Session, limit: int = 200, source_id: int | None = None, task_id: int | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilityCollectLog)
    if source_id:
        query = query.filter(ObservabilityCollectLog.source_id == source_id)
    if task_id:
        query = query.filter(ObservabilityCollectLog.task_id == task_id)
    rows = query.order_by(ObservabilityCollectLog.create_time.desc(), ObservabilityCollectLog.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "task_id": row.task_id,
            "source_id": row.source_id,
            "log_level": row.log_level,
            "log_type": row.log_type,
            "message": row.message,
            "raw_data": row.raw_data,
            "create_time": row.create_time,
        }
        for row in rows
    ]
