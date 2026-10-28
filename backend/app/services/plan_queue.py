"""Bounded durable delivery of explicit plans, never an alternate execution engine."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import event, func, inspect as sa_inspect, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.transaction_lock import lock_transaction
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision
from app.services import capability_registry as registry, plan_service as plans, plan_validation
from app.services.capability_effect_contract import EFFECT_RESULT_SCHEMA, is_common_effect

MAX_BATCH_TARGETS = 100
MAX_PENDING = 1000
MAX_ACTIVE = 8
MAINTENANCE_LIMIT = 32
LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 10
LOCK_KEY = "capability_queue"
PENDING = ("queued", "running", "paused", "reconciliation_required")
ATTENTION = ("reconciliation_required", "blocked")
logger = logging.getLogger(__name__)


class QueueError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ExecutionLeaseLost(RuntimeError):
    """Not a handler failure: the executor must unwind without publishing any result."""

    def __init__(self, code="execution_lease_lost"):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(value):
    return value.isoformat() + "Z" if value else None


def _clean(db):
    if db.new or db.dirty or db.deleted:
        raise QueueError("queue_requires_clean_session")


def _members(db, batch_id):
    return db.query(DBCapabilityExecution).filter_by(batch_id=batch_id).order_by(
        DBCapabilityExecution.seq).populate_existing().all()


def _authorized_batch(db, batch_id, actor, *, owner_only=False):
    user, _, _ = registry._principal(db, actor)
    batch = db.query(DBCapabilityBatch).filter_by(batch_id=batch_id).populate_existing().first()
    if batch is None or (batch.requested_by != user.id and (owner_only or user.role != "admin")):
        raise QueueError("batch_not_found")
    members = _members(db, batch_id)
    if not members:
        raise QueueError("batch_corrupt")
    loaded = []
    for member in members:
        plan = plans._load(db, member.plan_id, actor, owner_only=owner_only)
        if plan.requested_by != batch.requested_by:
            raise QueueError("batch_owner_changed")
        if member.state != "cancelled" and plans._hash(plan.capability_snapshot_json) != member.binding_hash:
            raise QueueError("batch_plan_changed")
        loaded.append((member, plan))
    return batch, loaded


def _view(batch, loaded, *, reused=False, schema_version=1):
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise QueueError("invalid_batch_schema_version")
    if schema_version == 1 and any(member.control_enabled for member, _ in loaded):
        raise QueueError("batch_control_requires_v2")
    items = [{"plan_id": plan.plan_id, "seq": member.seq, "queue_state": member.state,
              "reason": member.reason, "status": plan.status, "outcome": plan.outcome,
              "outcome_kind": plan.outcome_kind,
              "bound_revision": member.plan_revision, "binding_hash": member.binding_hash,
              "binding_current": plans._hash(plan.capability_snapshot_json) == member.binding_hash,
              "result_ref": plan.result_ref_json if member.state != "cancelled" else None,
              "attempt": member.attempt, "started_at": _stamp(plan.started_at),
              "finished_at": _stamp(plan.finished_at)} for member, plan in loaded]
    if schema_version == 2:
        for item, (member, plan) in zip(items, loaded):
            item.update(execution_id=member.id, control_enabled=member.control_enabled,
                        control_version=member.control_version, desired_state=member.desired_state,
                        observed_state=member.observed_state, checkpoint_hash=member.checkpoint_hash,
                        fencing_epoch=member.fencing_epoch,
                        instance_id=plan.instance_id, db_type=plan.db_type, capability_id=plan.capability_id)
    queue_counts = dict(Counter(item["queue_state"] for item in items))
    outcomes = dict(Counter(item["outcome"] for item in items if item["outcome"] is not None))
    actual_settled = sum(item["status"] == "settled" for item in items)
    if any(item["queue_state"] in ATTENTION for item in items):
        status, outcome = "reconciliation_required", None
    elif queue_counts.get("running"):
        status, outcome = "running", None
    elif queue_counts.get("queued"):
        status, outcome = "queued", None
    elif queue_counts.get("paused"):
        status, outcome = "paused", None
    elif queue_counts.get("stopped"):
        status, outcome = "stopped", None
    else:
        status = "settled"
        if all(item["status"] == "settled" and item["outcome"] == "succeeded"
               and item["queue_state"] == "archived" for item in items):
            outcome = "succeeded"
        elif queue_counts.get("cancelled") == len(items):
            outcome = "cancelled"
        elif queue_counts.get("expired") == len(items):
            outcome = "expired"
        elif len(outcomes) == 1 and actual_settled == len(items):
            outcome = next(iter(outcomes))
        else:
            outcome = "partial"
    return {"schema": f"capability.batch_read/v{schema_version}", "batch_id": batch.batch_id,
            "requested_by": batch.requested_by, "status": status, "outcome": outcome,
            "accepted": True, "reused": reused, "total": len(items),
            "settled_plans": actual_settled, "queue_counts": queue_counts, "outcomes": outcomes,
            "requires_attention": status == "reconciliation_required" or bool(outcomes.get("unknown")),
            "cancel_requested": batch.cancel_requested_at is not None,
            "cancel_requested_at": _stamp(batch.cancel_requested_at),
            "created_at": _stamp(batch.created_at), "deadline_at": _stamp(batch.deadline_at),
            "items": items}


def read_batch(db, batch_id, *, actor, schema_version=1) -> dict:
    return _view(*_authorized_batch(db, batch_id, actor), schema_version=schema_version)


def list_batches(db, *, actor, page=1, page_size=50, schema_version=1) -> dict:
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise QueueError("invalid_batch_schema_version")
    if (type(page) is not int or not 1 <= page <= 1000000
            or type(page_size) is not int or not 1 <= page_size <= 100):
        raise QueueError("invalid_pagination")
    user, _, _ = registry._principal(db, actor)
    query = db.query(DBCapabilityBatch.batch_id).order_by(
        DBCapabilityBatch.created_at.desc(), DBCapabilityBatch.batch_id.desc())
    if user.role != "admin":
        query = query.filter(DBCapabilityBatch.requested_by == user.id)
    # Filter before counting/pagination: even totals must not expose revoked scopes.
    # Stream candidate IDs; do not hydrate all historical plans/results into memory.
    visible, total = [], 0
    start = (page - 1) * page_size
    for (batch_id,) in query.yield_per(50):
        try:
            batch, loaded = _authorized_batch(db, batch_id, actor)
        except (QueueError, plans.PlanError, registry.CapabilityError) as exc:
            if getattr(exc, "code", "").endswith("storage_unavailable"):
                raise
            continue
        if schema_version == 1 and any(member.control_enabled for member, _ in loaded):
            raise QueueError("batch_control_requires_v2")
        if start <= total < start + page_size:
            summary = _view(batch, loaded, schema_version=schema_version)
            summary.pop("items")
            visible.append(summary)
        total += 1
    return {"schema": f"capability.batch_list/v{schema_version}", "items": visible,
            "total": total, "page": page, "page_size": page_size}


def _evidence(db, plan):
    """Terminal ledger evidence only; no handler calls or synthesized success."""
    if plan is None or plan.status != "settled" or not plan.finished_at or not plan.outcome:
        return False
    rows = plans._steps(db, plan.plan_id)
    revision = db.query(DBCapabilityPlanRevision).filter_by(
        plan_id=plan.plan_id, revision=plan.capability_snapshot_json.get("revision", 1)).first()
    try:
        plan_validation.validate_frozen_plan(plan, revision, rows)
    except plan_validation.PlanValidationError:
        return False
    if not rows or any(row.status in {"pending", "running"} for row in rows):
        return False
    products = [row for row in rows if row.status in {"succeeded", "partial"}]
    definitions = {step["seq"]: entry for step, entry in zip(
        plan.capability_snapshot_json["steps"], plan.capability_snapshot_json["definitions"])}
    for row in rows:
        reference = row.result_ref_json
        confirmed_cancel = plan.started_at is not None and row.status == "cancelled"
        if row.status in {"succeeded", "partial"} or confirmed_cancel or reference is not None:
            if not isinstance(reference, dict):
                return False
            reference_kind = reference.get("kind")
            definition = definitions[row.seq]
            typed_root_cause_effect = (
                reference_kind == "capability_effect"
                and definition.get("capability_id") == "diagnosis.root_cause.execute"
                and reference.get("capability_id") == "diagnosis.root_cause.execute"
                and reference.get("schema") == "diagnosis.root_cause_result/v1"
            )
            typed_report_effect = (
                reference_kind == "capability_effect"
                and definition.get("capability_id") == "report.document.generate"
                and reference.get("capability_id") == "report.document.generate"
                and reference.get("schema") == "report.document_result/v1"
            )
            typed_common_effect = (
                reference_kind == "capability_effect"
                and is_common_effect(
                    definition.get("capability_id"),
                    definition.get("version"),
                    definition.get("output_schema"),
                )
                and reference.get("capability_id") == definition.get("capability_id")
                and reference.get("schema") == EFFECT_RESULT_SCHEMA
            )
            if (reference_kind not in {"capability_read", "inspection_job"}
                    and not typed_root_cause_effect and not typed_report_effect
                    and not typed_common_effect):
                return False
            if confirmed_cancel and reference_kind != "inspection_job":
                return False
            try:
                if reference != plans._result_ref(definition, reference.get("inline")):
                    return False
                if reference_kind == "inspection_job":
                    if plans.validate_effect_reference(db, plan, row) is False:
                        return False
                    domain_outcome = reference["inline"]["outcome"]
                    step_outcome = "succeeded" if domain_outcome == "success" else domain_outcome
                    if step_outcome != row.status:
                        return False
                elif typed_root_cause_effect or typed_report_effect or typed_common_effect:
                    if plans.validate_effect_reference(db, plan, row) is False:
                        return False
                    domain_outcome = reference["inline"].get("outcome")
                    step_outcome = "succeeded" if domain_outcome == "success" else domain_outcome
                    if step_outcome != row.status:
                        return False
                elif row.status == "partial":
                    return False  # The current read contracts do not declare partial products.
            except (ValueError, TypeError, KeyError, AttributeError):
                return False
    if plan.outcome == "unknown" or any(row.status == "unknown" for row in rows):
        return False  # A persisted unknown verdict is evidence of uncertainty, not resolution.
    if plan.outcome in {"succeeded", "partial", "failed", "expired"} or (
            plan.outcome == "cancelled" and plan.started_at is not None):
        expected = {"kind": "capability_plan_steps", "plan_id": plan.plan_id,
                    "schema": "capability.plan_results/v1",
                    "steps": [row.seq for row in rows if row.result_ref_json]}
        if not plan.started_at or plan.result_ref_json != expected:
            return False
        if plan.outcome == "succeeded":
            return all(row.status == "succeeded" for row in rows)
        if plan.outcome == "partial":
            return bool(products) and any(row.status != "succeeded" for row in rows)
        if plan.outcome == "cancelled":
            return rows[0].status == "cancelled" and all(row.status == "skipped" for row in rows[1:])
        return any(row.status == plan.outcome for row in rows)
    if plan.outcome == "cancelled":
        return all(row.status == "cancelled" and row.started_at is None and row.result_ref_json is None
                   for row in rows)
    return bool(plan.outcome_reason)


def enqueue_batch(db, plan_ids: list[str], *, actor: dict, idempotency_key: str,
                  deadline_seconds: int = 3600, schema_version=1) -> dict:
    if (not isinstance(plan_ids, list) or not 1 <= len(plan_ids) <= MAX_BATCH_TARGETS
            or any(not isinstance(value, str) or not 1 <= len(value) <= 30 for value in plan_ids)
            or len(set(plan_ids)) != len(plan_ids)):
        raise QueueError("invalid_plan_ids")
    if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
            or len(idempotency_key) > 128):
        raise QueueError("invalid_idempotency_key")
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 86400:
        raise QueueError("invalid_deadline")
    _clean(db)
    fingerprint = hashlib.sha256(json.dumps(
        {"plan_ids": plan_ids, "deadline_seconds": deadline_seconds},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        lock_transaction(db, "capability_plan_state")
        lock_transaction(db, LOCK_KEY)
        user, _, _ = registry._principal(db, actor)
        previous = db.query(DBCapabilityBatch).filter_by(
            requested_by=user.id, idempotency_key=idempotency_key).populate_existing().first()
        if previous:
            if previous.idempotency_key != idempotency_key or previous.input_hash != fingerprint:
                raise QueueError("idempotency_conflict")
            result = _view(*_authorized_batch(db, previous.batch_id, actor), reused=True, schema_version=schema_version)
            db.commit()
            return result
        loaded = []
        for plan_id in plan_ids:
            plan = plans._load(db, plan_id, actor, owner_only=True)
            if plan.status == "settled":
                if not _evidence(db, plan):
                    raise QueueError("plan_terminal_evidence_missing")
            elif plan.status not in {"validated", "approved"} or not _unstarted(db, plan):
                raise QueueError("plan_not_queueable")
            else:
                review = plans.review_plan(db, plan_id, actor=actor)
                if not review["can_execute"]:
                    raise QueueError(review["issues"][0]["code"])
            loaded.append(plan)
        if db.query(DBCapabilityExecution.id).filter(DBCapabilityExecution.plan_id.in_(plan_ids),
                                                    DBCapabilityExecution.state != "cancelled").first():
            raise QueueError("plan_already_enqueued")
        pending = db.query(func.count(DBCapabilityExecution.id)).filter(
            DBCapabilityExecution.state.in_(PENDING)).scalar()
        if pending + sum(plan.status != "settled" for plan in loaded) > MAX_PENDING:
            raise QueueError("queue_full")
        now = _now()
        batch = DBCapabilityBatch(batch_id="CB" + uuid.uuid4().hex[:28], requested_by=user.id,
                                  idempotency_key=idempotency_key, input_hash=fingerprint,
                                  deadline_seconds=deadline_seconds, created_at=now,
                                  deadline_at=now + timedelta(seconds=deadline_seconds))
        db.add(batch)
        pairs = []
        for seq, plan in enumerate(loaded, 1):
            archived = plan.status == "settled"
            member = DBCapabilityExecution(
                batch_id=batch.batch_id, seq=seq, plan_id=plan.plan_id,
                plan_revision=plan.capability_snapshot_json.get("revision", 1),
                binding_hash=plans._hash(plan.capability_snapshot_json),
                target_key=(f"instance:{plan.instance_id}" if plan.instance_id is not None
                            else f"global:{plan.capability_id}"),
                state="archived" if archived else "queued", attempt=0,
                finished_at=now if archived else None)
            db.add(member)
            pairs.append((member, plan))
        db.flush()
        result = _view(batch, pairs, schema_version=schema_version)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def cancel_batch(db, batch_id, *, actor, schema_version=1) -> dict:
    _clean(db)
    try:
        lock_transaction(db, LOCK_KEY)
        batch, loaded = _authorized_batch(db, batch_id, actor, owner_only=True)
        if schema_version == 1 and any(member.control_enabled for member, _ in loaded):
            raise QueueError("batch_control_requires_v2")
        batch.cancel_requested_at = batch.cancel_requested_at or _now()
        batch.cancel_requested_by = batch.requested_by
        for member, plan in loaded:
            if member.state == "queued":
                if _unstarted(db, plan):
                    _finish(member, "cancelled", "delivery_cancelled_before_start")
                    if member.control_enabled:
                        from app.services import plan_control
                        plan_control.cancel_unstarted_delivery(db, member, plan)
                elif _evidence(db, plan):
                    _finish(member, "archived", "persisted_terminal_evidence")
                else:
                    _finish(member, "reconciliation_required", "cancel_encountered_started_plan")
        db.flush()
        result = _view(batch, loaded, schema_version=schema_version)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def assert_plan_editable(db, plan_id):
    """Hold both coordination locks until the caller commits or rolls back its revision."""
    lock_transaction(db, "capability_plan_state")
    lock_transaction(db, LOCK_KEY)
    members = db.query(DBCapabilityExecution).filter_by(plan_id=plan_id).populate_existing().all()
    if any(member.state != "cancelled" for member in members):
        raise plans.PlanError("plan_batch_binding_active")
    if members:
        plan = db.query(DBCapabilityPlan).filter_by(plan_id=plan_id).populate_existing().first()
        rows = plans._steps(db, plan_id)
        if (plan is None or plan.started_at is not None or plan.result_ref_json is not None
                or plan.status == "running" or not rows
                or any(row.started_at is not None or row.result_ref_json is not None
                       or row.status not in {"pending", "skipped", "cancelled"} for row in rows)):
            raise plans.PlanError("executed_plan_requires_rerun")


def _unstarted(db, plan):
    return (plan is not None and plan.status in {"validated", "approved"} and plan.started_at is None
            and all(row.status == "pending" and row.started_at is None and row.result_ref_json is None
                    for row in plans._steps(db, plan.plan_id)))


def _finish(member, state, reason):
    member.state, member.reason = state, reason
    member.owner_token = member.lease_expires_at = None
    member.finished_at = _now() if state != "queued" else None


def _recover(db, now):
    stale = db.query(DBCapabilityExecution, DBCapabilityBatch).join(
        DBCapabilityBatch, DBCapabilityBatch.batch_id == DBCapabilityExecution.batch_id).filter(or_(
            (DBCapabilityExecution.state == "running") & or_(
                DBCapabilityExecution.lease_expires_at <= now,
                DBCapabilityBatch.deadline_at <= now),
            (DBCapabilityExecution.state.in_(("queued", "paused"))) & (DBCapabilityBatch.deadline_at <= now),
        )).order_by(DBCapabilityExecution.id).populate_existing().limit(MAINTENANCE_LIMIT).all()
    uncertain = db.query(DBCapabilityExecution, DBCapabilityBatch).join(
        DBCapabilityBatch, DBCapabilityBatch.batch_id == DBCapabilityExecution.batch_id).filter(
            DBCapabilityExecution.state == "reconciliation_required").order_by(
                DBCapabilityExecution.reconciliation_checked_at,
                DBCapabilityExecution.id).populate_existing().limit(8).all()
    for member, batch in stale + uncertain:
        plan = db.query(DBCapabilityPlan).filter_by(plan_id=member.plan_id).populate_existing().first()
        member.reconciliation_checked_at = now
        if plan and plans._hash(plan.capability_snapshot_json) != member.binding_hash:
            _finish(member, "reconciliation_required", "batch_plan_changed")
            continue
        if _evidence(db, plan):
            _finish(member, "archived", "persisted_terminal_evidence")
        elif member.state == "reconciliation_required":
            continue
        elif _unstarted(db, plan):
            if batch.cancel_requested_at:
                _finish(member, "cancelled", "delivery_cancelled_before_start")
            elif batch.deadline_at <= now:
                _finish(member, "expired", "deadline_before_start")
            else:
                _finish(member, "queued", "unstarted_owner_recovered")
        else:
            _finish(member, "reconciliation_required", "deadline_or_owner_lost_after_start")
        if member.control_enabled:
            from app.services import plan_control
            if member.state in {"cancelled", "expired"} and _unstarted(db, plan):
                plan_control.cancel_unstarted_delivery(db, member, plan)
            else:
                plan_control.complete(db, member, plan, certain=member.state == "archived")
    db.flush()


def _require_guard():
    if "execution_guard" not in inspect.signature(plans.execute_plan).parameters:
        raise QueueError("execution_guard_not_integrated")


def _claim(session_factory, worker_id, worker_identity=None):
    with session_factory() as db:
        lock_transaction(db, LOCK_KEY)
        now = _now()
        _recover(db, now)
        active = db.query(DBCapabilityExecution).filter_by(state="running").count()
        if active >= MAX_ACTIVE:
            db.commit()
            return None
        occupied = {key for (key,) in db.query(DBCapabilityExecution.target_key).filter(
            DBCapabilityExecution.state.in_(("running", "paused", "reconciliation_required"))).all()}
        from app.services import plan_control
        from app.models.capability_control import DBCapabilityTargetFence
        controlled = db.query(DBCapabilityExecution.id).filter_by(control_enabled=True).first() is not None
        fences = ({row.target_key: row for row in db.query(DBCapabilityTargetFence).filter(
            DBCapabilityTargetFence.hold_state.notin_(("released", "unclaimed"))).all()} if controlled else {})
        occupied.update(fences)
        candidate_query = db.query(DBCapabilityExecution).filter_by(state="queued")
        if occupied:
            holders = [row.holder_execution_id for row in fences.values() if row.holder_execution_id is not None]
            candidate_query = candidate_query.filter(or_(
                DBCapabilityExecution.target_key.notin_(occupied), DBCapabilityExecution.id.in_(holders)))
        # Different pinned worker releases can coexist. Looking only at the first
        # 32 queued IDs can indefinitely starve a later matching release.
        candidates = candidate_query.order_by(DBCapabilityExecution.id).populate_existing().limit(MAX_PENDING).all()
        for member in candidates:
            fence = fences.get(member.target_key)
            if member.target_key in occupied and not (
                    member.control_enabled
                    and fence is not None and fence.holder_execution_id == member.id):
                continue
            batch = db.get(DBCapabilityBatch, member.batch_id)
            if batch.deadline_at <= now:
                raw = db.get(DBCapabilityPlan, member.plan_id) if member.control_enabled else None
                if member.control_enabled and not _unstarted(db, raw):
                    _finish(member, "reconciliation_required", "deadline_before_resume")
                    plan_control.complete(db, member, raw, certain=False)
                else:
                    _finish(member, "expired", "deadline_before_start")
                    if member.control_enabled:
                        plan_control.cancel_unstarted_delivery(db, member, raw)
                continue
            if batch.cancel_requested_at:
                raw = db.get(DBCapabilityPlan, member.plan_id) if member.control_enabled else None
                if member.control_enabled and not _unstarted(db, raw):
                    _finish(member, "reconciliation_required", "cancel_before_resume_requires_reconciliation")
                    plan_control.complete(db, member, raw, certain=False)
                else:
                    _finish(member, "cancelled", "delivery_cancelled_before_start")
                    if member.control_enabled:
                        plan_control.cancel_unstarted_delivery(db, member, raw)
                continue
            actor = {"user_id": batch.requested_by}
            raw = db.query(DBCapabilityPlan).filter_by(plan_id=member.plan_id).populate_existing().first()
            from app.models.engine_execution import DBEnginePlanBinding
            legacy_binding = db.get(DBEnginePlanBinding, member.plan_id)
            if legacy_binding is not None and legacy_binding.state == "reprepare_required":
                _finish(member, "reconciliation_required" if raw and raw.status == "running" else "blocked",
                        "reprepare_required")
                continue
            from app.runtime.execution_identity import worker_matches
            if raw is not None and not worker_matches(raw.capability_snapshot_json, worker_identity):
                continue
            resuming = raw is not None and plan_control.resumable(db, member, raw)
            if raw is None or (not resuming and not _unstarted(db, raw) and not _evidence(db, raw)):
                _finish(member, "reconciliation_required", "plan_already_started_or_missing")
                occupied.add(member.target_key)
                continue
            try:
                plan = plans._load(db, member.plan_id, actor, owner_only=True)
                if plans._hash(plan.capability_snapshot_json) != member.binding_hash:
                    raise QueueError("batch_plan_changed")
                if _evidence(db, plan):
                    _finish(member, "archived", "persisted_terminal_evidence")
                    continue
                if not resuming and not _unstarted(db, plan):
                    _finish(member, "reconciliation_required", "plan_already_started")
                    occupied.add(member.target_key)
                    continue
                if resuming:
                    plans._check_plan(db, plan, actor)
                    if reason := plan_validation.approval_block_reason(plan, db=db):
                        raise QueueError(reason)
                    plan_control.validate_checkpoint(db, member, plan)
                else:
                    review = plans.review_plan(db, member.plan_id, actor=actor)
                    if not review["can_execute"]:
                        raise QueueError(review["issues"][0]["code"])
                plan_control.on_claim(db, member, plan)
            except (QueueError, plans.PlanError, registry.CapabilityError, plan_control.ControlError) as exc:
                if getattr(exc, "code", "").endswith("storage_unavailable"):
                    raise
                _finish(member, "reconciliation_required" if resuming or member.control_enabled else "blocked", exc.code)
                if member.control_enabled:
                    plan_control.complete(db, member, raw, certain=False)
                continue
            token = uuid.uuid4().hex
            claimed = db.query(DBCapabilityExecution).filter_by(id=member.id, state="queued").update(
                {"state": "running", "owner_token": token, "worker_id": worker_id,
                 "attempt": DBCapabilityExecution.attempt + 1, "claimed_at": now,
                 "heartbeat_at": now, "lease_expires_at": min(
                     now + timedelta(seconds=LEASE_SECONDS), batch.deadline_at),
                 "reason": None, "finished_at": None}, synchronize_session=False)
            if claimed == 1:
                claim = {"id": member.id, "batch_id": batch.batch_id, "plan_id": member.plan_id,
                         "owner_token": token, "worker_id": worker_id, "actor": actor,
                         "binding_hash": member.binding_hash, "worker_identity": worker_identity}
                db.commit()
                return claim
            db.rollback()
            return None
        db.commit()
    return None


def _owned(db, claim, *, locking=True):
    now = _now()
    member_query = select(DBCapabilityExecution.__table__).where(
        DBCapabilityExecution.id == claim["id"], DBCapabilityExecution.state == "running",
        DBCapabilityExecution.owner_token == claim["owner_token"], DBCapabilityExecution.worker_id == claim["worker_id"])
    batch_query = select(DBCapabilityBatch.__table__).where(DBCapabilityBatch.batch_id == claim["batch_id"])
    if locking:
        member_query, batch_query = member_query.with_for_update(), batch_query.with_for_update()
    member_row = db.execute(member_query).mappings().first()
    batch_row = db.execute(batch_query).mappings().first()
    member = SimpleNamespace(**member_row) if member_row else None
    batch = SimpleNamespace(**batch_row) if batch_row else None
    if (member is None or batch is None or member.lease_expires_at is None or member.heartbeat_at is None
            or member.lease_expires_at <= now or batch.deadline_at <= now
            or member.plan_id != claim["plan_id"] or member.binding_hash != claim["binding_hash"]
            or batch.requested_by != claim["actor"]["user_id"]):
        raise ExecutionLeaseLost()
    return member, batch


class ExecutionGuard:
    """Live checkpoint plus atomic fencing on every executor-session commit."""

    def __init__(self, claim, execution_db):
        self.claim, self.execution_db = claim, execution_db
        event.listen(execution_db, "before_commit", self._before_commit)

    def __call__(self, db=None):
        db = self.execution_db if db is None else db
        # A checkpoint must not carry write locks into handler I/O. Its possible race
        # is closed by _before_commit on the actual publication transaction.
        with Session(db.get_bind(), autoflush=False) as fresh:
            _owned(fresh, self.claim, locking=False)
            try:
                plan = plans._load(fresh, self.claim["plan_id"], self.claim["actor"], owner_only=True)
                if plans._hash(plan.capability_snapshot_json) != self.claim["binding_hash"]:
                    raise QueueError("batch_plan_changed")
                plans._check_plan(fresh, plan, self.claim["actor"])
                if reason := plan_validation.approval_block_reason(plan, db=fresh):
                    raise QueueError(reason)
            except (QueueError, plans.PlanError, registry.CapabilityError) as exc:
                raise ExecutionLeaseLost(exc.code) from exc
        return True

    def before_step(self, db, plan):
        with db.no_autoflush:
            return self._before_step_locked(db, plan)

    def _before_step_locked(self, db, plan):
        from app.services import plan_control
        lock_transaction(db, "capability_plan_state")
        lock_transaction(db, LOCK_KEY)
        _owned(db, self.claim)
        member = plan_control._member(db, self.claim["id"])
        if plan_control.boundary(db, member, plan):
            db.commit()
            return False
        return True

    def resume_state(self, db, plan):
        from app.services import plan_control
        lock_transaction(db, "capability_plan_state")
        lock_transaction(db, LOCK_KEY)
        _owned(db, self.claim)
        member = plan_control._member(db, self.claim["id"])
        if not member.control_enabled or not member.checkpoint_json:
            return None
        return plan_control.validate_checkpoint(db, member, plan)

    def _before_commit(self, db):
        with db.no_autoflush:
            self._check_commit(db)

    def _check_commit(self, db):
        from app.services import plan_control
        lock_transaction(db, "capability_plan_state")
        lock_transaction(db, LOCK_KEY)
        try:
            member, _ = _owned(db, self.claim)
            if member.control_enabled:
                from app.services import plan_control
                plan_control.check_target(db, member)
            for pending in db.dirty:
                if isinstance(pending, DBCapabilityPlan) and pending.plan_id == self.claim["plan_id"]:
                    if any(sa_inspect(pending).attrs[key].history.has_changes() for key in (
                        "plan_id", "requested_by", "instance_id", "capability_id",
                        "definition_version", "capability_snapshot_json",
                    )):
                        raise QueueError("batch_plan_changed")
            # Core projection avoids populate_existing wiping staged ORM outcomes.
            row = db.execute(select(DBCapabilityPlan.__table__).where(
                DBCapabilityPlan.plan_id == self.claim["plan_id"]).with_for_update()).mappings().first()
            if (row is None or row["requested_by"] != self.claim["actor"]["user_id"]
                    or plans._hash(row["capability_snapshot_json"]) != self.claim["binding_hash"]):
                raise QueueError("batch_plan_changed")
            plan = SimpleNamespace(**row)
            plans._check_plan(db, plan, self.claim["actor"])
            if reason := plan_validation.approval_block_reason(plan, db=db):
                raise QueueError(reason)
        except (ExecutionLeaseLost, QueueError, plans.PlanError, registry.CapabilityError, plan_control.ControlError) as exc:
            raise plans.PlanOwnershipLost("execution_ownership_lost") from exc

    def close(self):
        event.remove(self.execution_db, "before_commit", self._before_commit)


def _renew(session_factory, claim):
    with session_factory() as db:
        lock_transaction(db, LOCK_KEY)
        member, batch = _owned(db, claim)
        now = _now()
        count = db.query(DBCapabilityExecution).filter_by(
            id=member.id, state="running", owner_token=claim["owner_token"]).update(
                {"heartbeat_at": now, "lease_expires_at": min(
                    now + timedelta(seconds=LEASE_SECONDS), batch.deadline_at)}, synchronize_session=False)
        if count != 1:
            raise ExecutionLeaseLost()
        db.commit()


def _heartbeat(session_factory, claim, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            _renew(session_factory, claim)
        except ExecutionLeaseLost:
            return
        except SQLAlchemyError:
            # Loss of the store is not permission to extend the lease in memory.
            logger.warning("capability_queue_heartbeat_storage_unavailable member_id=%s", claim["id"])
            return


def _complete(session_factory, claim, failure=None):
    with session_factory() as db:
        lock_transaction(db, LOCK_KEY)
        member = db.query(DBCapabilityExecution).filter_by(
            id=claim["id"], state="running", owner_token=claim["owner_token"]).populate_existing().first()
        if member is None:
            boundary_member = db.get(DBCapabilityExecution, claim["id"])
            if (boundary_member and boundary_member.control_enabled and boundary_member.state in {"paused", "stopped"}
                    and boundary_member.worker_id == claim["worker_id"] and boundary_member.checkpoint_hash):
                plan = db.get(DBCapabilityPlan, claim["plan_id"])
                db.commit()
                return {"batch_id": claim["batch_id"], "plan_id": claim["plan_id"],
                        "queue_state": boundary_member.state, "status": plan.status,
                        "outcome": plan.outcome, "requires_attention": False}
            db.commit()
            return {"batch_id": claim["batch_id"], "plan_id": claim["plan_id"],
                    "queue_state": "ownership_lost", "requires_attention": True}
        plan = db.query(DBCapabilityPlan).filter_by(plan_id=claim["plan_id"]).populate_existing().first()
        if plan and plans._hash(plan.capability_snapshot_json) != claim["binding_hash"]:
            _finish(member, "reconciliation_required", "batch_plan_changed")
        elif _evidence(db, plan):
            _finish(member, "archived", "persisted_terminal_evidence")
        elif _unstarted(db, plan):
            if failure in {"execution_lease_lost", "execution_ownership_lost"}:
                batch = db.get(DBCapabilityBatch, claim["batch_id"])
                if batch.deadline_at <= _now():
                    _finish(member, "expired", "deadline_before_start")
                elif batch.cancel_requested_at:
                    _finish(member, "cancelled", "delivery_cancelled_before_start")
                else:
                    _finish(member, "queued", "unstarted_owner_recovered")
            else:
                _finish(member, "blocked", failure or "executor_did_not_start")
        else:
            uncertain = plan and (plan.outcome == "unknown" or any(
                step.status == "unknown" for step in plans._steps(db, plan.plan_id)))
            _finish(member, "reconciliation_required", failure or (
                "plan_outcome_uncertain" if uncertain else "executor_returned_without_terminal_evidence"))
        if member.control_enabled:
            from app.services import plan_control
            plan_control.complete(db, member, plan, certain=member.state == "archived")
        result = {"batch_id": claim["batch_id"], "plan_id": claim["plan_id"],
                  "queue_state": member.state, "status": plan.status if plan else None,
                  "outcome": plan.outcome if plan else None, "reason": member.reason,
                  "requires_attention": member.state in ATTENTION or bool(plan and plan.outcome == "unknown")}
        db.commit()
        if member.state == "archived" and plan and "execution_identity" in plan.capability_snapshot_json:
            from app.services import engine_execution
            bridge = engine_execution.bridge()
            if bridge is not None:
                try:
                    bridge.release_completed(plan)
                except Exception:
                    # A retained artifact reference is conservative: maintenance
                    # can reconcile it after a lost cross-store completion receipt.
                    logger.exception("engine_plan_reference_release_pending plan_id=%s", plan.plan_id)
        return result


def process_one(session_factory, *, worker_id: str, worker_identity: dict | None = None) -> dict | None:
    if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
        raise QueueError("invalid_worker_id")
    if worker_identity is not None:
        from app.runtime.execution_identity import validate_identity, ExecutionIdentityError
        try:
            worker_identity = dict(validate_identity(worker_identity))
        except ExecutionIdentityError as exc:
            raise QueueError(exc.code) from exc
    _require_guard()
    claim = (_claim(session_factory, worker_id) if worker_identity is None else
             _claim(session_factory, worker_id, worker_identity=worker_identity))
    if claim is None:
        return None
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(session_factory, claim, stop), daemon=True,
                                 name="capability-lease")
    heartbeat.start()
    failure = None
    try:
        with session_factory() as db:
            guard = ExecutionGuard(claim, db)
            try:
                plans.execute_plan(db, claim["plan_id"], actor=claim["actor"], execution_guard=guard)
            except ExecutionLeaseLost as exc:
                db.rollback()
                failure = exc.code
            except plans.PlanOwnershipLost as exc:
                db.rollback()
                failure = exc.code
            except Exception:
                db.rollback()
                # Do not save exception messages: handlers may include credentials or payloads.
                failure = "executor_interrupted"
            finally:
                guard.close()
    finally:
        stop.set()
        heartbeat.join()
    return _complete(session_factory, claim, failure)
