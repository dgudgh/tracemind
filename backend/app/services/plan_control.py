"""Durable local step-boundary control. Never claims remote interruption/fencing."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from sqlalchemy import select

from app.core.transaction_lock import lock_transaction
from app.models.capability_control import DBCapabilityControlCommand as Command, DBCapabilityTargetFence as Fence
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.services import access_control, capability_registry as registry, plan_service as plans, plan_validation

LOCK_KEY = "capability_queue"
SUPPORTED = frozenset({"inspection.run.query", "inspection.template.read",
                       "operations.capability_matrix.read", "inspection.run.execute"})


class ControlError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _locks(db):
    lock_transaction(db, "capability_plan_state")
    lock_transaction(db, LOCK_KEY)


def _member(db, execution_id, *, locking=True):
    query = db.query(DBCapabilityExecution).filter_by(id=execution_id).populate_existing()
    member = (query.with_for_update() if locking else query).first()
    if member is None:
        raise ControlError("execution_not_found")
    return member


def _authorize(db, member, actor, action=None):
    user, grants, _ = registry._principal(db, actor)
    plan = plans._load(db, member.plan_id, actor, owner_only=True)
    batch = db.get(DBCapabilityBatch, member.batch_id)
    target_key = f"instance:{plan.instance_id}" if plan.instance_id is not None else f"global:{plan.capability_id}"
    if (plan.requested_by != user.id or batch is None or batch.requested_by != user.id
            or plans._hash(plan.capability_snapshot_json) != member.binding_hash
            or plan.capability_snapshot_json.get("revision", 1) != member.plan_revision or member.target_key != target_key):
        raise ControlError("execution_binding_changed")
    if action and not registry.access_control.action_matches(grants, "control." + action):
        raise ControlError("permission_denied")
    if user.role != "admin":
        scope = registry.access_control.resolve_scope(db, user.id, minimum_access="operate")
        if scope.restricted and (plan.instance_id is None or plan.instance_id not in scope.native_instance_ids):
            raise ControlError("scope_denied")
    return plan


def _fence(db, member):
    row = db.query(Fence).filter_by(target_key=member.target_key).with_for_update().first()
    if row is None:
        row = Fence(target_key=member.target_key, epoch=1, row_version=0, hold_state="unclaimed",
                    updated_at=_now(), hold_reason="Local generation only; no external fence")
        db.add(row)
        db.flush()
    return row


def acquire_target(db, member):
    """Caller owns queue lock; each local claim gets a new retained target epoch."""
    row = _fence(db, member)
    if row.hold_state == "legacy_unverified" or (
            row.holder_execution_id is not None and row.holder_execution_id != member.id):
        raise ControlError("target_occupied")
    row.epoch += 1
    row.row_version += 1
    row.hold_state, row.holder_execution_id = "held", member.id
    row.holder_plan_id, row.binding_hash = member.plan_id, member.binding_hash
    row.acquired_at = row.updated_at = _now()
    row.released_at = row.release_evidence_ref = None
    member.fencing_epoch = row.epoch


def check_target(db, member):
    if not member.control_enabled:
        return
    row = db.execute(select(Fence.__table__).where(Fence.target_key == member.target_key).with_for_update()).mappings().first()
    if (row is None or row["holder_execution_id"] != member.id or row["epoch"] != member.fencing_epoch
            or row["binding_hash"] != member.binding_hash or row["hold_state"] not in {"held", "stop_pending"}):
        raise ControlError("execution_epoch_lost")


def _release(db, member):
    row = _fence(db, member)
    if member.fencing_epoch > 0 and (row.holder_execution_id != member.id
            or row.epoch != member.fencing_epoch or row.binding_hash != member.binding_hash):
        raise ControlError("execution_epoch_lost")
    if row.holder_execution_id == member.id:
        if row.epoch != member.fencing_epoch or row.binding_hash != member.binding_hash:
            raise ControlError("execution_epoch_lost")
        row.holder_execution_id = row.holder_plan_id = row.binding_hash = None
        row.hold_state, row.released_at, row.updated_at = "released", _now(), _now()
        row.row_version += 1
        row.release_evidence_ref = {"kind": "checkpoint", "id": str(member.id),
                                     "schema": "capability.checkpoint/v1", "sha256": member.checkpoint_hash}


def _steps_evidence(db, plan):
    rows = plans._steps(db, plan.plan_id)
    definitions = {step["seq"]: entry for step, entry in zip(
        plan.capability_snapshot_json["steps"], plan.capability_snapshot_json["definitions"])}
    outputs, failure = {}, None
    pending_seen = False
    for row in rows:
        if row.status == "pending":
            pending_seen = True
            if row.started_at is not None or row.result_ref_json is not None:
                raise ControlError("checkpoint_invalid")
            continue
        if pending_seen or row.status not in {"succeeded", "partial", "failed", "cancelled", "expired", "skipped"} or not row.finished_at:
            raise ControlError("checkpoint_uncertain")
        ref = row.result_ref_json
        if row.status in {"succeeded", "partial", "cancelled"} and not ref:
            raise ControlError("checkpoint_evidence_missing")
        if ref is not None:
            try:
                if ref != plans._result_ref(definitions[row.seq], ref.get("inline")):
                    raise ControlError("checkpoint_evidence_invalid")
                if ref["kind"] == "inspection_job":
                    plans.validate_effect_reference(db, plan, row)
                    actual = ref["inline"]["outcome"]
                    if ("succeeded" if actual == "success" else actual) != row.status:
                        raise ControlError("checkpoint_uncertain")
            except (plans.PlanError, KeyError, TypeError, AttributeError) as exc:
                raise ControlError("checkpoint_evidence_invalid") from exc
        if row.status in {"succeeded", "partial"}:
            outputs[row.seq] = deepcopy(ref["inline"])
        if row.status != "succeeded":
            failure = failure or row.status
    return rows, outputs, failure


def _checkpoint(db, member, plan):
    rows, outputs, failure = _steps_evidence(db, plan)
    batch = db.get(DBCapabilityBatch, member.batch_id)
    value = {"schema": "capability.checkpoint/v1", "execution_id": member.id, "plan_id": member.plan_id,
             "revision": member.plan_revision, "binding_hash": member.binding_hash, "epoch": member.fencing_epoch,
             "deadline": batch.deadline_at.isoformat(), "steps": [
                 {"seq": row.seq, "status": row.status, "result_ref": deepcopy(row.result_ref_json),
                  "started_at": row.started_at.isoformat() if row.started_at else None,
                  "finished_at": row.finished_at.isoformat() if row.finished_at else None} for row in rows],
             "outputs": {str(seq): result for seq, result in outputs.items()}, "failure": failure}
    if len(plans._json(value)) > 2097152:
        raise ControlError("checkpoint_budget_exceeded")
    member.checkpoint_json, member.checkpoint_hash, member.checkpoint_at = value, plans._hash(value), _now()
    return outputs, failure


def validate_checkpoint(db, member, plan):
    previous, digest, at = member.checkpoint_json, member.checkpoint_hash, member.checkpoint_at
    if not previous or not digest or not at or plans._hash(previous) != digest:
        raise ControlError("checkpoint_missing_or_changed")
    # Rebuild only from current persisted step/domain evidence, never caller-provided outputs.
    outputs, failure = _checkpoint(db, member, plan)
    rebuilt = member.checkpoint_json
    member.checkpoint_json, member.checkpoint_hash, member.checkpoint_at = previous, digest, at
    if rebuilt != previous:
        raise ControlError("checkpoint_changed")
    return outputs, failure


def resumable(db, member, plan):
    return bool(member.control_enabled and member.checkpoint_json and member.desired_state == "run"
                and member.observed_state == "paused" and plan.status in {"running", "validated", "approved"})


def on_claim(db, member, plan):
    if not member.control_enabled:
        return
    if resumable(db, member, plan):
        _authorize(db, member, {"user_id": plan.requested_by}, "resume")
        validate_checkpoint(db, member, plan)
    acquire_target(db, member)
    _checkpoint(db, member, plan)
    member.observed_state = "running"
    command = db.get(Command, member.control_command_id) if member.control_command_id else None
    if command and command.action == "resume":
        _ack(db, member, "resumed", "running")


def _ack(db, member, stage, observed):
    if member.control_command_id:
        command = db.get(Command, member.control_command_id)
        if command and command.status in {"accepted", "sent", "acknowledged"}:
            command.status, command.ack_stage, command.observed_state = "applied", stage, observed
            command.acknowledged_at = command.applied_at = _now()
            command.ack_evidence_ref = {"kind": "checkpoint", "id": str(member.id),
                                        "schema": "capability.checkpoint/v1", "sha256": member.checkpoint_hash}


def boundary(db, member, plan):
    """Called under plan->queue locks before a new step intent, never during handler I/O."""
    if not member.control_enabled or member.desired_state == "run":
        return False
    _checkpoint(db, member, plan)
    if member.desired_state == "pause":
        member.state, member.observed_state = "paused", "paused"
        row = _fence(db, member)
        if member.fencing_epoch > 0 and (row.holder_execution_id != member.id
                or row.epoch != member.fencing_epoch or row.binding_hash != member.binding_hash):
            raise ControlError("execution_epoch_lost")
        if row.holder_execution_id == member.id:
            row.hold_state, row.updated_at = "paused", _now()
        _ack(db, member, "quiesced", "paused")
    else:
        member.state, member.observed_state, member.finished_at = "stopped", "stopped", _now()
        _release(db, member)
        _ack(db, member, "not_started" if plan.started_at is None else "terminated", "stopped")
    member.owner_token = member.lease_expires_at = None
    return True


def complete(db, member, plan, *, certain):
    if not member.control_enabled:
        return
    if not certain:
        member.observed_state = "unknown"
        row = _fence(db, member)
        if row.holder_execution_id == member.id:
            row.hold_state, row.updated_at = "reconciliation_required", _now()
        return
    _checkpoint(db, member, plan)
    member.observed_state = "completed"
    _release(db, member)
    if member.control_command_id:
        command = db.get(Command, member.control_command_id)
        if command and command.status in {"accepted", "sent", "acknowledged"}:
            command.status, command.ack_stage, command.observed_state = "superseded", "completed", "completed"
            command.acknowledged_at = _now()
            command.ack_evidence_ref = {"kind": "checkpoint", "id": str(member.id),
                                        "schema": "capability.checkpoint/v1", "sha256": member.checkpoint_hash}


def cancel_unstarted_delivery(db, member, plan):
    if not member.control_enabled:
        return
    _checkpoint(db, member, plan)
    member.observed_state = "stopped"
    _release(db, member)
    command = db.get(Command, member.control_command_id) if member.control_command_id else None
    if command and command.status in {"accepted", "sent", "acknowledged"}:
        command.status, command.ack_stage, command.observed_state = "superseded", "not_started", "stopped"
        command.acknowledged_at = _now()
        command.ack_evidence_ref = {"kind": "checkpoint", "id": str(member.id),
                                    "schema": "capability.checkpoint/v1", "sha256": member.checkpoint_hash}


def _view(db, member, *, command=None, reused=False):
    command = command or (db.get(Command, member.control_command_id) if member.control_command_id else None)
    return {"schema": "capability.control/v1", "execution_id": member.id, "plan_id": member.plan_id,
            "control_enabled": member.control_enabled, "control_version": member.control_version,
            "desired_state": member.desired_state, "observed_state": member.observed_state,
            "queue_state": member.state, "checkpoint_hash": member.checkpoint_hash,
            "fencing_epoch": member.fencing_epoch, "scope": "local_step_boundary",
            "external_stop": "unsupported", "external_fence": "unsupported", "reused": reused,
            "command": ({"command_id": command.command_id, "action": command.action,
                         "status": command.status, "ack_stage": command.ack_stage,
                         "control_version": command.control_version} if command else None)}


def read_control(db, execution_id: int, *, actor: dict, idempotency_key: str | None = None) -> dict:
    if type(execution_id) is not int or execution_id <= 0:
        raise ControlError("invalid_execution_id")
    member = _member(db, execution_id, locking=False)
    plan = _authorize(db, member, actor, "probe")
    if idempotency_key is None:
        return _view(db, member)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ControlError("invalid_idempotency_key")
    command = db.query(Command).filter_by(requested_by=plan.requested_by,
                                          idempotency_key=idempotency_key).populate_existing().first()
    if (command is None or command.idempotency_key != idempotency_key
            or command.execution_id != member.id or command.plan_id != member.plan_id
            or command.plan_revision != member.plan_revision or command.binding_hash != member.binding_hash):
        raise ControlError("control_command_not_found")
    return _view(db, member, command=command)


def request_control(db, execution_id: int, *, actor: dict, action: str,
                    expected_control_version: int, idempotency_key: str, reason: str) -> dict:
    if (type(execution_id) is not int or not 0 < execution_id < 2**63
            or not isinstance(action, str) or action not in {"pause", "resume", "stop", "probe"}
            or type(expected_control_version) is not int or not 0 <= expected_control_version < 2**63 - 1
            or not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 300):
        raise ControlError("invalid_control_request")
    if db.new or db.dirty or db.deleted:
        raise ControlError("control_requires_clean_session")
    try:
        db.rollback()
        _locks(db)
        member = _member(db, execution_id)
        plan = _authorize(db, member, actor, action)
        fingerprint = plans._hash({"execution_id": execution_id, "action": action,
                                   "expected_control_version": expected_control_version, "reason": reason})
        previous = db.query(Command).filter_by(requested_by=plan.requested_by,
                                               idempotency_key=idempotency_key).populate_existing().first()
        if previous:
            if (previous.idempotency_key != idempotency_key or previous.input_hash != fingerprint
                    or previous.execution_id != member.id or previous.plan_id != member.plan_id
                    or previous.plan_revision != member.plan_revision or previous.binding_hash != member.binding_hash):
                raise ControlError("idempotency_conflict")
            value = _view(db, member, command=previous, reused=True)
            db.commit()
            return value
        if member.control_version != expected_control_version:
            raise ControlError("control_version_conflict")
        batch = db.get(DBCapabilityBatch, member.batch_id)
        if batch.deadline_at <= _now() or (deadline := plan_validation.validity_deadline(plan.capability_snapshot_json)) and deadline <= _now():
            raise ControlError("control_deadline_expired")
        if any(entry["capability_id"] not in SUPPORTED for entry in plan.capability_snapshot_json["definitions"]):
            raise ControlError("control_unsupported")
        if action != "probe" and (member.state == "reconciliation_required" or member.observed_state == "unknown"
                or any(row.status == "unknown" for row in plans._steps(db, plan.plan_id))):
            raise ControlError("execution_requires_reconciliation")
        if action != "probe" and member.state not in {"queued", "running", "paused"}:
            raise ControlError("execution_not_controllable")
        if member.desired_state == "stop" and action not in {"stop", "probe"}:
            raise ControlError("stop_request_is_irreversible")
        if action == "resume":
            if batch.cancel_requested_at:
                raise ControlError("batch_delivery_cancelled")
            if member.state != "paused":
                raise ControlError("execution_not_paused")
            plans._check_plan(db, plan, actor)
            if reason_code := plan_validation.approval_block_reason(plan, db=db):
                raise ControlError(reason_code)
            validate_checkpoint(db, member, plan)
        if not member.control_enabled and action != "probe":
            member.control_enabled = True
            if member.state == "running":
                acquire_target(db, member)
        row = _fence(db, member)
        if row.epoch is None:
            raise ControlError("target_fence_unverified")
        command = Command(command_id="CC" + uuid.uuid4().hex[:28], execution_id=member.id,
                          plan_id=member.plan_id, plan_revision=member.plan_revision, binding_hash=member.binding_hash,
                          target_key=member.target_key, action=action, requested_by=plan.requested_by, reason=reason,
                          created_at=_now(), deadline_at=batch.deadline_at,
                          expected_control_version=expected_control_version, control_version=expected_control_version + 1,
                          fencing_epoch=row.epoch, idempotency_key=idempotency_key, input_hash=fingerprint)
        if member.control_command_id and action != "probe":
            older = db.get(Command, member.control_command_id)
            if older and older.status in {"accepted", "sent", "acknowledged"}:
                older.status = "superseded"
        db.add(command)
        changed = db.query(DBCapabilityExecution).filter_by(
            id=member.id, control_version=expected_control_version).update(
                {"control_version": expected_control_version + 1}, synchronize_session=False)
        if changed != 1:
            raise ControlError("control_version_conflict")
        member.control_version = expected_control_version + 1
        if action != "probe":
            member.control_command_id = command.command_id
        db.flush()
        if action == "probe":
            command.status, command.ack_stage, command.observed_state = "applied", "observed", member.observed_state or "unknown"
            command.acknowledged_at = command.applied_at = _now()
            command.ack_evidence_ref = {"kind": "target_observation", "id": str(member.id),
                                        "schema": "capability.local_observation/v1"}
        elif action == "resume":
            member.state, member.desired_state, member.observed_state = "queued", "run", "paused"
        else:
            member.desired_state = "pause" if action == "pause" else "stop"
            if member.state in {"queued", "paused"}:
                boundary(db, member, plan)
        db.flush()
        user, _, _ = registry._principal(db, actor)
        audit = access_control.append_security_audit(
            db, category="capability_control", action="control_" + action,
            actor=user.username, actor_user_id=user.id, target_type="capability_execution", target_id=member.id,
            details={"plan_id": member.plan_id, "revision": member.plan_revision, "binding_hash": member.binding_hash,
                     "command_id": command.command_id, "control_version": member.control_version,
                     "action": action, "reason": reason, "scope": "local_step_boundary",
                     "idempotency_key": idempotency_key, "input_hash": fingerprint},
        )
        if audit is None:
            raise ControlError("control_audit_unavailable")
        value = _view(db, member, command=command)
        db.commit()
        return value
    except BaseException:
        db.rollback()
        raise
