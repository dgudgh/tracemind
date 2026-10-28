"""Durable inspection control plane. Collectors never own the result commit."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.database_options import redact_database_error
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.services import run_status
from app.services.inspection import result_writer, runner, template_runtime

TERMINAL = {"success", "partial", "failed", "cancelled", "timed_out", "interrupted", "expired"}
RETRYABLE = {"partial", "failed", "cancelled", "timed_out", "interrupted", "expired"}
LEASE_SECONDS = 45
MAX_ATTEMPTS = 3
#: How long an acknowledged cancel may sit unanswered before the platform terminates the run
#: itself. Without a deadline, an executor that keeps heartbeating but ignores the request makes
#: "取消中" a terminal state nobody can leave: `retry` needs a finished parent, `reap_interrupted`
#: needs an expired lease, and `timeout_seconds` was only ever checked inside `commit_results`.
CANCEL_GRACE_SECONDS = 60
TARGET_SNAPSHOT_KEY = "inspection_target_snapshot"
#: Stages that only appear once a run has reached a verdict.
TERMINAL_STAGES = {"success", "partial", "failed", "cancelled", "timed_out", "interrupted", "expired", "completed"}


class JobError(ValueError):
    """A refusal that the caller is expected to turn into 4xx, with a machine-readable code.

    ``code`` exists because the message is Chinese prose that will be reworded. Classifying a
    failure by matching on that prose would silently degrade the day somebody edits a string.
    """

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code

    @property
    def classification(self) -> str | None:
        """``code`` if it is a registered classification, else ``None``."""
        return self.code if self.code in run_status.FAILURE_CODES else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


def target_fingerprint(instance) -> str:
    # Credentials are resolved afresh, but changing a saved endpoint must not retarget a queued task.
    fields = (
        "id", "db_type", "host", "port", "database_name", "service_name", "sid",
        "oracle_connect_type", "username", "ssh_host", "ssh_port", "ssh_username",
    )
    return _digest({key: getattr(instance, key, None) for key in fields})


def freeze_policy(policy) -> dict:
    fields = (
        "id", "policy_key", "name", "db_types_json", "categories_json", "item_config_json",
        "threshold_config_json", "action_config_json", "report_config_json", "update_time",
        "enabled",
    )
    return json.loads(json.dumps({key: getattr(policy, key, None) for key in fields}, default=str))


def frozen_policy(row: DBInspectionJob):
    """The template exactly as it stood when the job was queued.

    Executing the live row instead would run whatever SQL and thresholds the template says
    *now*, under the revision the operator actually reviewed. The stored snapshot is the only
    source that can prove what ran.
    """
    snapshot = {
        key: value for key, value in (row.policy_snapshot_json or {}).items()
        if key != TARGET_SNAPSHOT_KEY
    }
    if not snapshot:
        raise JobError("巡检任务缺少模板快照，不能执行")
    fields = (
        "id", "policy_key", "name", "db_types_json", "categories_json", "item_config_json",
        "threshold_config_json", "action_config_json", "report_config_json", "update_time",
    )
    values = {key: snapshot.get(key) for key in fields}
    # Jobs queued before `enabled` was captured provably ran while the template was enabled:
    # `enqueue` refuses anything else. The live row is still checked in validate_job_target.
    values["enabled"] = snapshot.get("enabled", 1)
    return SimpleNamespace(**values)


def _event(stage: str, now: datetime, **extra) -> dict:
    return {"stage": stage, "time": now.isoformat() + "Z", **extra}


def _stamp(value):
    return value.isoformat() + "Z" if value else None


def last_terminal_event(row: DBInspectionJob) -> dict | None:
    """The event that recorded the verdict, or ``None`` for a job that has not reached one."""
    for event in reversed(row.progress_json or []):
        if isinstance(event, dict) and event.get("stage") in TERMINAL_STAGES:
            return event
    return None


def failure_code(row: DBInspectionJob) -> str | None:
    """The producer's explicit classification, never parsed out of the message."""
    event = last_terminal_event(row)
    return (event or {}).get("failure_code")


def cancel_attribution(row: DBInspectionJob) -> str | None:
    """Whether "who asked to cancel" is recorded, unknown, or not applicable.

    Jobs cancelled before the control-plane migration have ``cancel_requested=1`` and no stamp.
    Reporting the request without claiming to know when is the honest representation.
    """
    if not row.cancel_requested:
        return None
    return "recorded" if row.cancel_requested_at is not None else "unknown_pre_upgrade"


def outcome_block(row: DBInspectionJob, *, result_count: int | None = None) -> dict:
    """Derived outcome for one job row. Pure: no queries, no writes, no caching."""
    events = row.progress_json or []
    derived = run_status.normalize(
        row.status,
        failure_code=failure_code(row),
        stage=row.stage,
        started=row.started_at is not None,
        event_count=len(events),
    )
    reason = row.failure_reason
    if reason is not None:
        reason = redact_database_error(str(reason))[:1500]
    derived["outcome_reason"] = reason
    derived["results_published"] = (
        run_status.results_published(row.stage, result_count) if result_count is not None else None
    )
    return derived


#: Keys of :func:`outcome_block` that describe the derived verdict rather than repeating the
#: stored lifecycle columns.
_DERIVED_FIELDS = (
    "terminal", "outcome", "outcome_kind", "outcome_reason",
    "requires_attention", "percent", "percent_basis", "results_published",
)


def job_view(
    row: DBInspectionJob,
    *,
    result_count: int | None = None,
    include_progress: bool = True,
) -> dict:
    """One run, flat: the stored lifecycle columns plus the derived verdict.

    The derived verdict is **flattened** into this block rather than nested under ``outcome``,
    so a list item and this detail block describe the same fact with the same key and the same
    type. A nested ``outcome`` object in one place and an ``outcome`` string in the other is the
    two-shapes-for-one-fact problem this module exists to remove.
    """
    derived = outcome_block(row, result_count=result_count)
    view = {
        "job_id": row.id, "instance_id": row.instance_id, "db_type": row.db_type,
        "status": row.status, "stage": row.stage, "attempt": row.attempt,
        "parent_job_id": row.parent_job_id, "trigger_type": row.trigger_type,
        "policy_key": row.policy_snapshot_json.get("policy_key"),
        "policy_name": row.policy_snapshot_json.get("name"), "policy_revision": row.policy_revision,
        "failure_reason": row.failure_reason,
        "timeout_seconds": row.timeout_seconds, "cancel_requested": bool(row.cancel_requested),
        "cancel_requested_at": _stamp(row.cancel_requested_at),
        "cancel_requested_by": row.cancel_requested_by,
        "cancel_attribution": cancel_attribution(row),
        "created_at": _stamp(row.created_at), "started_at": _stamp(row.started_at),
        "finished_at": _stamp(row.finished_at),
        "task_id": row.id if row.status in {"success", "partial", "failed"} and row.stage == "completed" else None,
        "can_retry": row.status in RETRYABLE and row.attempt < MAX_ATTEMPTS,
        "linkage": "job",
        **{key: derived[key] for key in _DERIVED_FIELDS},
    }
    if include_progress:
        # The event stream is the detail's step timeline; a 50-item list does not need it.
        view["progress"] = row.progress_json or []
    return view


def validate_actor(db, row: DBInspectionJob) -> None:
    if row.requested_by is None:
        return  # Only the internal scheduler or authenticated local-admin API can omit an actor.
    from app.models.user import User
    from app.services.access_control import is_action_allowed, resolve_scope

    user = db.get(User, row.requested_by)
    if user is None or user.status != "active" or (user.locked_until and user.locked_until > datetime.now()):
        raise JobError("巡检申请账号已停用或失效", code="actor_inactive")
    if user.role != "admin":
        if not is_action_allowed(db, user.id, "inspection.execute")[0]:
            raise JobError("巡检执行权限已撤销", code="permission_revoked")
        scope = resolve_scope(db, user.id, minimum_access="operate")
        if scope.restricted and row.instance_id not in scope.native_instance_ids:
            raise JobError("巡检实例已不在授权范围内", code="scope_revoked")


def validate_job_target(db, row: DBInspectionJob):
    validate_actor(db, row)
    instance = db.get(DBInstance, row.instance_id)
    if instance is None or not instance.enabled:
        raise JobError("巡检实例不存在或已停用", code="instance_disabled")
    if target_fingerprint(instance) != row.target_fingerprint:
        raise JobError("实例连接目标已变更，请重新提交巡检", code="target_changed")
    policy = db.get(DBInspectionPolicy, row.policy_id)
    if policy is None or not policy.enabled:
        raise JobError("任务使用的巡检模板已删除或停用", code="template_missing")
    if _digest(row.policy_snapshot_json) != row.policy_revision:
        raise JobError("巡检模板快照校验失败", code="snapshot_invalid")
    return instance


def enqueue(db, instance, db_type: str, policy, *, requested_by=None, trigger_type="manual",
            timeout_seconds=900, idempotency_key=None, parent_job=None) -> DBInspectionJob:
    if not instance.enabled or db_type not in runner.INSPECTION_DB_TYPES:
        raise JobError("实例未启用或不支持该巡检类型")
    if db_type != "host" and db_type != instance.db_type:
        raise JobError("实例与巡检数据库类型不匹配")
    if policy is None or not policy.enabled or db_type not in (policy.db_types_json or []):
        raise JobError("请选择已启用且适用于当前实例的巡检模板")
    if not 30 <= timeout_seconds <= 3600:
        raise JobError("任务超时必须在 30 到 3600 秒之间")
    if parent_job is None:
        snapshot = freeze_policy(policy)
        snapshot[TARGET_SNAPSHOT_KEY] = {
            **result_writer.target_snapshot(instance), "db_type": db_type,
        }
    else:
        if _digest(parent_job.policy_snapshot_json) != parent_job.policy_revision:
            raise JobError("原任务快照校验失败，不能重试")
        snapshot = deepcopy(parent_job.policy_snapshot_json)
    template_runtime.require_template_runtime(snapshot["db_types_json"], snapshot["item_config_json"] or [])
    revision = _digest(snapshot)
    target = target_fingerprint(instance)
    # Display-only renames must not replace a frozen target on idempotent resubmission.
    def policy_digest(value):
        return _digest({key: item for key, item in value.items() if key != TARGET_SNAPSHOT_KEY})

    request_identity = (instance.id, db_type, policy_digest(snapshot), timeout_seconds, target)

    def same_request(existing):
        return _digest(existing.policy_snapshot_json) == existing.policy_revision and (
            existing.instance_id, existing.db_type, policy_digest(existing.policy_snapshot_json),
            existing.timeout_seconds, existing.target_fingerprint,
        ) == request_identity

    key = _digest([requested_by, trigger_type, idempotency_key]) if idempotency_key else None
    if key:
        old = db.query(DBInspectionJob).filter_by(idempotency_key=key).first()
        if old:
            if not same_request(old):
                raise JobError("幂等键已用于不同的巡检参数")
            return old
    if parent_job is not None:
        if parent_job.status not in RETRYABLE or parent_job.attempt >= MAX_ATTEMPTS:
            raise JobError("该任务不允许重试或已达到三次尝试上限")
        if parent_job.target_fingerprint != target:
            raise JobError("原任务目标已变更，不能重试")
        previous = db.query(DBInspectionJob).filter_by(parent_job_id=parent_job.id).first()
        if previous:
            return previous
    now = utcnow()
    row = DBInspectionJob(
        id=uuid.uuid4().hex[:30], instance_id=instance.id, db_type=db_type,
        requested_by=requested_by, trigger_type=trigger_type, policy_id=policy.id,
        policy_snapshot_json=snapshot, policy_revision=revision, target_fingerprint=target,
        idempotency_key=key, parent_job_id=parent_job.id if parent_job else None,
        attempt=parent_job.attempt + 1 if parent_job else 1, status="queued", stage="queued",
        progress_json=[_event("queued", now)], timeout_seconds=timeout_seconds,
        cancel_requested=0, created_at=now,
    )
    validate_actor(db, row)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # The unique keys decide duplicate submissions even across different API workers.
        existing = db.query(DBInspectionJob).filter_by(parent_job_id=parent_job.id).first() if parent_job else (
            db.query(DBInspectionJob).filter_by(idempotency_key=key).first() if key else None
        )
        if existing is None:
            raise
        if not same_request(existing):
            raise JobError("幂等键已用于不同的巡检参数")
        return existing
    db.refresh(row)
    return row


CANCEL_BEFORE_RUN_REASON = "任务在执行前被取消"
CANCEL_DURING_RUN_REASON = "任务在采集过程中被取消；采集可能仍在进行，结果未发布"


def cancel(db, row: DBInspectionJob, *, requested_by=None, now=None) -> DBInspectionJob:
    """Request cancellation.

    Two different events hide behind one word:

    * a ``queued`` job has not touched the target, so it can be cancelled outright;
    * a ``running`` job may be mid-query on a production database. Recording "cancelled" there
      would tell the operator the target is safe, so only the *request* is stored: the job keeps
      running until the executor acknowledges it through :func:`finish_cancelled` or
      :func:`commit_results` refuses to publish.

    Raises:
        JobError: if the job already reached a terminal state (``code="not_cancellable"``).
    """
    now = now or utcnow()
    if row.status not in {"queued", "running"}:
        raise JobError("任务已结束，不能取消", code="not_cancellable")
    if row.status == "queued":
        progress = [
            *(row.progress_json or []),
            _event("cancelled", now, detail=CANCEL_BEFORE_RUN_REASON,
                   failure_code="cancelled_before_run"),
        ][-30:]
        db.query(DBInspectionJob).filter_by(id=row.id, status="queued").update({
            "status": "cancelled", "stage": "cancelled", "cancel_requested": 1,
            "cancel_requested_at": now, "cancel_requested_by": requested_by,
            "finished_at": now, "failure_reason": CANCEL_BEFORE_RUN_REASON,
            "progress_json": progress,
        }, synchronize_session=False)
    else:
        db.query(DBInspectionJob).filter_by(id=row.id, status="running").update({
            "cancel_requested": 1, "cancel_requested_at": now,
            "cancel_requested_by": requested_by,
        }, synchronize_session=False)
    db.commit()
    db.refresh(row)
    return row


def finish_cancelled(db, job_id: str, token: str, kind: str = "cancelled_during_run", *, execution_guard=None) -> bool:
    """End a running job because its owner acknowledged the cancel request.

    Only a job that is still running, still owned by ``token`` and actually flagged as
    cancel-requested can reach this path. Anything else returns ``False`` and is left alone:
    a late acknowledgement must never rewrite a run that already finished.
    """
    if kind not in {"cancelled_before_run", "cancelled_during_run"}:
        raise JobError("Invalid cancellation kind")
    check_execution_guard(db, execution_guard)
    now = utcnow()
    row = db.get(DBInspectionJob, job_id)
    if row is None:
        return False
    reason = CANCEL_DURING_RUN_REASON if kind == "cancelled_during_run" else CANCEL_BEFORE_RUN_REASON
    progress = [*(row.progress_json or []), _event("cancelled", now, detail=reason, failure_code=kind)][-30:]
    count = db.query(DBInspectionJob).filter(
        DBInspectionJob.id == job_id, DBInspectionJob.status == "running",
        DBInspectionJob.owner_token == token, DBInspectionJob.cancel_requested == 1,
    ).update({
        "status": "cancelled", "stage": "cancelled", "finished_at": now,
        "owner_token": None, "lease_until": None, "failure_reason": reason,
        "progress_json": progress,
    }, synchronize_session=False)
    db.commit()
    return bool(count)


#: Stored explanation for the plain case: the executor stopped heartbeating, no cancel pending.
LEASE_LOST_REASON = "执行节点心跳中断；没有自动重做采集，请确认后重试"

#: Same event, but a cancel was already pending when the lease died. Both facts have to
#: survive in the wording: the node is gone *and* the operator's cancel was never confirmed.
#: Writing only "heartbeat lost" would silently swallow the cancel.
INTERRUPTED_WITH_CANCEL_REASON = (
    "执行节点心跳中断，且此前已请求取消；取消未获执行端确认，平台按取消强制终结处理，"
    "执行端是否仍在运行未知，结果不发布，请人工核对目标端"
)


def _timeout_seconds(value) -> float | None:
    """A job's own deadline in seconds, or ``None`` when it has no usable deadline.

    ``timeout_seconds`` is nullable, and rows that predate the column (or arrived through an
    import) can carry ``0``. Feeding either into ``float(row.timeout_seconds or 0)`` made
    ``now - started >= 0`` unconditionally true, so the very next scheduler tick terminated a
    run that never had a deadline at all - a job killed by arithmetic. ``None`` and ``0`` both
    mean "no deadline"; anything negative is nonsense and is treated the same way.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def reap_interrupted(db, now=None) -> int:
    now = now or utcnow()
    rows = db.query(DBInspectionJob).filter(
        DBInspectionJob.status == "running",
        or_(DBInspectionJob.lease_until < now, DBInspectionJob.lease_until.is_(None)),
    ).all()
    count = 0
    for row in rows:
        # A cancel that was still pending when the lease died must not vanish here. The
        # scheduler runs recovery *before* overdue, so without this branch the row would be
        # closed as a plain "heartbeat lost" and `reap_overdue` would then find zero running
        # rows: the operator's cancel would leave no trace, and they could not tell whether it
        # had taken effect.
        #
        # The status deliberately stays "interrupted" - the executor died, that is the stored
        # fact - while the failure code says the cancel was force-closed. The pair carries both
        # truths, and neither is allowed to overwrite the other: this is not "cancel succeeded",
        # it is "cancel could not be confirmed because the node is gone".
        cancel_pending = bool(row.cancel_requested)
        reason = INTERRUPTED_WITH_CANCEL_REASON if cancel_pending else LEASE_LOST_REASON
        code = "cancelled_force_closed" if cancel_pending else "lease_lost"
        progress = [
            *(row.progress_json or []),
            _event("interrupted", now, detail=reason, failure_code=code),
        ][-30:]
        count += db.query(DBInspectionJob).filter_by(
            id=row.id, status="running",
        ).update({
            "status": "interrupted", "stage": "interrupted", "finished_at": now,
            "failure_reason": reason, "owner_token": None, "lease_until": None,
            "progress_json": progress,
        }, synchronize_session=False)
    db.commit()
    return count


def reap_expired(db, *, now=None, limit: int = 200) -> int:
    """Expire queued jobs that were never claimed within their own timeout.

    Without this, a job whose enqueue succeeded and whose claim never happened sits "queued"
    for ever: the list shows work in progress that will never arrive, and the operator has no
    way to tell it apart from a job that is genuinely about to run.
    """
    now = now or utcnow()
    candidates = db.query(DBInspectionJob).filter(
        DBInspectionJob.status == "queued",
    ).order_by(DBInspectionJob.created_at.asc()).limit(max(int(limit), 1)).all()
    count = 0
    for row in candidates:
        created = row.created_at
        if created is None:
            continue
        timeout = _timeout_seconds(row.timeout_seconds)
        if timeout is None:
            # No usable deadline on the row: it can never be "over" anything. Expiring it
            # anyway would be inventing a limit the job was never given.
            continue
        if (now - created).total_seconds() < timeout:
            continue
        reason = "任务排队超过超时时间仍未执行，已过期"
        progress = [
            *(row.progress_json or []),
            _event("expired", now, detail=reason, failure_code="expired_in_queue"),
        ][-30:]
        count += db.query(DBInspectionJob).filter_by(
            id=row.id, status="queued",
        ).update({
            "status": "expired", "stage": "expired", "finished_at": now,
            "failure_reason": reason, "progress_json": progress,
        }, synchronize_session=False)
    db.commit()
    return count


def reap_overdue(db, now=None, *, limit: int = 200) -> int:
    """Terminate running jobs that overran their own deadline, or ignored a cancel.

    ``reap_interrupted`` only closes runs whose **lease** expired, so a healthy executor that
    keeps heartbeating never becomes a candidate - and ``timeout_seconds`` was only consulted
    inside ``commit_results``, which an executor that never commits never reaches. The result
    was a run that stayed ``running`` for ever: no ``retry`` (the parent is not terminal), no
    ``cancel`` effect (a second request only rewrites the timestamp). There was no exit at all.

    Two rules, checked in this order:

    * cancel grace expired -> ``cancelled``. The operator asked; the executor did not answer.
      The reason says the target may still be working, so nobody reads this as "stopped".
    * total timeout passed -> ``timed_out``. Same vocabulary as the commit-path deadline, so
      "over time" means one thing everywhere. The outcome stays ``unknown``: we know the
      deadline passed, not what the target did.
    """
    now = now or utcnow()
    candidates = db.query(DBInspectionJob).filter(
        DBInspectionJob.status == "running",
    ).order_by(
        # Oldest effective deadline first. `started_at` is nullable, and NULL ordering differs
        # between SQLite/MySQL (first) and Oracle (last), so an explicit COALESCE keeps the
        # oldest rows from being starved by the `limit` on every dialect.
        func.coalesce(DBInspectionJob.started_at, DBInspectionJob.created_at).asc(),
    ).limit(max(int(limit), 1)).all()
    reaped = 0
    for row in candidates:
        grace_expired = (
            bool(row.cancel_requested)
            and row.cancel_requested_at is not None
            and (now - row.cancel_requested_at).total_seconds() > CANCEL_GRACE_SECONDS
        )
        if grace_expired:
            reason = "取消未获执行端确认，已强制终结；执行端可能仍在运行，结果不发布"
            progress = [
                *(row.progress_json or []),
                _event("cancelled", now, detail=reason, failure_code="cancelled_force_closed"),
            ][-30:]
            reaped += db.query(DBInspectionJob).filter_by(
                id=row.id, status="running",
            ).update({
                "status": "cancelled", "stage": "cancelled", "finished_at": now,
                "failure_reason": reason, "owner_token": None, "lease_until": None,
                "progress_json": progress,
            }, synchronize_session=False)
            continue
        timeout = _timeout_seconds(row.timeout_seconds)
        # A run that never stamped `started_at` used to be invisible to *both* reapers: this
        # one required `started_at`, and `reap_interrupted` required an expired lease. A
        # healthy-but-wedged executor that claimed the job without stamping the start would
        # stay "running" for ever. The deadline is measured from the enqueue time in that case,
        # which is the only timestamp we actually have and is never earlier than reality.
        deadline_anchor = row.started_at or row.created_at
        deadline_passed = (
            timeout is not None
            and deadline_anchor is not None
            and (now - deadline_anchor).total_seconds() >= timeout
        )
        if not deadline_passed:
            continue
        reason = "超过任务总超时，执行端未在期限内结束；结果不发布、需人工核对"
        progress = [
            *(row.progress_json or []),
            _event("timed_out", now, detail=reason, failure_code="timeout"),
        ][-30:]
        reaped += db.query(DBInspectionJob).filter_by(
            id=row.id, status="running",
        ).update({
            "status": "timed_out", "stage": "timed_out", "finished_at": now,
            "failure_reason": reason, "owner_token": None, "lease_until": None,
            "progress_json": progress,
        }, synchronize_session=False)
    db.commit()
    return reaped


@contextmanager
def lease_guard(job_id: str, token: str, *, interval: float = LEASE_SECONDS / 3):
    """Keep a claimed job's lease alive while the work runs.

    A lease is 45s and an inspection routinely runs longer. Without renewal the
    lease expires mid-run and `commit_results` refuses the results - the operator
    then sees "failed" for an inspection that actually finished, which is worse
    than a failure because it is a lie about the outcome.

    Renewal uses its own session: sharing the caller's session across threads is
    not safe, and the renewal must not join the caller's transaction.
    """
    stop = threading.Event()

    def _renew() -> None:
        while not stop.wait(interval):
            renewal = SessionLocal()
            try:
                heartbeat(renewal, job_id, token)
            except Exception:  # renewal is best-effort; the run itself reports failure
                return
            finally:
                renewal.close()

    keeper = threading.Thread(target=_renew, daemon=True, name=f"inspection-lease-{job_id[:8]}")
    keeper.start()
    try:
        yield
    finally:
        stop.set()
        keeper.join(timeout=interval + 1)


def default_policy_for(db, instance, db_type: str):
    """Pick the policy a manual/implicit run would have used, without connecting anywhere.

    `execution_policy.resolve_execution_policy` also selects a default, but it connects to the
    target first (Oracle profile, host platform) and an enqueue must happen before any of that.
    This only chooses the row; the full validation still runs inside `execute`, so a wrong
    default produces a failed job instead of a silently different report.
    """
    from app.services.inspection import policy_service

    policy_service.ensure_default_policies(db)
    rows = (
        db.query(DBInspectionPolicy)
        .filter(DBInspectionPolicy.enabled == 1)
        .populate_existing()
        .order_by(DBInspectionPolicy.id)
        .all()
    )
    applicable = [row for row in rows if db_type in (row.db_types_json or [])]
    if not applicable:
        return None
    defaults = [row for row in applicable if db_type in policy_service._assistant_default_db_types(row)]
    candidates = defaults or applicable
    fallback_key = "host_resource" if db_type == "host" else f"{db_type}_standard"
    return next((row for row in candidates if row.policy_key == fallback_key), candidates[0])


def submit(db, instance, policy, *, requested_by=None, trigger_type="manual",
           idempotency_key=None, timeout_seconds=900, execution_guard=None) -> DBInspectionJob:
    """Enqueue, claim and run to a terminal state.

    Kept next to `enqueue` because "queued but never claimed" is the same class of defect as
    "built but never wired": the row exists, nothing executes. Every caller that wants work to
    happen must come through here, not through `enqueue` alone.
    """
    db_type = db_type_for(policy, instance)
    check_execution_guard(db, execution_guard)
    row = enqueue(db, instance, db_type, policy, requested_by=requested_by,
                  trigger_type=trigger_type, idempotency_key=idempotency_key,
                  timeout_seconds=timeout_seconds)
    if row.status != "queued":
        # An idempotent replay, or an existing retry child: the run already exists, and running
        # it again would create a second execution under one idempotency key.
        if row.status in {"cancelled", "expired"}:
            # These produced nothing on purpose. Returning one as the answer to a *new* request
            # would present a cancelled run as its result.
            raise JobError("该任务已取消或已过期，请提交新的巡检请求", code="not_resumable")
        return row
    token = uuid.uuid4().hex
    check_execution_guard(db, execution_guard)
    if claim(db, row.id, token) is None:
        raise JobError("巡检任务已被取消或已被其他执行节点领取")
    return execute(db, row, token, execution_guard=execution_guard)


def db_type_for(policy, instance) -> str:
    """The db_type a job must be filed under, mirroring `runner.run_inspection`.

    A host-scoped policy is executed against a synthesized host target, so the
    queue has to agree with the runner about which scope the job belongs to.
    """
    return "host" if (getattr(policy, "db_types_json", None) or []) == ["host"] else instance.db_type


def check_execution_guard(db, execution_guard=None) -> None:
    if execution_guard is not None and execution_guard(db) is False:
        raise JobError("Execution guard refused", code="execution_guard_rejected")


def execute(db, row: DBInspectionJob, token: str, *, execution_guard=None,
            sync_report_artifact: bool = True) -> DBInspectionJob:
    """Run an already-claimed job to a terminal state.

    The frozen snapshot is what runs. `validate_job_target` still consults the live row, so
    deleting or disabling a template between enqueue and execute refuses the job - but an
    *edit* to the template no longer silently swaps the content under a reviewed revision.

    Report indexing is on for every producer (manual, scheduled, diagnosis, legacy pages), not
    only the plan adapter: keeping it opt-in meant the same run appeared in the report centre
    or did not depending on which door it came through.
    """
    policy = frozen_policy(row)
    job_id = row.id
    with lease_guard(row.id, token):
        try:
            check_execution_guard(db, execution_guard)
            instance = validate_job_target(db, row)
            db.commit()
            _target, findings = runner.prepare_findings(db, instance, policy=policy, frozen=True)
            db.rollback()
            db.refresh(row)
            validate_job_target(db, row)
            commit_results(db, job_id, token, findings, execution_guard=execution_guard,
                           sync_report_artifact=sync_report_artifact)
        except Exception as exc:
            db.rollback()
            if getattr(exc, "code", None) == "execution_guard_rejected":
                raise
            # Carry the producer's explicit classification into the terminal event so the read
            # model does not have to reconstruct the cause from a reworded message.
            finish_error(
                db, job_id, token, "failed", redact_database_error(str(exc)),
                code=getattr(exc, "code", None) or getattr(exc, "classification", None),
                execution_guard=execution_guard,
            )
    db.expire_all()
    return db.get(DBInspectionJob, row.id)


def claim(db, job_id: str, token: str, now=None) -> DBInspectionJob | None:
    now = now or utcnow()
    count = db.query(DBInspectionJob).filter_by(id=job_id, status="queued", cancel_requested=0).update({
        "status": "running", "stage": "validating", "owner_token": token,
        "started_at": now, "lease_until": now + timedelta(seconds=LEASE_SECONDS),
    }, synchronize_session=False)
    db.commit()
    db.expire_all()
    return db.get(DBInspectionJob, job_id) if count else None


def heartbeat(db, job_id: str, token: str, *, stage=None, detail=None, now=None) -> bool:
    now = now or utcnow()
    row = db.get(DBInspectionJob, job_id)
    if not row:
        return False
    updates = {"lease_until": now + timedelta(seconds=LEASE_SECONDS)}
    if stage and stage != row.stage:
        updates.update(stage=stage, progress_json=[*(row.progress_json or []), _event(stage, now, detail=detail)][-30:])
    count = db.query(DBInspectionJob).filter(
        DBInspectionJob.id == job_id, DBInspectionJob.status == "running",
        DBInspectionJob.owner_token == token, DBInspectionJob.lease_until >= now,
    ).update(updates, synchronize_session=False)
    db.commit()
    return bool(count)


def finish_error(db, job_id: str, token: str, status: str, reason: str, *, code: str | None = None,
                 execution_guard=None) -> None:
    """Write a terminal failure **and the event that proves it**.

    The event matters as much as the status: a zero-result failed run has no result rows, so the
    progress stream is the only evidence of what happened. Without it the read model could only
    show "failed" with no detail, which is indistinguishable from a bug.
    """
    if status not in {"failed", "cancelled", "timed_out", "interrupted", "expired"}:
        raise JobError("Invalid terminal error status")
    check_execution_guard(db, execution_guard)
    now = utcnow()
    row = db.get(DBInspectionJob, job_id)
    if row is None:
        return
    redacted = redact_database_error(str(reason))[:1500]
    extra = {"detail": redacted}
    if code:
        extra["failure_code"] = code
    progress = [*(row.progress_json or []), _event(status, now, **extra)][-30:]
    db.query(DBInspectionJob).filter(
        DBInspectionJob.id == job_id, DBInspectionJob.status == "running",
        DBInspectionJob.owner_token == token, DBInspectionJob.lease_until >= now,
    ).update({
        "status": status, "stage": status, "finished_at": now, "owner_token": None,
        "lease_until": None, "failure_reason": redacted, "progress_json": progress,
    }, synchronize_session=False)
    db.commit()


def _finalize_refused_commit(db, job_id: str, token: str, *, execution_guard=None) -> bool:
    """Resolve the job after ``commit_results`` refused to publish.

    A refusal happens for several reasons (lost lease, duplicate commit, deadline, cancellation).
    The cancellation case is special: leaving the job ``running`` would let the reaper rewrite it
    as "heartbeat lost", which is a fabricated cause. The owner is right here and acknowledged
    the cancel, so the run is closed as cancelled.
    """
    db.rollback()
    row = db.get(DBInspectionJob, job_id)
    if row is None or row.status != "running" or row.owner_token != token:
        return False
    if not row.cancel_requested:
        return False
    finish_cancelled(db, job_id, token, "cancelled_during_run", execution_guard=execution_guard)
    return False


def commit_results(db, job_id: str, token: str, findings: list, *, execution_guard=None,
                   sync_report_artifact: bool = True) -> bool:
    check_execution_guard(db, execution_guard)
    now = utcnow()
    # Hold the row write lock until both evidence and terminal outcome commit atomically.
    claimed = db.query(DBInspectionJob).filter(
        DBInspectionJob.id == job_id, DBInspectionJob.status == "running",
        DBInspectionJob.owner_token == token, DBInspectionJob.cancel_requested == 0,
        DBInspectionJob.lease_until >= now,
    ).update({"stage": "saving"}, synchronize_session=False)
    if not claimed:
        return _finalize_refused_commit(db, job_id, token, execution_guard=execution_guard)
    db.expire_all()
    row = db.get(DBInspectionJob, job_id)
    if (now - row.started_at).total_seconds() >= row.timeout_seconds:
        db.rollback()
        finish_error(db, job_id, token, "timed_out", "巡检达到任务总超时，结果未发布", code="timeout",
                     execution_guard=execution_guard)
        return False
    try:
        validate_job_target(db, row)
        snapshot = row.policy_snapshot_json.get(TARGET_SNAPSHOT_KEY)
        if isinstance(snapshot, dict) and (
            snapshot.get("id", row.instance_id) != row.instance_id
            or snapshot.get("db_type", row.db_type) != row.db_type
        ):
            raise JobError("巡检目标快照与任务身份不一致", code="snapshot_invalid")
        recorded = result_writer.normalize_target_snapshot(snapshot, instance_id=row.instance_id, db_type=row.db_type)
        target = SimpleNamespace(
            id=row.instance_id, db_type=row.db_type, instance_name=recorded["name"],
            host=recorded["host"], port=recorded["port"],
        )
        published = runner.materialize_staged_writes(db, findings, instance_id=row.instance_id)
        rows = result_writer.save_findings(db, target, job_id, published, commit=False)
        summary = result_writer.execution_summary(rows)
        row.status = summary["task_status"]
        row.stage = "completed"
        row.failure_reason = redact_database_error(summary["failure_reason"]) if summary["failure_reason"] else None
        row.finished_at = now
        row.owner_token = None
        row.lease_until = None
        terminal = _event("completed", now, result_count=len(rows))
        if summary["task_status"] in {"failed", "partial"}:
            # Partial/failed collection is a collector problem and says so, instead of being
            # left as an unexplained failure the read model has to guess about.
            terminal["failure_code"] = "collector_error"
        row.progress_json = [*(row.progress_json or []), terminal][-30:]
        if sync_report_artifact:
            from app.services.inspection import report_artifacts

            db.flush()  # The source mapping must see the terminal job as well as its real results.
            artifact = report_artifacts.sync_job_artifact(db, row)
            if artifact is not None:
                row.progress_json = [*(row.progress_json or [])[:-1], {**terminal, "report_artifact_id": artifact.id}]
                db.flush()
            check_execution_guard(db, execution_guard)
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise


ALL_STATUSES = (
    "queued", "running", "success", "partial", "failed",
    "cancelled", "timed_out", "interrupted", "expired",
)
#: outcome word -> the stored statuses that produce it. ``unknown`` also covers any status this
#: build does not recognise, which is why it is handled separately in ``list_jobs``.
OUTCOME_STATUSES = {
    "success": ("success",),
    "partial": ("partial",),
    "failed": ("failed",),
    "cancelled": ("cancelled",),
    "expired": ("expired",),
    "unknown": ("timed_out", "interrupted"),
}


def list_jobs(
    db,
    *,
    instance_ids=None,
    instance_id: int | None = None,
    db_type: str | None = None,
    statuses=None,
    outcomes=None,
    trigger_type: str | None = None,
    since=None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[DBInspectionJob], int]:
    """The queue itself: every run, including the ones that produced no result rows.

    Aggregating this list from ``DBCheckResult`` is what hid failed and cancelled runs: they
    have no rows, so they simply were not there. The job table is the only complete source.
    """
    query = db.query(DBInspectionJob)
    if instance_ids is not None:
        query = query.filter(DBInspectionJob.instance_id.in_(sorted(int(value) for value in instance_ids)))
    if instance_id is not None:
        query = query.filter(DBInspectionJob.instance_id == int(instance_id))
    if db_type:
        query = query.filter(DBInspectionJob.db_type == db_type)
    if statuses:
        query = query.filter(DBInspectionJob.status.in_(sorted(set(statuses))))
    if trigger_type:
        query = query.filter(DBInspectionJob.trigger_type == trigger_type)
    if since is not None:
        query = query.filter(DBInspectionJob.created_at >= since)
    if outcomes:
        wanted = {str(value).strip().lower() for value in outcomes if str(value).strip()}
        conditions = []
        mapped = {
            status
            for outcome in wanted if outcome in OUTCOME_STATUSES
            for status in OUTCOME_STATUSES[outcome]
        }
        if mapped:
            conditions.append(DBInspectionJob.status.in_(sorted(mapped)))
        if "unknown" in wanted:
            # A status written by a newer build is *also* unknown; excluding it would let a
            # future status quietly disappear from the "needs attention" filter.
            conditions.append(DBInspectionJob.status.not_in(ALL_STATUSES))
        if not conditions:
            return [], 0
        query = query.filter(or_(*conditions))
    total = query.count()
    rows = (
        query.order_by(DBInspectionJob.created_at.desc(), DBInspectionJob.id.desc())
        .offset((max(int(page), 1) - 1) * int(page_size))
        .limit(int(page_size))
        .all()
    )
    return rows, total


def retry(db, row: DBInspectionJob, *, requested_by=None, execution_guard=None) -> DBInspectionJob:
    """Queue and run one more attempt of a retryable job, inheriting its frozen snapshot.

    Retrying as a *new* independent job would re-freeze the template from whatever it says now,
    so the operator would get results for a plan they never reviewed. The child inherits the
    parent's snapshot and differs only in ``attempt``.
    """
    instance = db.get(DBInstance, row.instance_id)
    if instance is None:
        raise JobError("巡检实例不存在，不能重试", code="instance_disabled")
    policy = db.get(DBInspectionPolicy, row.policy_id)
    check_execution_guard(db, execution_guard)
    child = enqueue(
        db, instance, row.db_type, policy,
        requested_by=row.requested_by if requested_by is None else requested_by,
        trigger_type="retry", timeout_seconds=row.timeout_seconds, parent_job=row,
    )
    token = uuid.uuid4().hex
    check_execution_guard(db, execution_guard)
    if claim(db, child.id, token) is None:
        # An earlier retry already exists (unique parent_job_id) or was cancelled meanwhile.
        return db.get(DBInspectionJob, child.id)
    return execute(db, child, token, execution_guard=execution_guard)
