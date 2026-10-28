"""Entry point for interactive inspections (manual API, per-engine pages, report centre).

These callers used to invoke :func:`app.services.inspection.runner.run_unified_inspection`
directly, which bypassed the durable queue entirely: the scheduled path had leases,
cancellation, retries and progress while every operator-triggered run had none of them. The
result was a control plane that only governed half the executions - the half nobody was
watching.

They now go through :func:`jobs.submit`, so an interactive run is indistinguishable from a
scheduled one apart from ``trigger_type``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import jobs, report_builder, result_writer, run_readback
from .jobs import JobError

#: Statuses that produced findings worth building a report from.
PUBLISHED = {"success", "partial"}


def run_through_queue(
    db: Session,
    instance,
    policy=None,
    *,
    trigger_type: str = "manual",
    requested_by=None,
    idempotency_key: str | None = None,
    timeout_seconds: int = 900,
) -> dict:
    """Run an inspection through the queue and return the same payload as the direct path."""
    resolved = policy if policy is not None else jobs.default_policy_for(db, instance, instance.db_type)
    if resolved is None:
        raise JobError("未配置可用于当前实例的巡检模板")
    finished = jobs.submit(
        db,
        instance,
        resolved,
        requested_by=requested_by,
        trigger_type=trigger_type,
        idempotency_key=idempotency_key,
        timeout_seconds=timeout_seconds,
    )
    payload = report_builder.build_report_payload(db, finished.id, instance.id) or {
        "task": None, "modules": [], "findings": [], "report": None,
    }
    # The job row is the outcome of record; a report without it hides why a run came back empty.
    payload["job"] = {
        "id": finished.id,
        "status": finished.status,
        "stage": finished.stage,
        "attempt": finished.attempt,
        "failure_reason": finished.failure_reason,
        "published": finished.status in PUBLISHED,
    }
    # Additive: the same run read back through the shared read model, so a caller does not have
    # to infer the outcome from `job.status`. The keys above are kept unchanged for existing
    # callers and tests.
    payload["run"] = run_readback.build_run_read(db, finished.id, instance_id=instance.id)
    return payload


def run_collecting_rows(
    db: Session,
    instance,
    policy=None,
    *,
    trigger_type: str = "implicit",
    requested_by=None,
    timeout_seconds: int = 900,
) -> tuple[str, list]:
    """Queue an inspection and hand back its persisted rows.

    This is the drop-in replacement for ``runner.run_inspection`` at implicit call sites
    (diagnosis, rule engine, operation AI, legacy engine pages). They only need the rows, so
    the report payload is not built - but the execution is still a recorded job, which is the
    difference between "governed" and "the half nobody was watching".
    """
    resolved = policy if policy is not None else jobs.default_policy_for(db, instance, instance.db_type)
    if resolved is None:
        raise JobError("未配置可用于当前实例的巡检模板")
    finished = jobs.submit(
        db,
        instance,
        resolved,
        requested_by=requested_by,
        trigger_type=trigger_type,
        timeout_seconds=timeout_seconds,
    )
    return finished.id, result_writer.get_task_rows(db, finished.id, instance.id)
