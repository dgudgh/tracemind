"""Due-task runner for observability collectors."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.observability import ObservabilityCollectPolicy, ObservabilityCollectTask, ObservabilitySource
from app.services.observability.collector_runner import run_collect_task

logger = logging.getLogger("app.tasks.observability_scheduler")

DEFAULT_LIMITS = {
    "zabbix_hosts": 500,
    "zabbix_problems": 200,
    "zabbix_metrics": 300,
}

TRANSPORT_FAILURE_MARKERS = (
    "connection refused",
    "connection reset",
    "network is unreachable",
    "no route to host",
    "name or service not known",
    "temporary failure in name resolution",
    "timed out",
    "timeout",
    "urlopen error",
)


def run_due_observability_jobs() -> None:
    db = SessionLocal()
    unavailable_sources: dict[int, str] = {}
    try:
        policies = (
            db.query(ObservabilityCollectPolicy)
            .filter(ObservabilityCollectPolicy.enabled == 1)
            .order_by(ObservabilityCollectPolicy.id.asc())
            .all()
        )
        for policy in policies:
            sources = (
                db.query(ObservabilitySource)
                .filter(ObservabilitySource.enabled == 1)
                .filter(ObservabilitySource.source_type == policy.source_type)
                .order_by(ObservabilitySource.id.asc())
                .all()
            )
            for source in sources:
                try:
                    if source.id in unavailable_sources:
                        logger.warning(
                            "Observability source skipped after transport failure | "
                            "policy_id=%s source_id=%s collect_type=%s error=%s",
                            policy.id,
                            source.id,
                            policy.collect_type,
                            unavailable_sources[source.id],
                        )
                        continue
                    if not _is_due(db, policy, source):
                        continue
                    result = run_collect_task(db, _task_payload(policy, source))
                    if result.get("task_status") != "success":
                        error_message = str(result.get("error_message") or "")
                        logger.warning(
                            "Observability collect task failed | policy_id=%s source_id=%s collect_type=%s error=%s",
                            policy.id,
                            source.id,
                            policy.collect_type,
                            error_message,
                        )
                        if _is_transport_failure(error_message):
                            unavailable_sources[source.id] = error_message
                except Exception as exc:
                    db.rollback()
                    logger.error(
                        "Observability due task error | policy_id=%s source_id=%s collect_type=%s error=%s",
                        policy.id,
                        source.id,
                        policy.collect_type,
                        exc,
                    )
    finally:
        db.close()


def _is_transport_failure(error_message: str | None) -> bool:
    normalized = str(error_message or "").strip().lower()
    return bool(normalized) and any(marker in normalized for marker in TRANSPORT_FAILURE_MARKERS)


def _is_due(db: Session, policy: ObservabilityCollectPolicy, source: ObservabilitySource) -> bool:
    now = datetime.utcnow()
    last_task = (
        db.query(ObservabilityCollectTask)
        .filter(ObservabilityCollectTask.policy_id == policy.id)
        .filter(ObservabilityCollectTask.source_id == source.id)
        .filter(ObservabilityCollectTask.collect_type == policy.collect_type)
        .order_by(ObservabilityCollectTask.id.desc())
        .first()
    )
    if not last_task:
        return True

    last_time = last_task.start_time or last_task.create_time
    if not last_time:
        return True

    interval = max(int(policy.interval_seconds or source.collect_interval_seconds or 60), 30)
    if last_task.task_status == "running" and now - last_time < timedelta(seconds=max(interval * 2, 300)):
        return False
    return now - last_time >= timedelta(seconds=interval)


def _task_payload(policy: ObservabilityCollectPolicy, source: ObservabilitySource) -> dict:
    payload = {
        "policy_id": policy.id,
        "source_id": source.id,
        "task_name": f"{policy.policy_name} - {source.source_name}",
        "collect_type": policy.collect_type,
    }
    if policy.collect_type in DEFAULT_LIMITS:
        payload["limit"] = DEFAULT_LIMITS[policy.collect_type]
    return payload
