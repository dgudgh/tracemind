"""Read one stored run: the same numbers whether you ask the list, the detail, the report or
the evidence endpoint.

The defect this module removes is not "a missing endpoint" but "two answers to one question".
The list was aggregated from ``DBCheckResult`` while the detail was built from the job's
report payload, so a run with zero result rows existed in one view and not the other - and a
failed collection could be shown as a healthy report that simply had no rows.

Three rules:

1. **The job row is the identity.** ``DBInspectionJob.id == DBCheckResult.task_id``; there is no
   second run id.
2. **Counts and health are borrowed, never recomputed.** ``result_writer.execution_summary`` and
   ``result_writer.severity_counts`` remain the only places that decide what a row means.
3. **Zero rows is a state, not an absence.** ``evidence.state == "missing"`` says "this run
   produced no evidence", which is different from "there is no such run" (404).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.report_center import ReportArtifact
from app.services import run_status
from app.services.inspection import jobs, report_builder, result_writer

SCHEMA = "run_read/v1"
REPORT_SOURCE_PREFIX = "source:inspection:"
#: Content fields compared to answer "was the template edited after the freeze?". Volatile
#: fields (id/update_time/enabled) are deliberately absent: comparing them would report a change
#: on every run, which trains operators to ignore the warning.
_TEMPLATE_CONTENT_FIELDS = (
    "policy_key", "name", "db_types_json", "categories_json", "item_config_json",
    "threshold_config_json", "action_config_json", "report_config_json",
)


def _stamp(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return None


def _frozen_target(row: DBInspectionJob) -> dict:
    snapshot = (row.policy_snapshot_json or {}).get(jobs.TARGET_SNAPSHOT_KEY)
    return result_writer.normalize_target_snapshot(
        snapshot, instance_id=row.instance_id, db_type=row.db_type
    )


def _step_status(stage: str) -> str:
    if stage in run_status.TERMINAL_STATUSES:
        return stage
    return "completed" if stage == "completed" else "in_progress"


def steps_from_progress(row: DBInspectionJob) -> list[dict]:
    """Turn the stored event stream into steps. No events means one ``queued`` step.

    Fabricating intermediate steps for a run that recorded none would make the timeline look
    more informative than the evidence allows.
    """
    events = [
        event for event in (row.progress_json or [])
        if isinstance(event, dict) and event.get("stage")
    ]
    if not events:
        return [{
            "index": 0,
            "stage": "queued",
            "time": _stamp(row.created_at),
            "status": "queued",
            "detail": None,
            "failure_code": None,
        }]
    steps = []
    for index, event in enumerate(events):
        stage = str(event.get("stage"))
        detail = event.get("detail")
        if detail is None and event.get("result_count") is not None:
            detail = f"已入库 {event['result_count']} 条巡检结果"
        steps.append({
            "index": index,
            "stage": stage,
            "time": event.get("time"),
            "status": _step_status(stage),
            "detail": detail,
            "failure_code": event.get("failure_code"),
        })
    return steps


def plan_snapshot_block(row: DBInspectionJob) -> dict:
    """The plan exactly as it was frozen when the job was queued.

    Reads the stored snapshot only - it never loads the live template, because what *ran* is the
    snapshot, and a template edited afterwards must not rewrite history.
    """
    snapshot = {
        key: value for key, value in (row.policy_snapshot_json or {}).items()
        if key != jobs.TARGET_SNAPSHOT_KEY
    }
    item_config = snapshot.get("item_config_json") or []
    return {
        "policy_id": row.policy_id,
        "policy_key": snapshot.get("policy_key"),
        "policy_name": snapshot.get("name"),
        "policy_revision": row.policy_revision,
        "plan_source": "frozen_policy_snapshot",
        "plan_contract": "v0_snapshot",
        "db_types": snapshot.get("db_types_json") or [],
        "categories": snapshot.get("categories_json") or [],
        "item_count": len(item_config) if isinstance(item_config, list) else None,
        "threshold_config": snapshot.get("threshold_config_json") or {},
        "frozen_at": snapshot.get("update_time"),
        "timeout_seconds": row.timeout_seconds,
    }


def _template_changed_since_freeze(db: Session, row: DBInspectionJob) -> bool | None:
    """Advisory only: a template edited after the freeze does not change what ran."""
    policy = db.get(DBInspectionPolicy, row.policy_id)
    if policy is None:
        return None  # deleted is not "changed"; the run itself is unaffected
    snapshot = row.policy_snapshot_json or {}
    for field in _TEMPLATE_CONTENT_FIELDS:
        if snapshot.get(field) != getattr(policy, field, None):
            return True
    return False


def evidence_block(rows: list, *, instance_id: int, db_type: str) -> dict:
    """What the run recorded about its own target, and whether that is still provable."""
    if not rows:
        return {
            "state": "missing",
            "target": None,
            "row_count": 0,
            "reason": "该运行没有产生结果行，不能据此判断目标端采集情况",
        }
    targets = [
        result_writer.normalize_target_snapshot(
            (row.raw_data or {}).get(jobs.TARGET_SNAPSHOT_KEY)
            if isinstance(row.raw_data, dict) else None,
            instance_id=instance_id, db_type=db_type,
        )
        for row in rows
    ]
    recorded = next((item for item in targets if item["status"] == "recorded"), None)
    return {
        "state": "recorded" if recorded else "historical_context_missing",
        "target": recorded or targets[0],
        "row_count": len(rows),
        "recorded_rows": sum(1 for item in targets if item["status"] == "recorded"),
    }


def reports_block(db: Session, job_id: str) -> dict:
    """Report index for this run. ``not_synced`` is a normal state, not an error."""
    source_key = f"{REPORT_SOURCE_PREFIX}{job_id}"
    artifact = db.query(ReportArtifact).filter(ReportArtifact.source_key == source_key).first()
    if artifact is None:
        return {"state": "not_synced", "source_key": source_key, "artifacts": []}
    return {
        "state": "synced",
        "source_key": source_key,
        "artifacts": [{
            "id": artifact.id,
            "artifact_uid": artifact.artifact_uid,
            "status": artifact.status,
            "create_time": _stamp(artifact.create_time),
        }],
    }


def _notice(row: DBInspectionJob, outcome: dict) -> str | None:
    if row.cancel_requested and row.status == "running":
        return "取消已受理，采集可能仍在进行；结果尚未发布，结束后会如实标记为已取消"
    if outcome["outcome_kind"] == "cancelled_force_closed" and row.status == "interrupted":
        # The executor died *and* a cancel was pending when it did. Both belong in the notice:
        # saying only "cancel was force-closed" would hide that the node is gone, and saying
        # only "heartbeat lost" would hide that the operator's cancel was never confirmed.
        return (
            "执行节点心跳中断，且取消未获执行端确认；平台已按取消强制终结该运行，"
            "执行端是否仍在运行未知，结果未发布，请人工核对目标端"
        )
    if outcome["outcome_kind"] == "cancelled_force_closed":
        return "取消未获执行端确认，平台已强制终结该运行；执行端可能仍在运行，请人工核对目标端"
    if outcome["outcome_kind"] == "cancelled_during_run":
        return "任务在采集过程中被取消；采集可能仍在进行，结果未发布"
    if row.status == "interrupted":
        return "执行节点心跳中断，无法确认目标端采集情况；请核对后重试"
    if row.status == "timed_out":
        return "任务超过总超时，结果未发布；目标端实际执行情况未知，请核对后重试"
    return None


#: Fields only a list item carries. Everything else in an item comes from :func:`jobs.job_view`,
#: so the two read models cannot describe one column with two names or two types.
LIST_ONLY_FIELDS = frozenset(
    {"instance_name", "result_count", "counts", "score", "risk_level"}
)


def run_summary(
    db: Session,
    row: DBInspectionJob,
    *,
    result_counts: dict[str, int],
    severity_counts: dict[str, dict[str, int]] | None = None,
) -> dict:
    """One list item: the detail's run block plus counts and the report-facing score.

    Built from :func:`jobs.job_view` on purpose. Hand-writing the fields here is how the two
    shapes drifted apart in the first place (``job_status`` here, ``status`` there).
    """
    row_count = int(result_counts.get(row.id, 0))
    counts = None if severity_counts is None else severity_counts.get(row.id)
    if counts is None:
        counts = result_writer.severity_counts_by_task_ids(db, [row.id]).get(
            row.id, {"critical": 0, "warning": 0, "info": 0, "normal": 0}
        )
    item = jobs.job_view(row, result_count=row_count, include_progress=False)
    score = result_writer.score_for_counts(
        counts, task_status=item["outcome"], result_count=row_count,
    )
    item.update({
        "instance_name": _frozen_target(row)["name"],
        "result_count": row_count,
        "counts": counts,
        "score": score,
        # No evidence, no verdict: a zero-row run must not read as "normal".
        "risk_level": result_writer.risk_level_for(counts, score, result_count=row_count),
    })
    return item


def build_run_read(db: Session, job_id: str, *, instance_id: int | None = None) -> dict | None:
    """Full envelope for one run, or ``None`` when there is no such job.

    ``None`` means "no such run". A run that produced nothing returns a full envelope with
    ``results.total_count == 0`` and ``evidence.state == "missing"`` - collapsing those two into
    the same 404 is precisely how a failed inspection used to become invisible.
    """
    row = db.get(DBInspectionJob, job_id)
    if row is None:
        return None
    if instance_id is not None and row.instance_id != instance_id:
        return None
    rows = result_writer.get_task_rows(db, job_id, row.instance_id)
    counts = result_writer.severity_counts(rows)
    outcome = jobs.outcome_block(row, result_count=len(rows))
    run = jobs.job_view(row, result_count=len(rows))
    if rows:
        execution = result_writer.execution_summary(rows)
        results = {
            "items": [result_writer.row_to_dict(item) for item in rows],
            "total_count": len(rows),
            "counts": counts,
            "task_status": execution["task_status"],
            "failure_reason": execution["failure_reason"],
            "derived_from": "result_writer.execution_summary",
        }
    else:
        # ``execution_summary([])`` says "failed - no results returned". For a queued or
        # cancelled-before-run job that is a fabricated verdict, so no verdict is given.
        results = {
            "items": [],
            "total_count": 0,
            "counts": counts,
            "task_status": None,
            "failure_reason": None,
            "derived_from": None,
            "reason": "该运行没有产生结果行",
        }
    plan = plan_snapshot_block(row)
    plan["template_changed_since_freeze"] = _template_changed_since_freeze(db, row)
    return {
        "schema": SCHEMA,
        "run": run,
        "plan": plan,
        "steps": steps_from_progress(row),
        "results": results,
        "evidence": evidence_block(rows, instance_id=row.instance_id, db_type=row.db_type),
        "reports": reports_block(db, job_id),
        "report": report_builder.build_report_payload(db, job_id, row.instance_id) if rows else None,
        "notice": _notice(row, outcome),
    }


def legacy_summary(item: dict) -> dict:
    """A result row set from before the queue existed. It has no job row to read."""
    status = run_status.normalize(item.get("task_status"))
    counts = {
        "critical": item.get("critical_count", 0),
        "warning": item.get("warning_count", 0),
        "info": item.get("info_count", 0),
        "normal": item.get("normal_count", 0),
    }
    total = int(item.get("total_count") or 0)
    return {
        "job_id": item.get("task_id"),
        "instance_id": item.get("instance_id"),
        "instance_name": item.get("instance_name"),
        "db_type": item.get("db_type"),
        # A legacy row set *is* its own task id: there is no separate run row.
        "task_id": item.get("task_id"),
        "trigger_type": None,
        "attempt": None,
        "parent_job_id": None,
        "policy_key": None,
        "policy_name": None,
        "policy_revision": None,
        "status": None,
        "stage": None,
        "failure_reason": None,
        "timeout_seconds": None,
        "terminal": True,
        "outcome": status["outcome"],
        "outcome_kind": status["outcome_kind"],
        "outcome_reason": "该运行产生于队列接入之前，没有任务记录",
        "requires_attention": False,
        "percent": None,
        "percent_basis": "terminal_no_percentage",
        "results_published": total > 0,
        "result_count": total,
        "counts": counts,
        "score": item.get("overall_score"),
        "risk_level": item.get("risk_level"),
        "can_retry": False,
        "cancel_requested": False,
        "cancel_requested_at": None,
        "cancel_requested_by": None,
        "cancel_attribution": None,
        "created_at": item.get("create_time"),
        "started_at": None,
        "finished_at": None,
        "linkage": "legacy_result_only",
    }


def run_sources(db: Session, limit: int = 100) -> list[dict]:
    """Runs for the unified report catalogue - jobs first, then pre-queue result sets.

    Reading this from ``result_writer.list_tasks`` alone is why a failed or cancelled run never
    appeared in the report catalogue: it has no rows to aggregate from.
    """
    limit = max(int(limit), 1)
    rows = (
        db.query(DBInspectionJob)
        .order_by(DBInspectionJob.created_at.desc(), DBInspectionJob.id.desc())
        .limit(limit)
        .all()
    )
    job_ids = [row.id for row in rows]
    result_counts = result_writer.count_by_task_ids(db, job_ids)
    severity = result_writer.severity_counts_by_task_ids(db, job_ids)
    items = [
        run_summary(db, row, result_counts=result_counts, severity_counts=severity)
        for row in rows
    ]
    remaining = limit - len(items)
    if remaining <= 0:
        return items
    known = set(job_ids)
    for legacy in result_writer.list_tasks(db, limit=remaining + len(known)):
        if len(items) >= limit:
            break
        if legacy.get("task_id") in known:
            continue
        items.append(legacy_summary(legacy))
    return items
