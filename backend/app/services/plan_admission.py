"""Prepare every explicit target on the server before any durable execution admission."""
from __future__ import annotations

import re
import uuid

from app.core.transaction_lock import lock_transaction
from app.models.capability_execution import DBCapabilityBatch
from app.models.capability_preparation import DBCapabilityPreparation
from app.services import capability_registry as registry, plan_service as plans


SUMMARY_FIELDS = ("plan_id", "instance_id", "revision", "binding_hash", "status", "outcome",
                  "approval_state", "approval_available", "expires_at", "reused")


def _key(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None):
        raise plans.PlanError("invalid_idempotency_key")
    return value


def _response(items):
    return {"schema": "capability.batch_preparation/v1", "accepted": False,
            "status": "prepared", "total": len(items), "items": items,
            "notice": "仅保存全部目标的冻结清单；尚未入队或执行，需复核/必要审批后明确提交批次。"}


def _read_receipt(db, receipt, actor):
    bindings = receipt.plan_bindings_json
    if (receipt.request_key_hash != plans._hash({"request_key": receipt.idempotency_key})
            or not isinstance(bindings, list) or not 1 <= len(bindings) <= 100 or len(bindings) != receipt.total
            or any(not isinstance(item, dict) or set(item) != {"plan_id", "instance_id", "revision", "binding_hash"}
                   or not isinstance(item["plan_id"], str) or re.fullmatch(r"pl[a-f0-9]{26}", item["plan_id"]) is None
                   or type(item["instance_id"]) is not int or item["instance_id"] <= 0
                   or type(item["revision"]) is not int or item["revision"] <= 0
                   or not isinstance(item["binding_hash"], str) or re.fullmatch(r"[a-f0-9]{64}", item["binding_hash"]) is None
                   for item in bindings)
            or len({item["plan_id"] for item in bindings}) != receipt.total):
        raise plans.PlanError("preparation_binding_changed")
    items = []
    for position, binding in enumerate(bindings):
        plan = plans._load(db, binding["plan_id"], actor, owner_only=True)
        original_key = plans._hash({"actor": receipt.requested_by, "client_key": plans._hash({
            "batch_key": receipt.idempotency_key, "position": position,
        })})
        if ("workflow_binding" in plan.capability_snapshot_json
                or plan.capability_snapshot_json.get("admission_hash") != receipt.input_hash
                or plan.idempotency_key != original_key):
            raise plans.PlanError("preparation_binding_changed")
        current = plans._serialize(db, plan, reused=True)
        if any(current[key] != binding[key] for key in binding):
            raise plans.PlanError("preparation_binding_changed")
        items.append({key: current[key] for key in SUMMARY_FIELDS})
    return _response(items)


def read_preparation(db, *, actor, idempotency_key):
    user, _, _ = registry._principal(db, actor)
    receipt = db.query(DBCapabilityPreparation).filter_by(
        requested_by=user.id, request_key_hash=plans._hash({"request_key": _key(idempotency_key)})).populate_existing().first()
    if receipt is None:
        raise plans.PlanError("preparation_not_found")
    return _read_receipt(db, receipt, actor)


def read_admission(db, *, actor, idempotency_key, schema_version=1):
    from app.services import plan_queue

    user, _, _ = registry._principal(db, actor)
    batch = db.query(DBCapabilityBatch.batch_id, DBCapabilityBatch.idempotency_key).filter_by(
        requested_by=user.id, idempotency_key=_key(idempotency_key)).first()
    if batch is None or batch.idempotency_key != idempotency_key:
        raise plans.PlanError("batch_not_found")
    return plan_queue.read_batch(db, batch.batch_id, actor=actor, schema_version=schema_version)


def prepare_batch(db, *, actor, capability_id, definition_version, targets,
                  idempotency_key, steps=None, valid_for_seconds=3600, request_approval=False):
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 100
            or any(not isinstance(target, dict) or set(target) - {"instance_id", "params"}
                   or type(target.get("instance_id")) is not int or target["instance_id"] <= 0
                   or not isinstance(target.get("params", {}), dict) for target in targets)
            or len({target["instance_id"] for target in targets}) != len(targets)):
        raise plans.PlanError("invalid_batch_targets")
    _key(idempotency_key)
    if steps is not None and any(target.get("params") for target in targets):
        raise plans.PlanError("ambiguous_plan_params")
    if db.new or db.dirty or db.deleted:
        raise plans.PlanError("preparation_dirty_session")
    user, _, _ = registry._principal(db, actor)
    admission_hash = plans._hash({"capability_id": capability_id, "definition_version": definition_version,
                                  "targets": targets, "steps": steps, "valid_for_seconds": valid_for_seconds,
                                  "request_approval": request_approval})
    try:
        lock_transaction(db, "capability_plan_create")
        prior = db.query(DBCapabilityPreparation).filter_by(
            requested_by=user.id, request_key_hash=plans._hash({"request_key": idempotency_key})).populate_existing().first()
        if prior:
            if prior.input_hash != admission_hash:
                raise plans.PlanError("idempotency_conflict")
            value = _read_receipt(db, prior, actor)
            db.commit()
            return value
        items = []
        for position, target in enumerate(targets):
            value = plans.create_plan(
                db, actor=actor, capability_id=capability_id, definition_version=definition_version,
                instance_id=target["instance_id"], params=target.get("params", {}), steps=steps,
                idempotency_key=plans._hash({"batch_key": idempotency_key, "position": position}),
                valid_for_seconds=valid_for_seconds, request_approval=request_approval, commit=False,
                admission_hash=admission_hash,
            )
            items.append({key: value[key] for key in SUMMARY_FIELDS})
        db.add(DBCapabilityPreparation(
            preparation_id="CP" + uuid.uuid4().hex[:28], requested_by=user.id,
            idempotency_key=idempotency_key, request_key_hash=plans._hash({"request_key": idempotency_key}),
            input_hash=admission_hash, total=len(items),
            created_at=plans._now(), plan_bindings_json=[{
                key: item[key] for key in ("plan_id", "instance_id", "revision", "binding_hash")
            } for item in items],
        ))
        db.commit()
        return _response(items)
    except BaseException:
        db.rollback()
        raise
