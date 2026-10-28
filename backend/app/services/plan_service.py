"""Explicit, bounded read workflows. No approval, external executor or model is trusted here."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import logging
import re
from time import monotonic
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy import and_, event, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.transaction_lock import lock_transaction
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.capability_effect_receipt import DBCapabilityEffectReceipt
from app.models.db_instance import DBInstance
from app.services import capability_registry as registry
from app.services.capability_effect_contract import (
    COMMON_EFFECT_CAPABILITIES,
    EFFECT_RESULT_SCHEMA,
    EFFECT_RESULT_VERSION,
    digest as effect_digest,
)
from app.services.inspection.jobs import target_fingerprint
from app.services.plan_dag import PlanDAGError, normalize_steps, resolve_inputs
from app.services import plan_validation, workflow_binding as workflow_bindings
from app.services import engine_execution


logger = logging.getLogger(__name__)
STATUSES = frozenset({"draft", "validated", "awaiting_approval", "approved", "running", "settled"})
DECLARED_OUTCOMES = frozenset({"plan_only", "blocked", "unsupported", "needs_input"})
EXECUTED_OUTCOMES = frozenset({"succeeded", "partial", "failed", "cancelled", "expired", "unknown"})
OUTCOMES = DECLARED_OUTCOMES | EXECUTED_OUTCOMES
APPROVAL_KINDS = plan_validation.APPROVAL_KINDS
READ_CONTRACTS = {
    "inspection.run.query": {"output_fields": {"total": "integer", "page": "integer", "page_size": "integer"},
                             "input_types": {"page": "integer", "page_size": "integer", "status": "string",
                                             "outcome": "string", "trigger_type": "string"}},
    "inspection.template.read": {"output_fields": {"total": "integer", "page": "integer", "page_size": "integer"},
                                 "input_types": {"page": "integer", "page_size": "integer", "db_type": "string", "enabled": "boolean"}},
    "operations.capability_matrix.read": {"output_fields": {}, "input_types": {"engine": "string"}},
    # C13 batch 1: the six live read adapters. Overview-shaped readers expose no knobs; the
    # paginated ones window the list their service already returned.
    "operations.runtime_overview.read": {"output_fields": {}, "input_types": {}},
    "operations.space.read": {"output_fields": {}, "input_types": {}},
    "operations.log.read": {"output_fields": {}, "input_types": {}},
    "operations.session.read": {"output_fields": {"total": "integer", "page": "integer", "page_size": "integer"},
                                "input_types": {"page": "integer", "page_size": "integer"}},
    "performance.lock.read": {"output_fields": {"total": "integer", "page": "integer", "page_size": "integer"},
                              "input_types": {"page": "integer", "page_size": "integer"}},
    "performance.slow_sql.read": {"output_fields": {"total": "integer", "page": "integer", "page_size": "integer"},
                                  "input_types": {"page": "integer", "page_size": "integer",
                                                  "time_range": "string", "category": "string",
                                                  "hours": "integer"}},
    "inspection.run.execute": {"output_fields": {}, "input_types": {"policy_id": "integer", "timeout_seconds": "integer"}},
}


class PlanError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PlanOwnershipLost(PlanError):
    """An old worker may not publish even a failure after losing its claim."""


def _guard_execution(guard):
    if guard is None:
        return
    try:
        if guard() is False:
            raise ValueError("claim no longer current")
    except Exception as exc:
        raise PlanOwnershipLost("execution_ownership_lost") from exc


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return plan_validation.binding_hash(value)


def _safe_params(value):
    try:
        registry.validate_params(value)
        if len(_json(value)) > 16384:
            raise PlanError("invalid_params")
    except registry.CapabilityError as exc:
        raise PlanError(exc.code) from exc
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"password|secret|token|private_key|credential|authorization", key, re.I):
                raise PlanError("plan_sensitive_payload_rejected")
            _safe_params(child)
    elif isinstance(value, list):
        for child in value:
            _safe_params(child)


def _access(db, actor, entry, instance_id, *, allow_missing=False):
    user, grants, scope = registry._principal(db, actor)
    if entry["kind"] in APPROVAL_KINDS and user.role != "admin":
        scope = registry.access_control.resolve_scope(db, user.id, minimum_access="operate")
    instance = None
    if instance_id is not None:
        if type(instance_id) is not int or not 0 < instance_id <= 2**63 - 1:
            raise PlanError("invalid_target")
        if scope and scope.restricted and instance_id not in scope.native_instance_ids:
            raise PlanError("scope_denied")
        instance = db.query(DBInstance).filter_by(id=instance_id).populate_existing().first()
        if instance is None:
            raise PlanError("target_unavailable")
    engine = instance.db_type if instance else None
    if not registry._authorized(entry, grants, engine=engine):
        raise PlanError("permission_denied")
    if scope and scope.restricted:
        bridge_scope = (engine_execution.bridge() is not None
                        and entry["capability_id"] in engine_execution.READ_OPERATIONS
                        and instance is not None)
        if (entry.get("handler") not in registry.INSTANCE_FILTERED_HANDLERS
                and not bridge_scope) or instance is None:
            raise PlanError("scope_denied")
    if entry.get("requires_instance") and instance is None and not allow_missing:
        raise PlanError("missing_instance")
    return user, instance


def _steps(db, plan_id):
    return db.query(DBCapabilityPlanStep).filter_by(plan_id=plan_id).order_by(DBCapabilityPlanStep.seq).populate_existing().all()


def _load(db, plan_id, actor, *, owner_only=False, for_approval=False):
    user, grants, scope = registry._principal(db, actor)
    if for_approval and not registry.access_control.action_matches(grants, "automation.approve"):
        raise PlanError("permission_denied")
    plan = db.query(DBCapabilityPlan).filter_by(plan_id=plan_id).populate_existing().first()
    if plan is None or (plan.requested_by != user.id and not for_approval and (owner_only or user.role != "admin")):
        raise PlanError("plan_not_found")
    if for_approval and scope and scope.restricted and plan.instance_id not in scope.native_instance_ids:
        raise PlanError("scope_denied")
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=plan_id).order_by(
        DBCapabilityPlanRevision.revision.desc()).populate_existing().first()
    rows = _steps(db, plan_id)
    try:
        plan_validation.validate_frozen_plan(plan, revision, rows)
    except plan_validation.PlanValidationError as exc:
        raise PlanError(exc.code) from exc
    snapshot = plan.capability_snapshot_json
    if not for_approval:
        for definition in snapshot["definitions"]:
            _access(db, actor, definition, plan.instance_id, allow_missing=True)
    return plan


def _serialize(db, plan, *, reused=False):
    rows = _steps(db, plan.plan_id)
    missing = []
    if plan.outcome == "needs_input":
        if plan.instance_id is None:
            missing.append("instance_id")
        if any(step["capability_id"] == "inspection.run.execute" and not step["params"].get("policy_id")
               for step in plan.capability_snapshot_json["steps"]):
            missing.append("policy_id")
    value = {
        "schema": "capability.plan_read/v1", "plan_id": plan.plan_id,
        "capability_id": plan.capability_id, "definition_version": plan.definition_version,
        "instance_id": plan.instance_id, "status": plan.status, "outcome": plan.outcome,
        "requested_by": plan.requested_by, "trigger_type": plan.trigger_type, "db_type": plan.db_type,
        "outcome_kind": plan.outcome_kind, "outcome_reason": plan.outcome_reason,
        "revision": plan.capability_snapshot_json.get("revision", 1),
        "binding_hash": _hash(plan.capability_snapshot_json),
        "expires_at": (plan.capability_snapshot_json.get("validity") or {}).get("expires_at"),
        "approval_state": plan.approval_state,
        "approval_available": plan_validation.requires_approval(plan) and all(
            entry["execution"] == "server" and entry["handler_status"] == "wired"
            for entry in plan.capability_snapshot_json["definitions"]),
        "approved_by": plan.approved_by,
        "approval_expires_at": plan.approval_expires_at.isoformat() + "Z" if plan.approval_expires_at else None,
        "failure_policy": plan.capability_snapshot_json["failure_policy"],
        "missing_params": missing, "result_ref": deepcopy(plan.result_ref_json),
        "parent_plan_id": plan.parent_plan_id, "attempt": plan.attempt, "reused": reused,
        "created_at": plan.created_at.isoformat(),
        "started_at": plan.started_at.isoformat() if plan.started_at else None,
        "finished_at": plan.finished_at.isoformat() if plan.finished_at else None,
        "steps": [{"seq": row.seq, "title": row.title, "capability_id": row.capability_id,
                   "depends_on": row.depends_on_json, "status": row.status,
                   "definition_version": row.precondition_json["definition_version"],
                   "params": deepcopy(row.precondition_json["params"]),
                   "input_refs": deepcopy(row.precondition_json["input_refs"]),
                   "side_effect": row.side_effect,
                   "started_at": row.started_at.isoformat() if row.started_at else None,
                   "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                   "outcome_kind": row.outcome_kind, "result_ref": deepcopy(row.result_ref_json)} for row in rows],
    }
    if "workflow_binding" in plan.capability_snapshot_json:
        value["workflow_binding"] = deepcopy(plan.capability_snapshot_json["workflow_binding"])
    return value


def read_plan(db, plan_id: str, *, actor: dict):
    plan = _load(db, plan_id, actor)
    value = _serialize(db, plan)
    if "execution_identity" in plan.capability_snapshot_json:
        try:
            value["engine_result"] = engine_execution.decode_result(db, plan_id, actor=actor)
        except engine_execution.EngineExecutionError as exc:
            raise PlanError(exc.code) from exc
    return value


def list_plans(db, *, actor: dict, page: int = 1, page_size: int = 50,
               status: str | None = None, outcome: str | None = None,
               capability_id: str | None = None, instance_id: int | None = None):
    """Authorized summaries only; never hydrate result blobs to list workflow history."""
    if type(page) is not int or not 1 <= page <= 1000000 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise PlanError("invalid_pagination")
    if (status is not None and status not in STATUSES) or (outcome is not None and outcome not in OUTCOMES):
        raise PlanError("invalid_filter")
    user, grants, scope = registry._principal(db, actor)
    if instance_id is not None:
        if type(instance_id) is not int or not 0 < instance_id <= 2**63 - 1:
            raise PlanError("invalid_target")
        if scope and scope.restricted and instance_id not in scope.native_instance_ids:
            raise PlanError("scope_denied")
    entries = registry.validate_registry()
    if capability_id is not None:
        registry._entry(capability_id)
    permitted = []
    for engine in (None, "oracle", "mysql", "postgresql", "sqlserver", "host"):
        ids = [entry["capability_id"] for entry in entries
               if (not scope or not scope.restricted or entry.get("handler") in registry.INSTANCE_FILTERED_HANDLERS)
               and registry._authorized(entry, grants, engine=engine)]
        if not ids:
            continue
        # Every step must be authorized; a harmless primary step cannot hide a second domain.
        forbidden_step = db.query(DBCapabilityPlanStep.id).filter(
            DBCapabilityPlanStep.plan_id == DBCapabilityPlan.plan_id,
            or_(DBCapabilityPlanStep.capability_id.is_(None), DBCapabilityPlanStep.capability_id.notin_(ids)),
        ).exists()
        permitted.append(and_(DBCapabilityPlan.db_type == engine, DBCapabilityPlan.capability_id.in_(ids), ~forbidden_step))
    query = db.query(DBCapabilityPlan).filter(or_(*permitted) if permitted else False)
    if user.role != "admin":
        query = query.filter(DBCapabilityPlan.requested_by == user.id)
    if scope and scope.restricted:
        query = query.filter(DBCapabilityPlan.instance_id.in_(scope.native_instance_ids))
    for column, value in ((DBCapabilityPlan.status, status), (DBCapabilityPlan.outcome, outcome),
                          (DBCapabilityPlan.capability_id, capability_id), (DBCapabilityPlan.instance_id, instance_id)):
        if value is not None:
            query = query.filter(column == value)
    total = query.with_entities(func.count(DBCapabilityPlan.plan_id)).scalar()
    rows = query.with_entities(
        DBCapabilityPlan.plan_id, DBCapabilityPlan.capability_id, DBCapabilityPlan.definition_version,
        DBCapabilityPlan.instance_id, DBCapabilityPlan.requested_by, DBCapabilityPlan.status,
        DBCapabilityPlan.outcome, DBCapabilityPlan.outcome_kind, DBCapabilityPlan.approval_state,
        DBCapabilityPlan.created_at, DBCapabilityPlan.started_at, DBCapabilityPlan.finished_at,
    ).order_by(DBCapabilityPlan.created_at.desc(), DBCapabilityPlan.plan_id.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    items = []
    for row in rows:
        item = dict(row._mapping)
        for key in ("created_at", "started_at", "finished_at"):
            item[key] = item[key].isoformat() if item[key] else None
        items.append(item)
    return {"schema": "capability.plan_list/v1", "items": items, "total": total, "page": page, "page_size": page_size}


def declare_outcome(db, plan, outcome: str, *, declared_by: str, reason: str, kind: str | None = None):
    if outcome not in DECLARED_OUTCOMES:
        raise PlanError("declared_outcome_must_be_a_declaration")
    if not declared_by or not reason or len(declared_by) > 64 or len(reason) > 300:
        raise PlanError("declaration_reason_required")
    if plan.status == "settled":
        raise PlanError("plan_already_settled")
    if plan.status == "running" and outcome != "plan_only":
        raise PlanError("running_plan_requires_evidence")
    if outcome == "plan_only" and any(item["kind"] != "plan" for item in plan.capability_snapshot_json["definitions"]):
        raise PlanError("plan_only_requires_plan_capability")
    if outcome == "plan_only" and (plan.status != "running" or not all(
            step.status == "succeeded" and step.result_ref_json for step in _steps(db, plan.plan_id))):
        raise PlanError("plan_only_requires_result")
    plan.status, plan.outcome = "settled", outcome
    plan.outcome_kind, plan.outcome_reason = kind, reason
    plan.outcome_declared_by = declared_by
    plan.finished_at = _now()
    for step in _steps(db, plan.plan_id):
        if step.status == "pending":
            step.status, step.outcome_kind, step.finished_at = "skipped", kind or outcome, plan.finished_at
    db.flush()


def settle(db, plan, outcome: str, *, kind: str | None = None):
    if outcome not in EXECUTED_OUTCOMES or plan.status not in {"running", "draft", "validated"}:
        raise PlanError("invalid_evidence_outcome")
    db.flush()
    steps = _steps(db, plan.plan_id)
    if outcome == "cancelled":
        if plan.started_at is None:
            valid = bool(steps) and all(row.status == "cancelled" for row in steps)
        else:
            valid = (plan.status == "running" and bool(steps) and steps[0].status == "cancelled"
                     and all(row.status == "skipped" for row in steps[1:]))
    else:
        valid = plan.status == "running" and bool(steps) and not any(row.status in {"running", "pending"} for row in steps)
    if valid:
        definitions = {step["seq"]: entry for step, entry in zip(
            plan.capability_snapshot_json["steps"], plan.capability_snapshot_json["definitions"])}
        for step in steps:
            confirmed_cancel = step.status == "cancelled" and plan.started_at is not None
            if step.status in {"succeeded", "partial"} or confirmed_cancel:
                reference = step.result_ref_json
                try:
                    valid = (isinstance(reference, dict)
                             and reference == _result_ref(definitions[step.seq], reference.get("inline")))
                    if valid and confirmed_cancel:
                        valid = (reference["kind"] == "inspection_job"
                                 and reference["inline"].get("outcome") == "cancelled")
                    if valid and (reference["kind"] == "inspection_job"
                                  or definitions[step.seq]["kind"] in APPROVAL_KINDS):
                        validate_effect_reference(db, plan, step)
                except (PlanError, KeyError):
                    valid = False
                if not valid:
                    break
    if outcome == "succeeded":
        valid = valid and all(row.status == "succeeded" and row.result_ref_json for row in steps)
    if outcome == "partial":
        valid = valid and any(row.status in {"succeeded", "partial"} for row in steps) and any(row.status != "succeeded" for row in steps)
    if outcome == "unknown":
        valid = valid and any(row.status == "unknown" for row in steps)
    if outcome == "failed":
        valid = valid and any(row.status == "failed" for row in steps)
    if outcome == "expired":
        valid = valid and any(row.status == "expired" for row in steps)
    if not valid:
        raise PlanError("invalid_evidence_outcome")
    plan.status, plan.outcome = "settled", outcome
    plan.outcome_kind = kind
    plan.outcome_reason = kind
    plan.finished_at = _now()
    plan.result_ref_json = {"kind": "capability_plan_steps", "plan_id": plan.plan_id,
                            "schema": "capability.plan_results/v1", "steps": [row.seq for row in steps if row.result_ref_json]}
    db.flush()


def _schema(entry):
    contract = READ_CONTRACTS.get(entry["capability_id"], {"input_types": {}, "output_fields": {}})
    return {**deepcopy(contract), "capability_id": entry["capability_id"], "definition_version": entry["version"],
            "kind": entry["kind"], "output_schema": entry["output_schema"]}


def _check_plan(db, plan, actor):
    snapshot = plan.capability_snapshot_json
    if "execution_identity" in snapshot:
        try:
            engine_execution.check_binding(db, plan)
        except engine_execution.EngineExecutionError as exc:
            raise PlanError(exc.code) from exc
    try:
        deadline = plan_validation.validity_deadline(snapshot)
    except plan_validation.PlanValidationError as exc:
        raise PlanError(exc.code) from exc
    if deadline is not None and deadline <= _now():
        raise PlanError("plan_expired")
    if "workflow_binding" in snapshot:
        try:
            workflow_bindings.check_current(snapshot["workflow_binding"], instance_id=plan.instance_id,
                                            steps=snapshot["steps"])
        except workflow_bindings.WorkflowBindingError as exc:
            raise PlanError(exc.code) from exc
    instance = None
    for entry in snapshot["definitions"]:
        _, instance = _access(db, actor, entry, plan.instance_id, allow_missing=True)
        if entry.get("execution") == "server" and entry.get("requires_instance") and instance is None:
            raise PlanError("missing_instance")
        if entry["capability_id"] == "inspection.run.execute":
            from app.services.inspection import plan_adapter
            for step in snapshot["steps"]:
                if step["capability_id"] == entry["capability_id"]:
                    if not step["params"].get("policy_id"):
                        raise PlanError("missing_policy")
                    try:
                        plan_adapter.validate_params(db, instance, step["params"])
                    except plan_adapter.InspectionPlanError as exc:
                        raise PlanError(exc.code) from exc
        view = registry.effective_capability(db, entry["capability_id"], definition_version=entry["version"],
                                             engine=instance.db_type if instance else None)
        if view["definition_hash"] != registry.definition_hash(entry):
            raise PlanError("definition_out_of_sync")
        if view["unavailable_reason"]:
            raise PlanError(view["unavailable_reason"])
        if instance and not instance.enabled:
            raise PlanError("target_unavailable")
        if entry.get("requires_instance") and instance is None:
            raise PlanError("missing_instance")
        if not view["plan_executable"] and entry["kind"] not in APPROVAL_KINDS:
            raise PlanError("handler_not_wired")
    actual_target = target_fingerprint(instance) if instance else None
    if actual_target != snapshot["target_fingerprint"]:
        raise PlanError("target_changed")
    schemas = {step["seq"]: _schema(entry) for step, entry in zip(snapshot["steps"], snapshot["definitions"])}
    try:
        plan_validation.validate_step_contracts(snapshot["steps"], snapshot["definitions"], schemas,
                                                engine=instance.db_type if instance else None)
    except plan_validation.PlanValidationError as exc:
        raise PlanError(exc.code) from exc


def review_plan(db, plan_id: str, *, actor: dict):
    """Read-only current readiness, not an approval or an execution receipt."""
    plan = _load(db, plan_id, actor)
    user, _, _ = registry._principal(db, actor)
    issues = []
    if plan.requested_by != user.id:
        issues.append({"code": "plan_owner_required"})
    if plan.status not in {"validated", "approved"} or plan.outcome is not None:
        issues.append({"code": "invalid_plan_state"})
    elif (reason := plan_validation.start_block_reason(plan, _steps(db, plan.plan_id))):
        issues.append({"code": reason})
    reason = plan_validation.approval_block_reason(plan, db=db)
    if reason:
        issues.append({"code": reason})
    try:
        _check_plan(db, plan, actor)
    except (PlanError, registry.CapabilityError) as exc:
        # Storage loss is not an ordinary denied-readiness result.
        if exc.code in {"registry_storage_unavailable", "plan_storage_unavailable"}:
            raise
        issues.append({"code": exc.code})
    return {"schema": "capability.plan_validation/v1", "plan_id": plan.plan_id,
            "revision": plan.capability_snapshot_json.get("revision", 1),
            "checked_at": _now().isoformat() + "Z", "plan_status": plan.status,
            "can_execute": not issues, "issues": issues,
            "approval_available": _serialize(db, plan)["approval_available"],
            "requires_recheck": True}


def _reject_validation(db, plan, exc):
    code = exc.code
    outcome = "needs_input" if code in {"missing_instance", "missing_policy"} else (
        "unsupported" if code in {"engine_not_supported", "handler_not_wired", "capability_disabled"} else "blocked")
    declare_outcome(db, plan, outcome, declared_by="plan_service.validate", reason=code,
                    kind="missing_params" if outcome == "needs_input" else code)


def validate_plan(db, plan_id: str, *, actor: dict):
    lock_transaction(db, "capability_plan_state")
    plan = _load(db, plan_id, actor, owner_only=True)
    if plan.status != "draft":
        raise PlanError("invalid_plan_state")
    try:
        _check_plan(db, plan, actor)
    except (PlanError, registry.CapabilityError) as exc:
        _reject_validation(db, plan, exc)
    else:
        plan.status = "validated"
    db.commit()
    return _serialize(db, plan)


def compile_inputs(db, *, actor, capability_id, definition_version, instance_id=None, params=None, steps=None):
    """Shared source-owned compilation for creation and an explicit new revision."""
    if type(definition_version) is not int or definition_version < 1:
        raise PlanError("stale_definition_version")
    if params is not None and not isinstance(params, dict):
        raise PlanError("invalid_params")
    _safe_params(params or {})
    primary = registry._entry(capability_id)
    user, instance = _access(db, actor, primary, instance_id, allow_missing=True)
    raw = steps if steps is not None else [{"seq": 1, "title": primary["title"], "capability_id": capability_id,
                                         "definition_version": definition_version, "params": params or {}}]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise PlanError("plan_too_many_steps")
    if steps is not None and params:
        raise PlanError("ambiguous_plan_params")
    definitions, schemas = [], {}
    for step in raw:
        if not isinstance(step, dict) or not isinstance(step.get("capability_id"), str):
            raise PlanError("invalid_plan_step")
        entry = registry._entry(step["capability_id"])
        _access(db, actor, entry, instance_id, allow_missing=True)
        _safe_params(step.get("params", {}))
        if type(step.get("seq")) is not int:
            raise PlanError("invalid_plan_step")
        definitions.append(entry)
        schemas[step["seq"]] = _schema(entry)
    if raw[0]["capability_id"] != capability_id or raw[0].get("definition_version") != definition_version:
        raise PlanError("primary_step_mismatch")
    try:
        normalized = normalize_steps(raw)
    except PlanDAGError as exc:
        raise PlanError(exc.code) from exc
    return user, instance, definitions, schemas, normalized


def freeze_steps(db, instance, normalized, definitions):
    normalized = deepcopy(normalized)
    for step, entry in zip(normalized, definitions):
        if entry["capability_id"] == "inspection.run.execute" and instance is not None and step["params"].get("policy_id"):
            if step["input_refs"]:
                raise PlanError("inspection_dynamic_params_forbidden")
            from app.services.inspection import plan_adapter
            try:
                step["params"] = plan_adapter.prepare_params(db, instance, step["params"])
            except plan_adapter.InspectionPlanError as exc:
                raise PlanError(exc.code) from exc
    return normalized


def _workflow_request_identity(*, user_id, capability_id, instance_id, target, steps,
                               binding, valid_for_seconds, request_approval):
    """Shared workflow create/revise identity, before inspection runtime freezing.

    Legacy revisions intentionally keep their existing historical hash algorithm.
    Parent, entry operation and fresh expiry timestamps are not business identity.
    """
    return {"actor": user_id, "capability_id": capability_id, "instance_id": instance_id,
            "target": target,
            "steps": [{key: value for key, value in step.items() if key not in {"definition_version", "title"}}
                      for step in steps],
            "workflow_binding": binding, "valid_for_seconds": 3600 if valid_for_seconds is None else valid_for_seconds,
            "request_approval": request_approval}


def create_plan(db, *, actor: dict, capability_id: str, definition_version: int,
                instance_id: int | None = None, params: dict | None = None, steps: list | None = None,
                trigger_type: str = "manual", idempotency_key: str | None = None,
                parent_plan_id: str | None = None, valid_for_seconds: int | None = None,
                request_approval: bool = False, commit: bool = True, admission_hash: str | None = None,
                workflow_binding: dict | None = None):
    if type(request_approval) is not bool or (valid_for_seconds is not None and (
            type(valid_for_seconds) is not int or not 60 <= valid_for_seconds <= 86400)):
        raise PlanError("invalid_validity_policy")
    if admission_hash is not None and (not isinstance(admission_hash, str)
                                       or re.fullmatch(r"[a-f0-9]{64}", admission_hash) is None):
        raise PlanError("invalid_admission_identity")
    if trigger_type not in {"manual", "assistant", "schedule", "retry"}:
        raise PlanError("invalid_trigger_type")
    user, instance, definitions, schemas, normalized = compile_inputs(
        db, actor=actor, capability_id=capability_id, definition_version=definition_version,
        instance_id=instance_id, params=params, steps=steps)
    engine_frozen = None
    if engine_execution.bridge() is not None and capability_id in engine_execution.READ_OPERATIONS:
        if len(normalized) != 1 or normalized[0]["params"] or normalized[0]["input_refs"]:
            raise PlanError("provider_params_unsupported")
        try:
            engine_frozen = engine_execution.bridge().freeze(db, instance, capability_id)
        except engine_execution.EngineExecutionError as exc:
            raise PlanError(exc.code) from exc
    if workflow_binding is not None:
        try:
            workflow_binding = workflow_bindings.validate_request(
                workflow_binding, capability_id=capability_id, definition_version=definition_version,
                instance_id=instance_id, params=params, steps=steps, valid_for_seconds=valid_for_seconds,
                request_approval=request_approval)
        except workflow_bindings.WorkflowBindingError as exc:
            raise PlanError(exc.code) from exc
    business = [{key: value for key, value in step.items() if key not in {"definition_version", "title"}} for step in normalized]
    target = target_fingerprint(instance) if instance else None
    request_identity = {"actor": user.id, "capability_id": capability_id, "instance_id": instance_id,
                        "target": target, "steps": business}
    if valid_for_seconds is not None:
        request_identity["valid_for_seconds"] = valid_for_seconds
    if request_approval:
        request_identity["request_approval"] = True
    if workflow_binding is not None:
        request_identity = _workflow_request_identity(
            user_id=user.id, capability_id=capability_id, instance_id=instance_id, target=target,
            steps=normalized, binding=workflow_binding, valid_for_seconds=valid_for_seconds,
            request_approval=request_approval)
    if engine_frozen is not None:
        request_identity["execution_identity"] = engine_frozen["identity"]
        request_identity["provider_version"] = engine_frozen["provider_version"]
        request_identity["provider_generation"] = engine_frozen["provider_generation"]
        request_identity["provider_operation"] = engine_frozen["operation"]
    request_hash = _hash(request_identity)
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                                        or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key)):
        raise PlanError("invalid_idempotency_key")
    if parent_plan_id and not idempotency_key:
        raise PlanError("rerun_requires_explicit_key")
    key = _hash({"actor": user.id, "client_key": idempotency_key}) if idempotency_key else request_hash
    lock_transaction(db, "capability_plan_create")
    existing = db.query(DBCapabilityPlan).filter_by(idempotency_key=key).populate_existing().first()
    if existing:
        if (existing.capability_snapshot_json.get("request_hash") != request_hash
                or existing.capability_snapshot_json.get("admission_hash") != admission_hash
                or (parent_plan_id is not None and existing.parent_plan_id != parent_plan_id)):
            raise PlanError("idempotency_conflict")
        _load(db, existing.plan_id, actor, owner_only=True)
        if commit:
            db.commit()
        return _serialize(db, existing, reused=True)
    if any(step["definition_version"] != entry["version"] for step, entry in zip(normalized, definitions)):
        raise PlanError("stale_definition_version")
    normalized = freeze_steps(db, instance, normalized, definitions)
    try:
        normalized = plan_validation.validate_step_contracts(normalized, definitions, schemas,
                                                             engine=instance.db_type if instance else None)
    except plan_validation.PlanValidationError as exc:
        raise PlanError(exc.code) from exc
    # Infer a parent only for a new key. Replaying an existing key cannot change its binding.
    if idempotency_key and parent_plan_id is None:
        automatic = db.query(DBCapabilityPlan).filter_by(idempotency_key=request_hash).populate_existing().first()
        if automatic:
            parent_plan_id = automatic.plan_id
    parent = _load(db, parent_plan_id, actor, owner_only=True) if parent_plan_id else None
    if parent:
        if parent.status != "settled":
            raise PlanError("parent_plan_not_settled")
        if parent.capability_snapshot_json["request_hash"] != request_hash:
            raise PlanError("rerun_request_mismatch")
        if db.query(DBCapabilityPlan).filter_by(parent_plan_id=parent.plan_id).first():
            raise PlanError("parent_plan_already_retried")
    snapshot = {"schema": "capability.plan/v1", "definitions": definitions, "steps": normalized,
                "failure_policy": "stop",
                "request_hash": request_hash, "target_fingerprint": target,
                "identity": {"requested_by": user.id, "instance_id": instance_id,
                             "capability_id": capability_id, "definition_version": definition_version}}
    if engine_frozen is not None:
        snapshot.update(execution_identity=engine_frozen["identity"],
                        provider_version=engine_frozen["provider_version"],
                        provider_generation=engine_frozen["provider_generation"],
                        provider_operation=engine_frozen["operation"])
    if workflow_binding is not None:
        snapshot["workflow_binding"] = deepcopy(workflow_binding)
    now, ttl = _now(), valid_for_seconds if valid_for_seconds is not None else 3600
    snapshot["validity"] = {"seconds": ttl, "expires_at": (now + timedelta(seconds=ttl)).isoformat() + "Z"}
    if request_approval:
        snapshot["approval_requested"] = True
    if admission_hash is not None:
        snapshot["admission_hash"] = admission_hash
    needs_approval = request_approval or any(entry["kind"] in APPROVAL_KINDS for entry in definitions)
    plan = DBCapabilityPlan(
        plan_id="pl" + uuid4().hex[:26], capability_id=capability_id, definition_version=definition_version,
        capability_snapshot_json=snapshot, instance_id=instance_id, db_type=instance.db_type if instance else None,
        requested_by=user.id, trigger_type=trigger_type, status="draft", approval_state="pending" if needs_approval else "not_required",
        approval_binding_hash=_hash(snapshot) if needs_approval else None, idempotency_key=key,
        parent_plan_id=parent.plan_id if parent else None, attempt=parent.attempt + 1 if parent else 1, created_at=_now(),
    )
    db.add(plan)
    db.flush()
    if engine_frozen is not None:
        try:
            engine_execution.bridge().retain_plan(plan, engine_frozen)
        except engine_execution.EngineExecutionError as exc:
            raise PlanError(exc.code) from exc
        engine_execution.save_binding(db, plan, engine_frozen)
    for step, entry in zip(normalized, definitions):
        db.add(DBCapabilityPlanStep(plan_id=plan.plan_id, seq=step["seq"], title=step["title"],
                                   capability_id=entry["capability_id"], depends_on_json=step["depends_on"],
                                   side_effect="write" if entry["kind"] in APPROVAL_KINDS else "read",
                                   precondition_json=step, status="pending"))
    db.add(DBCapabilityPlanRevision(plan_id=plan.plan_id, revision=1, snapshot_json=deepcopy(snapshot),
                                    binding_hash=_hash(snapshot), created_at=_now(), created_by=user.id))
    db.flush()
    try:
        _check_plan(db, plan, actor)
    except (PlanError, registry.CapabilityError) as exc:
        _reject_validation(db, plan, exc)
    else:
        plan.status = "validated"
    if commit:
        db.commit()
    else:
        db.flush()
    return _serialize(db, plan)


def _result_ref(entry, result):
    if not isinstance(result, dict):
        raise PlanError("result_unknown")
    if entry["capability_id"] in COMMON_EFFECT_CAPABILITIES:
        expected_keys = {"schema", "capability_id", "definition_version", "instance_id",
                         "execution_id", "outcome", "data"}
        data = result.get("data")
        if (set(result) != expected_keys
                or result.get("schema") != EFFECT_RESULT_SCHEMA
                or result.get("capability_id") != entry["capability_id"]
                or result.get("definition_version") != EFFECT_RESULT_VERSION
                or result.get("instance_id") is not None and type(result.get("instance_id")) is not int
                or not isinstance(result.get("execution_id"), str)
                or re.fullmatch(r"pl[a-f0-9]{26}:[1-9][0-9]*:[1-9][0-9]*", result["execution_id"]) is None
                or result.get("outcome") not in {"success", "partial", "failed", "unknown"}
                or not isinstance(data, dict) or data.get("outcome") != result.get("outcome")):
            raise PlanError("result_unknown")
    elif entry["capability_id"] in {"inspection.run.query", "inspection.template.read"}:
        if result.get("schema") != entry["output_schema"] or not isinstance(result.get("items"), list):
            raise PlanError("result_unknown")
        for field in ("total", "page", "page_size"):
            if type(result.get(field)) is not int or result[field] < (0 if field == "total" else 1):
                raise PlanError("result_unknown")
    elif entry["capability_id"] == "operations.capability_matrix.read":
        if (("schema" in result and result["schema"] != entry["output_schema"])
                or not all(isinstance(result.get(field), list) for field in ("items", "instances", "engines"))
                or not isinstance(result.get("summary"), dict)):
            raise PlanError("result_unknown")
    elif entry["capability_id"] == "inspection.run.execute":
        if (result.get("schema") != "inspection.plan_result/v1"
                or not isinstance(result.get("job_id"), str) or re.fullmatch(r"[a-f0-9]{30}", result["job_id"]) is None
                or type(result.get("instance_id")) is not int
                or result.get("outcome") not in {"success", "partial", "failed", "cancelled", "expired", "unknown"}
                or not isinstance(result.get("report"), dict) or not isinstance(result.get("run"), dict)):
            raise PlanError("result_unknown")
    elif entry["capability_id"] == "diagnosis.root_cause.execute":
        sampling = result.get("sampling")
        if (result.get("schema") != "diagnosis.root_cause_result/v1"
                or result.get("outcome") not in {"success", "partial", "failed", "unknown"}
                or type(result.get("instance_id")) is not int
                or not isinstance(sampling, dict)
                or sampling.get("collection_mode") != "current_snapshot"
                or sampling.get("historical_replay_performed") is not False
                or not isinstance(sampling.get("requested_time_window"), str)
                or not isinstance(sampling.get("impact_scope"), str)):
            raise PlanError("result_unknown")
    elif entry["capability_id"] == "report.document.generate":
        if (result.get("schema") != "report.document_result/v1"
                or result.get("outcome") not in {"success", "partial"}
                or type(result.get("instance_id")) is not int
                or result.get("report_type") not in {"unified", "after_sales"}
                or result.get("format") not in {"markdown", "html"}
                or type(result.get("window_hours")) is not int
                or not isinstance(result.get("include_ai"), bool)
                or type(result.get("evidence_count")) is not int
                or result["evidence_count"] < 0):
            raise PlanError("result_unknown")
    elif entry["kind"] == "plan":
        # A plan step may only produce a reviewed document; it never executes the change.
        if (not isinstance(result.get("script"), str) or not result["script"].strip()
                or result.get("execute") is True or result.get("executed") is True):
            raise PlanError("result_unknown")
    elif entry["output_schema"] in {"object", "engine_specific_response"}:
        # Generic service payloads (C13 readers). The handler owns the shape; the plan layer
        # only requires a JSON object whose self-declared schema, when present, matches the
        # declaration. Without this branch every such read settles as
        # result_schema_not_wired even though the handler ran.
        if "schema" in result and result["schema"] != entry["output_schema"]:
            raise PlanError("result_unknown")
    else:
        raise PlanError("result_schema_not_wired")
    try:
        if len(_json(result)) > 1048576:
            raise PlanError("result_budget_exceeded")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError("result_unknown") from exc
    kind = ("inspection_job" if entry["capability_id"] == "inspection.run.execute"
            else "capability_effect" if entry["kind"] in APPROVAL_KINDS else "capability_read")
    return {"kind": kind,
            "capability_id": entry["capability_id"],
            "schema": entry["output_schema"], "inline": deepcopy(result)}


def _validate_capacity_effect(db, plan, step, ref, value):
    """The policy row is the evidence: it must have been written by this run, with this result."""
    from app.models.capacity_policy import DBCapacityPolicy

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if value.get("instance_id") != plan.instance_id:
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    policy = db.query(DBCapacityPolicy).filter_by(instance_id=plan.instance_id).populate_existing().first()
    if policy is None or policy.last_run_time is None:
        raise PlanError("result_unknown")
    if step.started_at is not None and policy.last_run_time < step.started_at:
        raise PlanError("result_unknown")   # the row predates this run: not this run's evidence
    if outcome == "success":
        if (policy.last_status != "success"
                or int(policy.last_sample_count or 0) != int(value.get("sample_count", -1))):
            raise PlanError("result_unknown")
    elif outcome == "partial":
        if value.get("reason_code") != f"capacity_{policy.last_status}":
            raise PlanError("result_unknown")
    elif outcome != "unknown":
        raise PlanError("result_unknown")
    return True


def _validate_root_cause_effect(db, plan, step, ref, value):
    """Accept a root-cause result only when this execution's incident proves it."""
    from app.models.diagnosis_center import DiagEvidence, DiagIncident, DiagRootCause
    from app.services.diagnosis.capability_effect import (
        COLLECTION_MODE,
        ROOT_CAUSE_RESULT_SCHEMA,
        customer_context_signals,
        customer_context_summary,
        trigger_source_for_execution,
    )

    if (not isinstance(value, dict) or ref.get("kind") != "capability_effect"
            or ref.get("schema") != ROOT_CAUSE_RESULT_SCHEMA
            or value.get("schema") != ROOT_CAUSE_RESULT_SCHEMA
            or type(value.get("incident_id")) is not int
            or value.get("instance_id") != plan.instance_id):
        raise PlanError("result_unknown")
    incident = db.query(DiagIncident).filter_by(id=value["incident_id"]).populate_existing().first()
    execution_id = f"{plan.plan_id}:{step.seq}:{plan.capability_snapshot_json.get('revision', 1)}"
    if (incident is None or incident.instance_id != plan.instance_id
            or incident.trigger_source != trigger_source_for_execution(execution_id)
            or step.started_at is None or incident.started_at is None or incident.started_at < step.started_at):
        raise PlanError("result_unknown")

    evidence_count = db.query(DiagEvidence.id).filter_by(incident_id=incident.id).count()
    root_cause_count = db.query(DiagRootCause.id).filter_by(incident_id=incident.id).count()
    ai_result = incident.ai_result if isinstance(incident.ai_result, dict) else {}
    expected_fields = {
        "incident_no": incident.incident_no,
        "status": incident.status,
        "severity": incident.severity,
        "root_cause_summary": incident.root_cause_summary,
        "primary_layer": incident.primary_layer,
        "evidence_count": evidence_count,
        "root_cause_count": root_cause_count,
        "execution_status": str(ai_result.get("execution_status") or "unknown"),
        "data_quality": str(ai_result.get("data_quality") or "unknown"),
        "conclusion_status": str(ai_result.get("conclusion_status") or "unknown"),
    }
    if (int(incident.evidence_count or 0) != evidence_count
            or int(incident.root_cause_count or 0) != root_cause_count
            or any(value.get(key) != expected for key, expected in expected_fields.items())):
        raise PlanError("result_unknown")

    sampling = value.get("sampling")
    if (not isinstance(sampling, dict) or sampling.get("collection_mode") != COLLECTION_MODE
            or sampling.get("historical_replay_performed") is not False
            or not isinstance(sampling.get("requested_time_window"), str)
            or not isinstance(sampling.get("impact_scope"), str)):
        raise PlanError("result_unknown")
    manual = (
        db.query(DiagEvidence).filter_by(incident_id=incident.id, source="manual_symptom")
        .order_by(DiagEvidence.id.desc()).first()
    )
    raw = manual.raw if manual is not None and isinstance(manual.raw, dict) else {}
    expected_signals = customer_context_signals()
    params = (step.precondition_json or {}).get("params") or {}
    if (not all(isinstance(params.get(key), str) for key in ("symptom", "time_window", "impact_scope"))
            or sampling["requested_time_window"] != params["time_window"].strip()
            or sampling["impact_scope"] != params["impact_scope"].strip()
            or not raw or raw.get("intent_signals") != expected_signals
            or raw.get("intent_summary") != customer_context_summary(
                sampling["requested_time_window"], sampling["impact_scope"])
            or raw.get("symptom") != params["symptom"].strip()):
        raise PlanError("result_unknown")

    execution_status = expected_fields["execution_status"]
    if incident.status == "failed" or execution_status == "failed":
        expected_outcome, code_field, code = "failed", "error_code", "diagnosis_failed"
    elif incident.status == "root_caused" and root_cause_count > 0 and execution_status == "success":
        expected_outcome, code_field, code = "success", None, None
    elif root_cause_count > 0:
        expected_outcome, code_field, code = "partial", "reason_code", "collection_incomplete"
    else:
        expected_outcome, code_field, code = "partial", "reason_code", "no_supported_root_cause"
    if value.get("outcome") != expected_outcome or (code_field and value.get(code_field) != code):
        raise PlanError("result_unknown")
    return True


def _validate_report_document_effect(db, plan, step, ref, value):
    from app.models.report_center import ReportArtifact
    from app.services import report_center, report_document

    params = (step.precondition_json or {}).get("params") or {}
    if (not isinstance(value, dict) or ref.get("kind") != "capability_effect"
            or ref.get("capability_id") != "report.document.generate"
            or ref.get("schema") != report_document.RESULT_SCHEMA
            or value.get("schema") != report_document.RESULT_SCHEMA
            or value.get("instance_id") != plan.instance_id
            or value.get("report_type") != params.get("report_type", "unified")
            or value.get("format") != params.get("format", "markdown")
            or value.get("window_hours") != params.get("window_hours", 168)
            or value.get("include_ai") != params.get("include_ai", True)):
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "partial":
        if (value.get("reason_code") != "no_evidence" or value.get("artifact_id") is not None
                or value.get("artifact_uid") is not None or value.get("sha256") is not None
                or value.get("download_url") is not None or value.get("evidence_count") != 0):
            raise PlanError("result_unknown")
        instance = db.get(DBInstance, plan.instance_id)
        evidence = report_document.collect_evidence(db, instance, window_hours=value["window_hours"])
        if (evidence.get("inspection") or (evidence.get("alerts") or {}).get("total")
                or (evidence.get("capacity") or {}).get("total")):
            raise PlanError("result_unknown")
        return True
    if outcome != "success":
        raise PlanError("result_unknown")
    artifact_id = value.get("artifact_id")
    if (type(artifact_id) is not int or not isinstance(value.get("artifact_uid"), str)
            or re.fullmatch(r"[a-f0-9]{32}", value["artifact_uid"]) is None
            or not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None
            or value.get("analysis_source") not in {"rule_engine", "ai"}
            or not isinstance(value.get("generated_at"), str)):
        raise PlanError("result_unknown")
    artifact = db.query(ReportArtifact).filter(ReportArtifact.id == artifact_id).first()
    if (artifact is None or artifact.instance_id != plan.instance_id
            or artifact.artifact_uid != value.get("artifact_uid")
            or artifact.source_type != "report_document" or artifact.artifact_kind != "document"
            or artifact.status != "generated" or artifact.report_type != f"report_{value['report_type']}"
            or artifact.db_type != plan.db_type or artifact.created_by != f"user:{plan.requested_by}"):
        raise PlanError("result_unknown")
    revision = (plan.capability_snapshot_json or {}).get("revision", 1)
    expected = f"report:document:{plan.plan_id}:{step.seq}:{revision}"
    if (artifact.source_key != expected or artifact.content_sha256 != value.get("sha256")
            or value.get("download_url") != f"/api/v1/reports/catalog/{artifact.id}/download"
            or value.get("format") not in (artifact.format_manifest or [])
            or len(artifact.evidence_manifest or []) != value.get("evidence_count")):
        raise PlanError("result_unknown")
    payload = artifact.content_json if isinstance(artifact.content_json, dict) else {}
    if (not artifact.content_markdown or not artifact.evidence_manifest
            or payload.get("schema") != report_document.SCHEMA
            or payload.get("report_type") != value.get("report_type")
            or (payload.get("instance") or {}).get("id") != plan.instance_id
            or (payload.get("window") or {}).get("hours") != value.get("window_hours")
            or not report_center.verify_artifact(artifact).get("valid")):
        raise PlanError("result_unknown")
    return True


def _validate_switchover_effect(db, plan, step, ref, value):
    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if (value.get("outcome") != "success" or value.get("status") != "plan_only"
            or value.get("plan_only") is not True
            or value.get("execute") is True or value.get("executed") is True):
        raise PlanError("result_unknown")
    if value.get("execution_blocked") is not True:
        raise PlanError("result_unknown")
    if not isinstance(value.get("sql"), str) or not value["sql"].strip():
        raise PlanError("result_unknown")
    if not isinstance(value.get("warnings"), list):
        raise PlanError("result_unknown")
    return True


def _validate_alert_import_effect(db, plan, step, ref, value):
    from app.models.alert_event import DBAlertEvent

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    ids = value.get("event_ids")
    if not isinstance(ids, list) or any(type(item) is not int for item in ids):
        raise PlanError("result_unknown")
    rows = db.query(DBAlertEvent.id).filter(DBAlertEvent.id.in_(ids or [-1])).all()
    if len(rows) != len(ids) or len(rows) != value.get("count"):
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "success" and not ids:
        raise PlanError("result_unknown")
    if outcome == "partial" and value.get("reason_code") != "empty_alert_cache":
        raise PlanError("result_unknown")
    if outcome not in {"success", "partial"}:
        raise PlanError("result_unknown")
    return True


def _validate_zabbix_sync_effect(db, plan, step, ref, value):
    from app.models.external_monitoring import ZabbixHostMapping, ZabbixProblemCache

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if type(value.get("config_id")) is not int:
        raise PlanError("result_unknown")
    host_ids = value.get("host_ids") if isinstance(value.get("host_ids"), list) else []
    problem_ids = value.get("problem_ids") if isinstance(value.get("problem_ids"), list) else []
    host_rows = db.query(ZabbixHostMapping.id).filter(
        ZabbixHostMapping.zabbix_config_id == value["config_id"],
        ZabbixHostMapping.id.in_(host_ids or [-1])).all()
    problem_rows = db.query(ZabbixProblemCache.id).filter(
        ZabbixProblemCache.zabbix_config_id == value["config_id"],
        ZabbixProblemCache.id.in_(problem_ids or [-1])).all()
    if len(host_rows) != value.get("host_count") or len(problem_rows) != value.get("problem_count"):
        raise PlanError("result_unknown")
    if value.get("outcome") == "success" and not (host_rows or problem_rows):
        raise PlanError("result_unknown")
    if value.get("outcome") not in {"success", "partial", "failed"}:
        raise PlanError("result_unknown")
    return True


def _validate_prometheus_sync_effect(db, plan, step, ref, value):
    from app.models.observability import ObservabilityCurrentMetric

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if type(value.get("source_id")) is not int or type(value.get("stored_count")) is not int:
        raise PlanError("result_unknown")
    rows = db.query(ObservabilityCurrentMetric.id).filter(
        ObservabilityCurrentMetric.source_id == value["source_id"],
        ObservabilityCurrentMetric.collect_time >= step.started_at.replace(microsecond=0),
    ).all() if step.started_at is not None else []
    if len(rows) != value["stored_count"] or len(rows) != value.get("row_count"):
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "success" and (not rows or value.get("status") != "success"):
        raise PlanError("result_unknown")
    if outcome == "partial" and not rows:
        raise PlanError("result_unknown")
    if outcome not in {"success", "partial", "failed"}:
        raise PlanError("result_unknown")
    if outcome == "failed" and not value.get("error_code"):
        raise PlanError("result_unknown")
    return True


def _validate_binding_effect(db, plan, step, ref, value):
    from app.models.observability import ObservabilityInstanceBinding

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    ids = value.get("binding_ids")
    if not isinstance(ids, list) or any(type(item) is not int for item in ids):
        raise PlanError("result_unknown")
    rows = db.query(ObservabilityInstanceBinding).filter(ObservabilityInstanceBinding.id.in_(ids or [-1])).all()
    if len(rows) != len(ids) or len(rows) != value.get("count"):
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "success" and not ids:
        raise PlanError("result_unknown")
    if outcome == "partial" and value.get("reason_code") != "no_matching_binding":
        raise PlanError("result_unknown")
    if outcome not in {"success", "partial"}:
        raise PlanError("result_unknown")
    return True


def _validate_plan_effect(db, plan, step, ref, value):
    """A plan step's deliverable is a reviewed script; execution_blocked must be explicit."""
    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if (value.get("instance_id") not in {None, plan.instance_id}
            or value.get("outcome") != "success" or value.get("plan_only") is not True
            or value.get("execution_blocked") is not True
            or not isinstance(value.get("script"), str) or not value["script"].strip()
            or value.get("execute") is True or value.get("executed") is True):
        raise PlanError("result_unknown")
    return True


def _validate_sql_profile_effect(db, plan, step, ref, value):
    from app.models.sql_profile import DBSqlProfile

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if value.get("instance_id") != plan.instance_id or type(value.get("live_count")) is not int:
        raise PlanError("result_unknown")
    rows = db.query(DBSqlProfile.id).filter(
        DBSqlProfile.instance_id == plan.instance_id, DBSqlProfile.capture_time >= step.started_at,
    ).all() if step.started_at is not None else []
    if sorted(row.id for row in rows) != value.get("live_ids") or len(rows) != value["live_count"]:
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "success" and (value["live_count"] == 0 or value.get("errors")):
        raise PlanError("result_unknown")
    if outcome == "partial" and not (value["live_count"] or value.get("sources") or value.get("reason_code")):
        raise PlanError("result_unknown")
    if outcome in {"failed", "unknown"} and (value["live_count"] != 0 or not value.get("error_code")):
        raise PlanError("result_unknown")
    if outcome not in {"success", "partial", "failed", "unknown"}:
        raise PlanError("result_unknown")
    return True


def _validate_candidate_effect(db, plan, step, ref, value):
    from app.models.data_governance import DBArchiveCandidate

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    ids = value.get("candidate_ids")
    if value.get("instance_id") != plan.instance_id or not isinstance(ids, list) or any(type(item) is not int for item in ids):
        raise PlanError("result_unknown")
    rows = db.query(DBArchiveCandidate).filter(DBArchiveCandidate.id.in_(ids or [-1])).all()
    if (len(rows) != len(ids) or len(rows) != value.get("count")
            or any(row.instance_id != plan.instance_id or step.started_at is None
                   or row.scan_time < step.started_at.replace(microsecond=0) for row in rows)):
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    if outcome == "success" and not ids:
        raise PlanError("result_unknown")
    if outcome == "partial" and value.get("reason_code") != "empty_scan":
        raise PlanError("result_unknown")
    if outcome not in {"success", "partial"}:
        raise PlanError("result_unknown")
    return True


def _validate_snapshot_effect(db, plan, step, ref, value, model):
    """Snapshot rows carrying this run's capture_time are the evidence."""
    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if value.get("instance_id") != plan.instance_id:
        raise PlanError("result_unknown")
    outcome, count = value.get("outcome"), value.get("count")
    if type(count) is not int or count < 0:
        raise PlanError("result_unknown")
    rows = db.query(model).filter(
        model.instance_id == plan.instance_id,
        model.capture_time >= step.started_at,
    ).all() if step.started_at is not None else []
    if len(rows) != count:
        raise PlanError("result_unknown")
    if outcome == "success":
        if count == 0 or value.get("errors"):
            raise PlanError("result_unknown")
    elif outcome == "partial":
        if not value.get("errors") and count > 0:
            raise PlanError("result_unknown")
    elif outcome in {"failed", "unknown"}:
        if count != 0 or not value.get("error_code"):
            raise PlanError("result_unknown")
    else:
        raise PlanError("result_unknown")
    return True


def _validate_workload_effect(db, plan, step, ref, value):
    from app.models.workload_report import DBWorkloadReport

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if value.get("instance_id") != plan.instance_id or type(value.get("report_id")) is not int:
        raise PlanError("result_unknown")
    row = db.query(DBWorkloadReport).filter_by(id=value["report_id"]).populate_existing().first()
    if (row is None or row.instance_id != plan.instance_id or step.started_at is None
            or row.create_time is None or row.create_time < step.started_at):
        raise PlanError("result_unknown")
    expected = {"success": "success", "partial": "partial", "failed": "failed"}.get(value.get("outcome"))
    if expected is None or row.status != expected:
        raise PlanError("result_unknown")
    if row.status == "success" and not isinstance(row.summary_json, dict):
        raise PlanError("result_unknown")
    return True


def _validate_baseline_effect(db, plan, step, ref, value):
    """The rows this run inserted are the evidence; a zero-row collector is never a success."""
    from app.models.perf_metric import DBPerfMetric

    if not isinstance(value, dict) or ref.get("kind") != "capability_effect":
        raise PlanError("result_unknown")
    if value.get("instance_id") != plan.instance_id:
        raise PlanError("result_unknown")
    outcome = value.get("outcome")
    count = value.get("count")
    if type(count) is not int or count < 0:
        raise PlanError("result_unknown")
    rows = db.query(DBPerfMetric).filter(
        DBPerfMetric.instance_id == plan.instance_id,
        DBPerfMetric.capture_time >= step.started_at,
    ).all() if step.started_at is not None else []
    if len(rows) != count:
        raise PlanError("result_unknown")
    if outcome in {"success", "partial"}:
        names = value.get("metric_names")
        if not isinstance(names, list) or sorted(names) != sorted({row.metric_name for row in rows}):
            raise PlanError("result_unknown")
        if outcome == "success" and (count == 0 or value.get("errors") or value.get("warnings")):
            raise PlanError("result_unknown")
        if outcome == "partial" and not (value.get("errors") or value.get("warnings")):
            raise PlanError("result_unknown")
    elif outcome in {"failed", "unknown"}:
        if count != 0 or not value.get("error_code"):
            raise PlanError("result_unknown")
    else:
        raise PlanError("result_unknown")
    return True


def _common_effect_payload(plan, step, ref):
    """Authenticate the shared envelope before a domain validator sees its payload."""
    if step.capability_id not in COMMON_EFFECT_CAPABILITIES or not isinstance(ref, dict):
        raise PlanError("result_unknown")
    value = ref.get("inline")
    expected_execution_id = (
        f"{plan.plan_id}:{step.seq}:{plan.capability_snapshot_json.get('revision', 1)}"
    )
    definitions = {
        frozen_step["seq"]: definition
        for frozen_step, definition in zip(
            plan.capability_snapshot_json.get("steps", []),
            plan.capability_snapshot_json.get("definitions", []),
        )
    }
    definition = definitions.get(step.seq)
    expected_keys = {"schema", "capability_id", "definition_version", "instance_id",
                     "execution_id", "outcome", "data"}
    if (not isinstance(definition, dict)
            or definition.get("capability_id") != step.capability_id
            or definition.get("version") != EFFECT_RESULT_VERSION
            or definition.get("output_schema") != EFFECT_RESULT_SCHEMA
            or ref.get("kind") != "capability_effect"
            or ref.get("capability_id") != step.capability_id
            or ref.get("schema") != EFFECT_RESULT_SCHEMA
            or not isinstance(value, dict) or set(value) != expected_keys
            or value.get("schema") != EFFECT_RESULT_SCHEMA
            or value.get("capability_id") != step.capability_id
            or value.get("definition_version") != EFFECT_RESULT_VERSION
            or value.get("instance_id") != plan.instance_id
            or value.get("execution_id") != expected_execution_id
            or value.get("outcome") not in {"success", "partial", "failed", "unknown"}
            or not isinstance(value.get("data"), dict)
            or value["data"].get("outcome") != value.get("outcome")):
        raise PlanError("result_unknown")
    receipt = None
    # The caller's Session is intentionally obtained from the mapped plan so this
    # helper remains a single gate for execution identity and domain evidence.
    from sqlalchemy.orm import object_session
    session = object_session(plan)
    if session is not None:
        receipt = session.query(DBCapabilityEffectReceipt).filter_by(
            execution_id=expected_execution_id).populate_existing().first()
    expected_params = {
        "params": (step.precondition_json or {}).get("params", {}),
        "input_refs": (step.precondition_json or {}).get("input_refs", []),
    }
    if (receipt is None
            or receipt.capability_id != step.capability_id
            or receipt.definition_version != EFFECT_RESULT_VERSION
            or receipt.requested_by != plan.requested_by
            or receipt.instance_id != plan.instance_id
            or receipt.target_hash != effect_digest(plan.capability_snapshot_json.get("target_fingerprint"))
            or receipt.params_hash != effect_digest(expected_params)
            or receipt.result_hash != effect_digest(value["data"])
            or step.started_at is None or receipt.created_at < step.started_at):
        raise PlanError("result_unknown")
    return value["data"]


def validate_effect_reference(db, plan, step):
    """A handler envelope is not evidence until it matches its durable domain run."""
    ref = step.result_ref_json
    common_value = (
        _common_effect_payload(plan, step, ref)
        if step.capability_id in COMMON_EFFECT_CAPABILITIES else None
    )
    if step.capability_id == "diagnosis.root_cause.execute":
        return _validate_root_cause_effect(db, plan, step, ref, ref.get("inline") if isinstance(ref, dict) else None)
    if step.capability_id in {"monitor.capacity.execute", "performance.capacity.collect"}:
        return _validate_capacity_effect(db, plan, step, ref, common_value)
    if step.capability_id == "performance.baseline.collect":
        return _validate_baseline_effect(db, plan, step, ref, common_value)
    if step.capability_id in {"performance.table_profile.collect", "performance.index.collect"}:
        from app.models.index_finding import DBIndexFinding
        from app.models.table_profile import DBTableProfile
        model = DBTableProfile if step.capability_id == "performance.table_profile.collect" else DBIndexFinding
        return _validate_snapshot_effect(db, plan, step, ref, common_value, model)
    if step.capability_id == "performance.workload.collect":
        return _validate_workload_effect(db, plan, step, ref, common_value)
    if step.capability_id == "performance.sql_profile.collect":
        return _validate_sql_profile_effect(db, plan, step, ref, common_value)
    if step.capability_id == "report.document.generate":
        return _validate_report_document_effect(db, plan, step, ref, ref.get("inline") if isinstance(ref, dict) else None)
    if step.capability_id == "operations.switchover.plan":
        return _validate_switchover_effect(db, plan, step, ref, common_value)
    if step.capability_id == "monitor.alert.sync":
        return _validate_alert_import_effect(db, plan, step, ref, common_value)
    if step.capability_id == "integration.zabbix.sync":
        return _validate_zabbix_sync_effect(db, plan, step, ref, common_value)
    if step.capability_id == "integration.prometheus.sync":
        return _validate_prometheus_sync_effect(db, plan, step, ref, common_value)
    if step.capability_id == "monitor.observability.bind":
        return _validate_binding_effect(db, plan, step, ref, common_value)
    if step.capability_id in {"operations.startup.plan", "operations.shutdown.plan",
                              "protection.backup.plan", "protection.restore.plan"}:
        return _validate_plan_effect(db, plan, step, ref, common_value)
    if step.capability_id == "governance.candidate.scan":
        return _validate_candidate_effect(db, plan, step, ref, common_value)
    from app.models.inspection_job import DBInspectionJob
    from app.models.check_result import DBCheckResult
    from app.services.inspection import jobs, plan_adapter

    value = ref.get("inline") if isinstance(ref, dict) else None
    if not isinstance(value, dict) or ref.get("kind") != "inspection_job":
        raise PlanError("result_unknown")
    job = db.query(DBInspectionJob).filter_by(id=value.get("job_id")).populate_existing().first()
    execution_id = f"{plan.plan_id}:{step.seq}:{plan.capability_snapshot_json.get('revision', 1)}"
    if (job is None or job.instance_id != plan.instance_id or job.requested_by != plan.requested_by
            or job.idempotency_key != jobs._digest([plan.requested_by, plan_adapter.TRIGGER, execution_id])
            or job.status not in jobs.TERMINAL or not job.finished_at
            or value.get("instance_id") != job.instance_id or value.get("status") != job.status
            or value.get("outcome") != jobs.outcome_block(job)["outcome"]):
        raise PlanError("result_unknown")
    expected_policy = step.precondition_json["params"].get("policy_revision")
    if jobs._digest({key: item for key, item in job.policy_snapshot_json.items()
                     if key != jobs.TARGET_SNAPSHOT_KEY}) != expected_policy:
        raise PlanError("result_unknown")
    if step.status == "unknown" and value.get("error_code") != jobs.failure_code(job):
        raise PlanError("result_unknown")
    if step.status == "cancelled" or plan.outcome == "cancelled":
        terminal = jobs.last_terminal_event(job) or {}
        # A request or a reaper verdict does not prove the owner acknowledged cancellation.
        if (job.status != "cancelled" or job.stage != "cancelled" or job.cancel_requested != 1
                or job.cancel_requested_at is None or job.owner_token is not None or job.lease_until is not None
                or terminal.get("stage") != "cancelled"
                or terminal.get("failure_code") not in {"cancelled_before_run", "cancelled_during_run"}
                or value.get("error_code") != terminal["failure_code"]
                or any(isinstance(event, dict) and event.get("stage") == "completed"
                       for event in job.progress_json or [])
                or db.query(DBCheckResult.id).filter_by(task_id=job.id).first() is not None):
            raise PlanError("result_unknown")
    return True


@contextmanager
def _publication_guard(db, plan_id, actor):
    """Serialize the actual plan commit with human revision/revocation, not just preflight."""
    publication = {"flushed": False}

    def touched_rows(session):
        return [row for row in session.dirty if isinstance(row, (DBCapabilityPlan, DBCapabilityPlanStep))
                and row.plan_id == plan_id]

    def validate_commit(session):
        if not publication["flushed"] and not touched_rows(session):
            return
        lock_transaction(session, "capability_plan_state")
        stored = session.execute(select(DBCapabilityPlan.__table__).where(
            DBCapabilityPlan.plan_id == plan_id).with_for_update()).mappings().first()
        current = session.get(DBCapabilityPlan, plan_id)
        if stored is None or _hash(stored["capability_snapshot_json"]) != _hash(current.capability_snapshot_json):
            raise PlanOwnershipLost("plan_revision_conflict")
        # Refusals may still be recorded without pretending a business action ran.
        if current.status == "settled" and current.outcome in DECLARED_OUTCOMES:
            return
        frozen = SimpleNamespace(**stored)
        try:
            _check_plan(session, frozen, actor)
            if reason := plan_validation.approval_block_reason(frozen, db=session):
                raise PlanOwnershipLost(reason)
        except (PlanError, registry.CapabilityError) as exc:
            raise PlanOwnershipLost(exc.code) from exc

    def check(session):
        with session.no_autoflush:
            validate_commit(session)

    def before_flush(session, _context, _instances):
        if touched_rows(session):
            publication["flushed"] = True
            check(session)

    def reset(_session):
        if not _session.in_nested_transaction():
            publication["flushed"] = False

    event.listen(db, "before_commit", check)
    event.listen(db, "before_flush", before_flush)
    event.listen(db, "after_commit", reset)
    event.listen(db, "after_rollback", reset)
    try:
        yield
    finally:
        event.remove(db, "before_commit", check)
        event.remove(db, "before_flush", before_flush)
        event.remove(db, "after_commit", reset)
        event.remove(db, "after_rollback", reset)


def execute_plan(db, plan_id: str, *, actor: dict, execution_guard=None):
    with _publication_guard(db, plan_id, actor):
        return _execute_plan(db, plan_id, actor=actor, execution_guard=execution_guard)


def _execute_plan(db, plan_id: str, *, actor: dict, execution_guard=None):
    _guard_execution(execution_guard)
    lock_transaction(db, "capability_plan_state")
    plan = _load(db, plan_id, actor, owner_only=True)
    if "execution_identity" in plan.capability_snapshot_json:
        claim = getattr(execution_guard, "claim", None)
        if (not isinstance(claim, dict)
                or claim.get("worker_identity") != plan.capability_snapshot_json["execution_identity"]
                or claim.get("plan_id") != plan_id):
            raise PlanError("pinned_worker_required")
    resumed = None
    if plan.status == "running" and callable(getattr(execution_guard, "resume_state", None)):
        resumed = execution_guard.resume_state(db, plan)
    if plan.status == "settled" or (plan.status == "running" and resumed is None):
        db.commit()
        return _serialize(db, plan, reused=True)
    if plan.status == "awaiting_approval":
        raise PlanError("approval_required")
    if plan.status not in {"validated", "approved"} and resumed is None:
        raise PlanError("invalid_plan_state")
    start_reason = plan_validation.start_block_reason(plan, _steps(db, plan.plan_id)) if resumed is None else None
    if start_reason:
        declare_outcome(db, plan, "blocked", declared_by="plan_service.execute", reason=start_reason, kind=start_reason)
        db.commit()
        return _serialize(db, plan)
    approval_reason = plan_validation.approval_block_reason(plan, db=db)
    if approval_reason:
        if plan_validation.requires_approval(plan) and approval_reason != "approval_not_available_until_u03":
            raise PlanError(approval_reason)
        declare_outcome(db, plan, "blocked", declared_by="plan_service.execute",
                        reason=approval_reason, kind=approval_reason)
        db.commit()
        return _serialize(db, plan)
    try:
        _check_plan(db, plan, actor)
    except (PlanError, registry.CapabilityError) as exc:
        _reject_validation(db, plan, exc)
        db.commit()
        return _serialize(db, plan)
    if any(entry["kind"] in APPROVAL_KINDS for entry in plan.capability_snapshot_json["definitions"]) and execution_guard is None:
        raise PlanError("durable_execution_required")
    gate = getattr(execution_guard, "before_step", None)
    if callable(gate) and not gate(db, plan):
        return read_plan(db, plan_id, actor=actor)
    _guard_execution(execution_guard)
    claimed = 1
    if resumed is None:
        claimed = db.query(DBCapabilityPlan).filter(DBCapabilityPlan.plan_id == plan_id,
                                                   DBCapabilityPlan.status.in_(["validated", "approved"])).update(
            {DBCapabilityPlan.status: "running", DBCapabilityPlan.started_at: _now()}, synchronize_session=False)
    db.commit()
    if claimed != 1:
        return read_plan(db, plan_id, actor=actor)
    plan = _load(db, plan_id, actor, owner_only=True)
    snapshot = plan.capability_snapshot_json
    definitions = {row["seq"]: entry for row, entry in zip(snapshot["steps"], snapshot["definitions"])}
    schemas = {seq: _schema(entry) for seq, entry in definitions.items()}
    outputs, failure = resumed if resumed is not None else ({}, None)
    for row in _steps(db, plan_id):
        if resumed is not None and row.status != "pending":
            continue
        if callable(gate) and not gate(db, plan):
            return read_plan(db, plan_id, actor=actor)
        if failure:
            dependent = any(seq not in outputs for seq in row.depends_on_json)
            row.status, row.outcome_kind, row.finished_at = "skipped", (
                "dependency_not_executed" if dependent else "stopped_after_failure"), _now()
            db.commit()
            continue
        row.status, row.started_at = "running", _now()
        db.commit()
        try:
            _guard_execution(execution_guard)
            if reason := plan_validation.approval_block_reason(plan, db=db):
                raise PlanError(reason)
            _check_plan(db, plan, actor)
            values = resolve_inputs(row.precondition_json, outputs, schemas)
            entry = definitions[row.seq]
            started = monotonic()
            if entry["kind"] in APPROVAL_KINDS:
                result = registry.execute_effect(
                    db, entry["capability_id"], entry["version"], actor=actor, instance_id=plan.instance_id, params=values,
                    execution_id=f"{plan_id}:{row.seq}:{snapshot.get('revision', 1)}", execution_guard=execution_guard,
                    target_binding=snapshot.get("target_fingerprint"),
                    params_binding={"params": row.precondition_json.get("params", {}),
                                    "input_refs": row.precondition_json.get("input_refs", [])})
            else:
                if "execution_identity" in snapshot:
                    # Resolve the ordinary business handler for current principal, scope,
                    # target, definition and parameter authorization before the Host call.
                    registry.resolve_handler(db, entry["capability_id"], entry["version"],
                                             actor=actor, instance_id=plan.instance_id, params=values,
                                             _allow_engine_bridge=True)
                    if values or entry["capability_id"] not in engine_execution.READ_OPERATIONS:
                        raise PlanError("provider_params_unsupported")
                    engine_execution.check_binding(db, plan)
                    provider_values = engine_execution.bridge().invoke(
                        {"identity": snapshot["execution_identity"],
                         "provider_version": snapshot["provider_version"],
                         "provider_generation": snapshot["provider_generation"],
                         "operation": snapshot["provider_operation"]},
                        plan.instance_id, timeout=entry["timeout_seconds"])
                    # Preserve the Provider's own engine.rows/v1 inside an object
                    # projection; the existing capability declaration says object.
                    result = {"provider_result": provider_values,
                              "engine_id": snapshot["execution_identity"]["engine_id"],
                              "operation": snapshot["provider_operation"]}
                else:
                    result = registry.execute_read(db, entry["capability_id"], entry["version"], actor=actor,
                                                   instance_id=plan.instance_id, params=values)
            if monotonic() - started > entry["timeout_seconds"]:
                raise TimeoutError("read returned after its deadline")
            # End the read snapshot before checking concurrent revocation/retargeting.
            # No step result has been staged yet; running intent was committed above.
            db.rollback()
            plan = _load(db, plan_id, actor, owner_only=True)
            _check_plan(db, plan, actor)
            _guard_execution(execution_guard)
            if reason := plan_validation.approval_block_reason(plan, db=db):
                raise PlanError(reason)
            reference = _result_ref(entry, result)
            if "execution_identity" in snapshot:
                fence_epoch = getattr(execution_guard, "claim", {}).get("id", 1)
                engine_execution.save_result(db, plan, row, provider_values, fence_epoch=fence_epoch)
            if sum(len(_json(value)) for value in outputs.values()) + len(_json(result)) > 1048576:
                raise PlanError("result_budget_exceeded")
            outputs[row.seq] = result
            effect_data = result.get("data") if isinstance(result.get("data"), dict) else result
            effect_outcome = result.get("outcome") if entry["kind"] in APPROVAL_KINDS else "succeeded"
            if effect_outcome == "success":
                effect_outcome = "succeeded"
            if effect_outcome == "cancelled" and effect_data.get("error_code") == "cancelled_force_closed":
                effect_outcome = "unknown"
            if effect_outcome in {"succeeded", "partial"}:
                row.result_ref_json, row.status = reference, "succeeded"
                if effect_outcome == "partial":
                    failure = "partial"
                    row.status = "partial"
                    row.outcome_kind = "inspection_partial"
            else:
                failure = effect_outcome if effect_outcome in {"failed", "unknown", "expired", "cancelled"} else "unknown"
                row.status = failure
                row.outcome_kind = effect_data.get("error_code") or f"inspection_{failure}"
                row.result_ref_json = reference
            if entry["kind"] in APPROVAL_KINDS:
                validate_effect_reference(db, plan, row)
        except PlanOwnershipLost:
            db.rollback()
            raise
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("capability_plan_storage_error plan_id=%s", plan_id)
            raise PlanError("plan_storage_unavailable") from exc
        except Exception as exc:
            failure = "unknown" if isinstance(exc, TimeoutError) or getattr(exc, "code", None) == "result_unknown" else "failed"
            row.status = failure
            row.result_ref_json = None
            row.outcome_kind = getattr(exc, "code", None) or ("timeout" if isinstance(exc, TimeoutError) else "handler_error")
        row.finished_at = _now()
        db.commit()
    rows = _steps(db, plan_id)
    _guard_execution(execution_guard)
    outcome = "partial" if failure and any(row.status == "succeeded" for row in rows) else failure or "succeeded"
    settle(db, plan, outcome, kind=next((row.outcome_kind for row in rows if row.outcome_kind), None))
    db.commit()
    return read_plan(db, plan_id, actor=actor)


def cancel_plan(db, plan_id: str, *, actor: dict):
    lock_transaction(db, "capability_plan_state")
    plan = _load(db, plan_id, actor, owner_only=True)
    if plan.status == "settled":
        db.commit()
        return _serialize(db, plan, reused=True)
    if plan.status not in {"draft", "validated"}:
        raise PlanError("running_cancellation_not_supported")
    claimed = db.query(DBCapabilityPlan).filter(DBCapabilityPlan.plan_id == plan_id,
                                               DBCapabilityPlan.status.in_(["draft", "validated"])).update(
        {DBCapabilityPlan.status: "draft"}, synchronize_session=False)
    if claimed != 1:
        db.rollback()
        raise PlanError("plan_state_changed")
    db.refresh(plan)
    for row in _steps(db, plan_id):
        row.status, row.outcome_kind, row.finished_at = "cancelled", "cancelled_before_run", _now()
    settle(db, plan, "cancelled", kind="cancelled_before_run")
    db.commit()
    return _serialize(db, plan)
