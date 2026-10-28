"""Revision-bound human decisions over the existing typed plan and audit ledger."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from app.core.transaction_lock import lock_transaction
from app.models.access_control import SecurityAuditEvent
from app.models.capability_plan import DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.services import access_control, capability_registry as registry, plan_service as plans, plan_validation
from app.services import workflow_binding as workflow_bindings


def _revision(plan):
    return plan.capability_snapshot_json.get("revision", 1)


def _approval_view(db, plan, actor):
    value = plans._serialize(db, plan)
    # Consent metadata has a different permission contract from business results.
    # Even an owner must use the ordinary authorized detail route for result data.
    value["result_ref"] = None
    for step in value["steps"]:
        step["result_ref"] = None
    return value


def _expect(plan, expected_revision):
    if type(expected_revision) is not int or expected_revision != _revision(plan):
        raise plans.PlanError("plan_revision_conflict")


def _audit(db, plan, actor, action, **details):
    user, _, _ = registry._principal(db, actor)
    event = access_control.append_security_audit(
        db, category="capability_plan", action=action, actor=user.username, actor_user_id=user.id,
        target_type="capability_plan", target_id=plan.plan_id,
        details={"revision": _revision(plan), "binding_hash": plans._hash(plan.capability_snapshot_json), **details},
    )
    if event is None:
        raise plans.PlanError("plan_audit_unavailable")


def _decisions(db, plan):
    if db is None:
        return []
    return [row for row in db.query(SecurityAuditEvent).filter_by(
        category="capability_plan", target_type="capability_plan", target_id=plan.plan_id,
        action="approve_plan", outcome="success").order_by(SecurityAuditEvent.id).all()
        if isinstance(row.details, dict) and row.details.get("revision") == _revision(plan)
        and row.details.get("binding_hash") == plans._hash(plan.capability_snapshot_json)]


def _approver_current(db, actor_id, plan):
    try:
        user, grants, scope = registry._principal(db, {"user_id": actor_id})
        return (user.id != plan.requested_by and access_control.action_matches(grants, "automation.approve")
                and (not scope or not scope.restricted or plan.instance_id in scope.native_instance_ids))
    except registry.CapabilityError:
        return False


def required_approvals(plan):
    return 2 if any(entry.get("risk") == "high" for entry in plan.capability_snapshot_json["definitions"]) else 1


def approval_problem(db, plan):
    if plan.revoked_at is not None or plan.approval_state == "revoked":
        return "approval_revoked"
    if plan.approval_state != "approved":
        return "approval_required"
    if (plan.approval_binding_hash != plans._hash(plan.capability_snapshot_json)
            or plan.approved_by is None or plan.approved_at is None or plan.approval_expires_at is None
            or plan.approved_by == plan.requested_by):
        return "approval_binding_invalid"
    if plan.approval_expires_at <= plans._now():
        return "approval_expired"
    decisions = _decisions(db, plan)
    if (not decisions or decisions[-1].actor_user_id != plan.approved_by
            or decisions[-1].details.get("approved_at") != plan.approved_at.isoformat() + "Z"
            or decisions[-1].details.get("expires_at") != plan.approval_expires_at.isoformat() + "Z"):
        return "approval_binding_invalid"
    identities = {row.actor_user_id for row in decisions}
    if len(identities) < required_approvals(plan) or not all(_approver_current(db, identity, plan) for identity in identities):
        return "approval_authority_revoked"
    return None


def request_approval(db, plan_id, *, actor, expected_revision):
    lock_transaction(db, "capability_plan_state")
    plan = plans._load(db, plan_id, actor, owner_only=True)
    _expect(plan, expected_revision)
    if plan.started_at is not None or plan.status not in {"validated", "awaiting_approval", "approved"}:
        raise plans.PlanError("invalid_plan_state")
    if not plan_validation.requires_approval(plan):
        raise plans.PlanError("approval_not_required")
    plans._check_plan(db, plan, actor)
    if any(not registry.effective_capability(db, entry["capability_id"], definition_version=entry["version"])["plan_executable"]
           for entry in plan.capability_snapshot_json["definitions"]):
        raise plans.PlanError("handler_not_wired")
    if plan.status in {"awaiting_approval", "approved"}:
        db.commit()
        return plans._serialize(db, plan, reused=True)
    if plan.approval_state == "revoked":
        raise plans.PlanError("approval_revoked_requires_revision")
    plan.status, plan.approval_state = "awaiting_approval", "pending"
    plan.approval_binding_hash = plans._hash(plan.capability_snapshot_json)
    _audit(db, plan, actor, "request_plan_approval")
    db.commit()
    return _approval_view(db, plan, actor)


def approve_plan(db, plan_id, *, actor, expected_revision, expected_binding_hash, valid_for_seconds=900):
    if type(valid_for_seconds) is not int or not 60 <= valid_for_seconds <= 3600:
        raise plans.PlanError("invalid_validity_policy")
    lock_transaction(db, "capability_plan_state")
    plan = plans._load(db, plan_id, actor, for_approval=True)
    _expect(plan, expected_revision)
    if plan.status != "awaiting_approval" or plan.approval_state != "pending" or plan.revoked_at is not None:
        raise plans.PlanError("invalid_approval_state")
    user, _, _ = registry._principal(db, actor)
    if user.id == plan.requested_by:
        raise plans.PlanError("self_approval_forbidden")
    binding = plans._hash(plan.capability_snapshot_json)
    if expected_binding_hash != binding or plan.approval_binding_hash != binding:
        raise plans.PlanError("approval_binding_invalid")
    plans._check_plan(db, plan, {"user_id": plan.requested_by})
    if any(not registry.effective_capability(db, entry["capability_id"], definition_version=entry["version"])["plan_executable"]
           for entry in plan.capability_snapshot_json["definitions"]):
        raise plans.PlanError("handler_not_wired")
    prior = _decisions(db, plan)
    if user.id in {row.actor_user_id for row in prior}:
        raise plans.PlanError("duplicate_approval")
    if prior and not all(_approver_current(db, row.actor_user_id, plan) for row in prior):
        raise plans.PlanError("approval_authority_revoked")
    # Generic MySQL DATETIME stores whole seconds; the audited binding must survive readback.
    observed_now = plans._now()
    now = observed_now.replace(microsecond=0)
    deadline = plan_validation.validity_deadline(plan.capability_snapshot_json)
    expires = min(now + timedelta(seconds=valid_for_seconds), deadline) if deadline else now + timedelta(seconds=valid_for_seconds)
    if prior:
        # A second approval must not extend the first person's authorization window.
        prior_expiry = plan.approval_expires_at
        if prior_expiry is None or prior_expiry <= observed_now:
            raise plans.PlanError("approval_expired")
        expires = min(expires, prior_expiry)
    expires = expires.replace(microsecond=0)
    if expires <= observed_now:
        raise plans.PlanError("approval_expired")
    plan.approved_by, plan.approved_at, plan.approval_expires_at = user.id, now, expires
    if len(prior) + 1 >= required_approvals(plan):
        plan.approval_state, plan.status = "approved", "approved"
    _audit(db, plan, actor, "approve_plan", approved_at=now.isoformat() + "Z",
           expires_at=expires.isoformat() + "Z", required_approvals=required_approvals(plan))
    db.commit()
    return _approval_view(db, plan, actor)


def revoke_approval(db, plan_id, *, actor, expected_revision):
    lock_transaction(db, "capability_plan_state")
    user, grants, _ = registry._principal(db, actor)
    as_approver = access_control.action_matches(grants, "automation.approve")
    plan = plans._load(db, plan_id, actor, owner_only=not as_approver, for_approval=as_approver)
    _expect(plan, expected_revision)
    if plan.status == "settled" or not plan_validation.requires_approval(plan):
        raise plans.PlanError("invalid_approval_state")
    if plan.approval_state == "revoked":
        db.commit()
        return {**_approval_view(db, plan, actor), "reused": True}
    plan.approval_state, plan.revoked_at, plan.revoked_by = "revoked", plans._now(), user.id
    if plan.status != "running":
        plan.status = "validated"
    _audit(db, plan, actor, "revoke_plan_approval", running=plan.status == "running")
    db.commit()
    return _approval_view(db, plan, actor)


def read_approval(db, plan_id, *, actor):
    _, grants, _ = registry._principal(db, actor)
    plan = plans._load(db, plan_id, actor, for_approval=access_control.action_matches(grants, "automation.approve"))
    if actor["user_id"] != plan.requested_by and not plan_validation.requires_approval(plan):
        raise plans.PlanError("plan_not_found")
    decisions = _decisions(db, plan)
    return {"schema": "capability.plan_approval/v1", "plan": _approval_view(db, plan, actor),
            "required_approvals": required_approvals(plan),
            "decisions": [{"actor_id": row.actor_user_id, "approved_at": row.details["approved_at"],
                           "expires_at": row.details["expires_at"]} for row in decisions]}


def revise_plan(db, plan_id, *, actor, expected_revision, capability_id, definition_version,
                instance_id=None, params=None, steps=None, valid_for_seconds=3600, request_approval=False,
                workflow_binding=None):
    if type(valid_for_seconds) is not int or not 60 <= valid_for_seconds <= 86400 or type(request_approval) is not bool:
        raise plans.PlanError("invalid_validity_policy")
    lock_transaction(db, "capability_plan_state")
    plan = plans._load(db, plan_id, actor, owner_only=True)
    _expect(plan, expected_revision)
    if "workflow_binding" in plan.capability_snapshot_json and workflow_binding is None:
        raise plans.PlanError("workflow_revision_requires_recipe")
    if workflow_binding is not None:
        try:
            workflow_binding = workflow_bindings.validate_request(
                workflow_binding, capability_id=capability_id, definition_version=definition_version,
                instance_id=instance_id, params=params, steps=steps, valid_for_seconds=valid_for_seconds,
                request_approval=request_approval)
        except workflow_bindings.WorkflowBindingError as exc:
            raise plans.PlanError(exc.code) from exc
    if plan.started_at is not None or plan.result_ref_json is not None or plan.outcome in plans.EXECUTED_OUTCOMES:
        raise plans.PlanError("executed_plan_requires_rerun")
    # A durable queue binding is immutable. Cancel it before editing its plan.
    from app.services.plan_queue import assert_plan_editable
    assert_plan_editable(db, plan_id)
    user, instance, definitions, schemas, normalized = plans.compile_inputs(
        db, actor=actor, capability_id=capability_id, definition_version=definition_version,
        instance_id=instance_id, params=params, steps=steps)
    if any(step["definition_version"] != entry["version"] for step, entry in zip(normalized, definitions)):
        raise plans.PlanError("stale_definition_version")
    workflow_identity = (plans._workflow_request_identity(
        user_id=user.id, capability_id=capability_id, instance_id=instance_id,
        target=plans.target_fingerprint(instance) if instance else None, steps=normalized,
        binding=workflow_binding, valid_for_seconds=valid_for_seconds, request_approval=request_approval)
        if workflow_binding is not None else None)
    normalized = plans.freeze_steps(db, instance, normalized, definitions)
    try:
        normalized = plan_validation.validate_step_contracts(normalized, definitions, schemas,
                                                             engine=instance.db_type if instance else None)
    except plan_validation.PlanValidationError as exc:
        raise plans.PlanError(exc.code) from exc
    now = plans._now()
    target = plans.target_fingerprint(instance) if instance else None
    business = [{key: value for key, value in step.items() if key not in {"definition_version", "title"}} for step in normalized]
    snapshot = {"schema": "capability.plan/v1", "revision": expected_revision + 1,
                "definitions": definitions, "steps": normalized, "failure_policy": "stop",
                "request_hash": plans._hash({"actor": user.id, "capability_id": capability_id,
                                             "instance_id": instance_id, "target": target, "steps": business}),
                "target_fingerprint": target,
                "identity": {"requested_by": user.id, "instance_id": instance_id,
                             "capability_id": capability_id, "definition_version": definition_version},
                "validity": {"seconds": valid_for_seconds, "expires_at": (now + timedelta(seconds=valid_for_seconds)).isoformat() + "Z"}}
    if workflow_binding is not None:
        snapshot["workflow_binding"] = deepcopy(workflow_binding)
        snapshot["request_hash"] = plans._hash(workflow_identity)
    if request_approval:
        snapshot["approval_requested"] = True
    db.query(DBCapabilityPlanStep).filter_by(plan_id=plan_id).delete(synchronize_session=False)
    plan.capability_id, plan.definition_version, plan.instance_id = capability_id, definition_version, instance_id
    plan.db_type, plan.capability_snapshot_json = instance.db_type if instance else None, snapshot
    plan.status, plan.outcome, plan.outcome_kind, plan.outcome_reason = "draft", None, None, None
    plan.outcome_declared_by, plan.finished_at = None, None
    required = plan_validation.requires_approval(plan)
    plan.approval_state, plan.approval_binding_hash = ("pending", plans._hash(snapshot)) if required else ("not_required", None)
    for field in ("approved_by", "approved_at", "approval_expires_at", "revoked_at", "revoked_by"):
        setattr(plan, field, None)
    for step, entry in zip(normalized, definitions):
        db.add(DBCapabilityPlanStep(plan_id=plan_id, seq=step["seq"], title=step["title"],
                                   capability_id=entry["capability_id"], depends_on_json=step["depends_on"],
                                   side_effect="write" if entry["kind"] in plans.APPROVAL_KINDS else "read",
                                   precondition_json=deepcopy(step), status="pending"))
    db.add(DBCapabilityPlanRevision(plan_id=plan_id, revision=expected_revision + 1, snapshot_json=deepcopy(snapshot),
                                    binding_hash=plans._hash(snapshot), created_at=now, created_by=user.id))
    db.flush()
    try:
        plans._check_plan(db, plan, actor)
    except (plans.PlanError, registry.CapabilityError) as exc:
        plans._reject_validation(db, plan, exc)
    else:
        plan.status = "validated"
    _audit(db, plan, actor, "revise_plan", previous_revision=expected_revision)
    db.commit()
    return plans._serialize(db, plan)
