"""Periodic consumer for observability data quality evaluation and retention.

Quality assessment used to be reachable only from the API: the service, its policy model and
its snapshot table all existed, but nothing ever called them on a clock. The platform therefore
had no trend of its own data quality - the page showed whatever the moment of the request
produced (ISS-22).

Retention is planned, not executed, unless a scope explicitly opts in. A scheduler that deletes
metric history by default would be a data-loss machine with a cron expression.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.observability import ObservabilityDataQualityPolicy, ObservabilityDataQualitySnapshot
from app.services.observability import data_quality_service

logger = logging.getLogger("app.tasks.data_quality_scheduler")

RETENTION_CONFIRMATION = "DELETE_EXPIRED_OBSERVABILITY"
MIN_INTERVAL_SECONDS = 60


def _interval(value, default: int) -> int:
    try:
        seconds = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(seconds, MIN_INTERVAL_SECONDS)


def _last_evaluation(db: Session, policy: ObservabilityDataQualityPolicy) -> datetime | None:
    query = db.query(func.max(ObservabilityDataQualitySnapshot.evaluated_at))
    if policy.source_id is not None:
        query = query.filter(ObservabilityDataQualitySnapshot.source_id == policy.source_id)
    elif policy.source_type:
        query = query.filter(ObservabilityDataQualitySnapshot.source_type == policy.source_type)
    return query.scalar()


def _evaluate_due(db: Session, policy: ObservabilityDataQualityPolicy, now: datetime) -> bool:
    last = _last_evaluation(db, policy)
    if last is None:
        return True
    return now - last >= timedelta(seconds=_interval(policy.evaluate_interval_seconds, 900))


def _retention_due(policy: ObservabilityDataQualityPolicy, now: datetime) -> bool:
    if policy.last_retention_at is None:
        return True
    return now - policy.last_retention_at >= timedelta(
        seconds=_interval(policy.retention_interval_seconds, 86400)
    )


def run_due_data_quality_jobs() -> dict:
    """Evaluate every enabled policy whose cadence elapsed, and plan/execute retention."""
    db: Session = SessionLocal()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    results = []
    try:
        # A platform with no policy row is still a platform that should be measuring itself.
        data_quality_service.ensure_default_policy(db)
        policies = (
            db.query(ObservabilityDataQualityPolicy)
            .filter(ObservabilityDataQualityPolicy.enabled == 1)
            .order_by(ObservabilityDataQualityPolicy.id.asc())
            .all()
        )
        for policy in policies:
            entry = {"policy_id": policy.id, "scope_key": policy.scope_key,
                     "evaluated": False, "retention_planned": False, "retention_executed": False}
            try:
                if _evaluate_due(db, policy, now):
                    data_quality_service.evaluate_data_quality(
                        db, source_id=policy.source_id, persist=True
                    )
                    entry["evaluated"] = True
                if _retention_due(policy, now):
                    auto_execute = int(policy.retention_auto_execute or 0) == 1
                    data_quality_service.run_retention(
                        db,
                        source_id=policy.source_id,
                        dry_run=not auto_execute,
                        confirmation=RETENTION_CONFIRMATION if auto_execute else None,
                        now=now,
                    )
                    entry["retention_planned"] = True
                    entry["retention_executed"] = auto_execute
                    policy.last_retention_at = now
                    db.commit()
            except Exception as exc:
                db.rollback()
                entry["error"] = str(exc)
                logger.error(
                    "Data quality due task error | policy_id=%s scope=%s error=%s",
                    policy.id, policy.scope_key, exc,
                )
            results.append(entry)
    finally:
        db.close()
    return {"processed": len(results), "items": results}
