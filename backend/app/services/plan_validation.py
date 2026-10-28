"""Shared, side-effect-free checks for frozen plans; never issue an approval."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from pydantic import ValidationError

from app.services import capability_registry as registry
from app.services.capability_handler import PARAM_SCHEMAS
from app.services.plan_dag import PlanDAGError, normalize_steps, validate_steps


APPROVAL_KINDS = frozenset({"collect", "scan", "refresh", "sync", "bind", "execute", "plan"})


class PlanValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def binding_hash(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def requires_approval(plan):
    return (plan.capability_snapshot_json.get("approval_requested") is True
            or any(entry["kind"] in APPROVAL_KINDS for entry in plan.capability_snapshot_json["definitions"]))


def validity_deadline(snapshot):
    value = snapshot.get("validity")
    if value is None:
        return None  # Historical plans retain their explicitly unspecified validity policy.
    if (not isinstance(value, dict) or set(value) != {"seconds", "expires_at"}
            or type(value["seconds"]) is not int or not 60 <= value["seconds"] <= 86400
            or not isinstance(value["expires_at"], str)):
        raise PlanValidationError("plan_snapshot_invalid")
    try:
        deadline = datetime.fromisoformat(value["expires_at"])
        if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
            raise ValueError("UTC required")
        return deadline.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, OverflowError) as exc:
        raise PlanValidationError("plan_snapshot_invalid") from exc


def _definition_shape(entry):
    if not isinstance(entry, dict):
        return False
    if (not isinstance(entry.get("capability_id"), str)
            or len(entry["capability_id"]) > 64 or not registry.ID_PATTERN.fullmatch(entry["capability_id"])
            or type(entry.get("version")) is not int or entry["version"] < 1
            or entry.get("kind") not in registry.KINDS
            or entry.get("execution") not in {"server", "client"}
            or (entry.get("handler") is not None and not isinstance(entry["handler"], str))
            or entry.get("handler_status") not in {"wired", "client_bound", "declared_not_wired"}
            or type(entry.get("requires_instance")) is not bool
            or not isinstance(entry.get("title"), str)
            or not isinstance(entry.get("output_schema"), str)
            or not isinstance(entry.get("budget"), dict)
            or type(entry.get("timeout_seconds")) is not int or entry["timeout_seconds"] < 1
            or not isinstance(entry.get("engines"), (list, tuple))
            or not all(isinstance(engine, str) for engine in entry["engines"])
            or not isinstance(entry.get("bindings"), (list, tuple))):
        return False
    for binding in entry["bindings"]:
        if not isinstance(binding, dict):
            return False
        endpoints = binding.get("endpoints") or (binding,)
        if not isinstance(endpoints, (list, tuple)):
            return False
        if not all(isinstance(endpoint, dict) and isinstance(endpoint.get("endpoint_template"), str)
                   and isinstance(endpoint.get("method", "GET"), str) for endpoint in endpoints):
            return False
    return True


def validate_frozen_plan(plan, revision, rows):
    """Check stored projections without consulting today's definition version.

    Historical results remain readable after a normal code upgrade. Current
    authorization and execution freshness remain the caller's separate checks.
    """
    snapshot = plan.capability_snapshot_json
    try:
        validity_deadline(snapshot) if isinstance(snapshot, dict) else None
        if (not isinstance(snapshot, dict) or snapshot.get("schema") != "capability.plan/v1"
                or snapshot.get("failure_policy") != "stop"
                or ("approval_requested" in snapshot and type(snapshot["approval_requested"]) is not bool)
                or ("revision" in snapshot and (type(snapshot["revision"]) is not int or snapshot["revision"] < 1))
                or not isinstance(snapshot.get("definitions"), list)
                or not isinstance(snapshot.get("steps"), list)
                or not 1 <= len(snapshot["steps"]) <= 20
                or len(snapshot["definitions"]) != len(snapshot["steps"])
                or not isinstance(snapshot.get("request_hash"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", snapshot["request_hash"])
                or "target_fingerprint" not in snapshot
                or (snapshot["target_fingerprint"] is not None
                    and (not isinstance(snapshot["target_fingerprint"], str)
                         or not re.fullmatch(r"[a-f0-9]{64}", snapshot["target_fingerprint"])))
                or not all(_definition_shape(entry) for entry in snapshot["definitions"])):
            raise PlanValidationError("plan_snapshot_invalid")
        identity = {"requested_by": plan.requested_by, "instance_id": plan.instance_id,
                    "capability_id": plan.capability_id, "definition_version": plan.definition_version}
        if (revision is None or revision.created_by != plan.requested_by
                or revision.revision != snapshot.get("revision", 1)
                or revision.binding_hash != binding_hash(snapshot)
                or revision.snapshot_json != snapshot or snapshot.get("identity") != identity
                or normalize_steps(snapshot["steps"]) != snapshot["steps"]
                or snapshot["steps"][0]["capability_id"] != plan.capability_id
                or snapshot["steps"][0]["definition_version"] != plan.definition_version
                or len(rows) != len(snapshot["steps"])):
            raise PlanValidationError("plan_snapshot_invalid")
        if "workflow_binding" in snapshot:
            from app.services.workflow_binding import validate_frozen_binding
            validate_frozen_binding(snapshot["workflow_binding"], instance_id=plan.instance_id,
                                    steps=snapshot["steps"])
        for row, frozen, entry in zip(rows, snapshot["steps"], snapshot["definitions"]):
            if (row.precondition_json != frozen or row.seq != frozen["seq"]
                    or row.depends_on_json != frozen["depends_on"] or row.title != frozen["title"]
                    or row.capability_id != entry["capability_id"]
                    or frozen["capability_id"] != entry["capability_id"]
                    or frozen["definition_version"] != entry["version"]
                    or row.side_effect != ("write" if entry["kind"] in APPROVAL_KINDS else "read")):
                raise PlanValidationError("plan_snapshot_invalid")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PlanValidationError("plan_snapshot_invalid") from exc


def validate_step_contracts(steps, definitions, schemas, *, engine=None):
    """Use the same DAG, budget and parameter checks at creation and execution."""
    try:
        budget = {key: min(entry["budget"][key] for entry in definitions)
                  for key in ("max_steps", "max_fanout", "max_retries")}
        normalized = validate_steps(steps, budget=budget, schemas=schemas)
    except PlanDAGError as exc:
        raise PlanValidationError(exc.code) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanValidationError("plan_snapshot_invalid") from exc
    for step, entry in zip(normalized, definitions):
        schema = PARAM_SCHEMAS.get(entry.get("handler"))
        if schema is not None:
            probe = dict(step["params"])
            if entry["capability_id"] == "inspection.run.execute" and "policy_id" not in probe:
                # Only type-check the remaining fields. The literal is not saved or
                # executed: _check_plan will persist a missing-policy declaration.
                probe["policy_id"] = 1
            for name, ref in step["input_refs"].items():
                probe[name] = {"integer": 1, "string": "", "boolean": False}[ref["type"]]
            try:
                schema.model_validate(probe)
            except ValidationError as exc:
                raise PlanValidationError("invalid_params") from exc
            if (entry["capability_id"] == "operations.capability_matrix.read" and engine is not None
                    and probe.get("engine") is not None and probe["engine"] != engine):
                raise PlanValidationError("invalid_params")
        elif step["params"]:
            raise PlanValidationError("input_schema_not_wired")
    return normalized


def approval_block_reason(plan, db=None):
    # Derive this from the frozen declarations, never a mutable approval flag.
    if requires_approval(plan):
        if any(entry["execution"] != "server" or entry["handler_status"] != "wired"
               for entry in plan.capability_snapshot_json["definitions"]):
            return "approval_not_available_until_u03"
        from app.services.plan_governance import approval_problem
        return approval_problem(db, plan)
    if plan.approval_state != "not_required":
        return "approval_metadata_invalid"
    if any(getattr(plan, field) is not None for field in (
        "approval_binding_hash", "approved_by", "approved_at", "approval_expires_at", "revoked_at", "revoked_by",
    )):
        return "approval_metadata_invalid"
    return None


def start_block_reason(plan, rows):
    if (plan.started_at is not None or plan.finished_at is not None or plan.result_ref_json is not None
            or plan.outcome_kind is not None or plan.outcome_reason is not None or plan.outcome_declared_by is not None):
        return "plan_steps_not_pending"
    if not rows or any(row.status != "pending" or row.result_ref_json is not None
                       or row.started_at is not None or row.finished_at is not None
                       or row.outcome_kind is not None for row in rows):
        return "plan_steps_not_pending"
    return None
