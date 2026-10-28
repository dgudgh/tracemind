"""Pure structural gate for untrusted model proposals, never an execution channel."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata

from app.services import workflow_library as library


MAX_RAW_BYTES = 16384
_MODEL_KEYS = frozenset({"decision", "workflow_id", "workflow_version", "slots"})
_MODEL_FORBIDDEN_SLOTS = frozenset({
    "policy_id", "trigger_type", "policy_revision", "runtime_digest", "target_fingerprint",
})


def _fail(code, field=None):
    raise library.WorkflowError("workflow_proposal_" + code, fields=(field,) if field else ()) from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _bounded(value, *, max_nodes=12000):
    nodes, characters = 0, 0

    def visit(item, depth=0):
        nonlocal nodes, characters
        nodes += 1
        if nodes > max_nodes or depth > 12:
            _fail("budget_exceeded")
        if type(item) is dict:
            if len(item) > 64:
                _fail("budget_exceeded")
            for key, child in item.items():
                if type(key) is not str:
                    _fail("invalid_json")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            if len(item) > 1000:
                _fail("budget_exceeded")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            characters += len(item)
            if len(item) > 4096 or characters > 65536:
                _fail("budget_exceeded")
            if any(unicodedata.category(char).startswith("C") for char in item):
                _fail("invalid_json")
        elif type(item) is float:
            if not math.isfinite(item):
                _fail("invalid_json")
        elif type(item) is int:
            if item.bit_length() > 128:
                _fail("budget_exceeded")
        elif item is not None and type(item) is not bool:
            _fail("invalid_json")

    visit(value)
    encoded = _canonical(value)
    if len(encoded) > 131072:
        _fail("budget_exceeded")
    return json.loads(encoded)


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            _fail("duplicate_key")
        value[key] = child
    return value


def _constant(value):
    _fail("invalid_json")


def _decode(raw_text):
    if not isinstance(raw_text, str):
        _fail("invalid_json")
    raw_text = str.__str__(raw_text)
    if len(raw_text) > MAX_RAW_BYTES:
        _fail("budget_exceeded")
    if not raw_text.strip():
        _fail("invalid_json")
    try:
        size = len(raw_text.encode("utf-8"))
    except UnicodeError:
        _fail("invalid_json")
    if size > MAX_RAW_BYTES:
        _fail("budget_exceeded")
    depth, quoted, escaped = 0, False, False
    for char in raw_text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > 12:
                _fail("budget_exceeded")
        elif char in "}]":
            depth -= 1
            if depth < 0:
                _fail("invalid_json")
    try:
        value = json.loads(raw_text, object_pairs_hook=_pairs, parse_constant=_constant)
    except library.WorkflowError:
        raise
    except (ValueError, TypeError, RecursionError):
        _fail("invalid_json")
    value = _bounded(value, max_nodes=4096)
    if type(value) is not dict:
        _fail("invalid_json")
    return value


def _proposal(raw_text):
    value = _decode(raw_text)
    if set(value) - _MODEL_KEYS or "decision" not in value:
        _fail("forbidden_field", "proposal")
    decision = value["decision"]
    if type(decision) is not str or decision not in {"matched", "needs_input", "not_matched"}:
        _fail("invalid_decision", "decision")
    if decision == "matched" and set(value) != _MODEL_KEYS:
        _fail("invalid_reference", "workflow")
    if ("workflow_id" in value) != ("workflow_version" in value):
        _fail("invalid_reference", "workflow")
    name, version = value.get("workflow_id"), value.get("workflow_version")
    if name is not None or version is not None:
        if (type(name) is not str or len(name) > 64 or library.WORKFLOW_ID_PATTERN.fullmatch(name) is None
                or type(version) is not int or not 1 <= version <= 2147483647):
            _fail("invalid_reference", "workflow")
    elif decision == "matched":
        _fail("invalid_reference", "workflow")
    slots = value.get("slots", {})
    if type(slots) is not dict:
        _fail("invalid_slots", "slots")
    if (name is None and slots) or (decision == "not_matched" and (name is not None or slots)):
        _fail("invalid_decision", "decision")
    return {"decision": decision, "workflow_id": name, "workflow_version": version, "slots": slots}


def _catalog(catalog):
    value = _bounded(catalog)
    if (type(value) is not dict or set(value) != {"schema", "items"}
            or value["schema"] != "capability.workflow_catalog/v1"
            or type(value["items"]) is not list or len(value["items"]) > 32):
        _fail("invalid_catalog", "catalog")
    entries = {}
    for item in value["items"]:
        if type(item) is not dict:
            _fail("invalid_catalog", "catalog")
        try:
            expected = library.get_workflow(item.get("workflow_id"), item.get("workflow_version"))
        except library.WorkflowError as exc:
            if exc.code == "workflow_definition_changed":
                raise
            _fail("invalid_catalog", "catalog")
        key = (expected["workflow_id"], expected["workflow_version"])
        if key in entries or _canonical(expected) != _canonical(item):
            _fail("catalog_changed", "catalog")
        entries[key] = item
    return entries, _hash([entries[key] for key in sorted(entries)])


def _id(value):
    return type(value) is int and 1 <= value <= 2**63 - 1


def _ids(values, field):
    if (type(values) is not list or len(values) > 1000 or any(not _id(value) for value in values)
            or len(set(values)) != len(values)):
        _fail("invalid_selection", field)
    return frozenset(values)


def _scope(scope, targets, policies, common):
    value = _bounded(scope)
    if common is not None and (not _id(common) or common not in policies):
        _fail("selection_not_allowed", "confirmed_policy_id")
    if type(value) is not dict or set(value) - {"mode", "targets"}:
        _fail("invalid_scope", "scope")
    if value == {}:
        return value
    if value.get("mode") == "global":
        if set(value) != {"mode"}:
            _fail("invalid_scope", "scope")
        return value
    if value.get("mode") != "explicit":
        _fail("invalid_scope", "scope")
    chosen = value.get("targets", [])
    if type(chosen) is not list or len(chosen) > 100:
        _fail("invalid_scope", "scope.targets")
    seen = set()
    for target in chosen:
        if (type(target) is not dict or set(target) - {"instance_id", "slots"}
                or not _id(target.get("instance_id")) or type(target.get("slots", {})) is not dict
                or set(target.get("slots", {})) - {"policy_id"}):
            _fail("invalid_scope", "scope.targets")
        identity = target["instance_id"]
        if identity not in targets:
            _fail("selection_not_allowed", "scope.targets")
        if identity in seen:
            _fail("invalid_scope", "scope.targets")
        seen.add(identity)
        if "policy_id" in target.get("slots", {}):
            policy = target["slots"]["policy_id"]
            if not _id(policy) or policy not in policies:
                _fail("selection_not_allowed", "scope.targets.slots.policy_id")
            if common is not None:
                _fail("selection_conflict", "confirmed_policy_id")
    return value


def _model_slots(slots, schema):
    if type(slots) is not dict:
        _fail("invalid_slots", "slots")
    properties = schema["properties"]
    if set(slots) - set(properties) or set(slots) & _MODEL_FORBIDDEN_SLOTS:
        _fail("forbidden_field", "slots")
    for key, value in slots.items():
        child = properties[key]
        if child.get("type") == "object":
            _model_slots(value, child)


def validate_proposal(raw_text: str, *, catalog: dict, scope: dict,
                      allowed_target_ids: list[int], allowed_policy_ids: list[int],
                      confirmed_policy_id: int | None = None, validator_version: int = 1) -> dict:
    """Validate structure against a trusted snapshot; no live authorization or intent proof."""
    if type(validator_version) is not int or validator_version != 1:
        _fail("validator_not_found")
    proposal = _proposal(raw_text)
    entries, catalog_hash = _catalog(catalog)
    targets = _ids(allowed_target_ids, "allowed_target_ids")
    policies = _ids(allowed_policy_ids, "allowed_policy_ids")
    chosen = _scope(scope, targets, policies, confirmed_policy_id)
    candidate, reference, missing = None, None, []
    decision = proposal["decision"]
    if proposal["workflow_id"] is not None:
        definition = entries.get((proposal["workflow_id"], proposal["workflow_version"]))
        if definition is None:
            _fail("not_allowed", "workflow")
        reference = {key: definition[key] for key in ("workflow_id", "workflow_version", "definition_hash")}
        _model_slots(proposal["slots"], definition["slots_schema"])
        has_policy = "policy_id" in definition["slots_schema"]["properties"]
        local_policy = any("policy_id" in target.get("slots", {}) for target in chosen.get("targets", []))
        if not has_policy and (confirmed_policy_id is not None or local_policy):
            _fail("selection_conflict", "confirmed_policy_id")
        slots = {**proposal["slots"]}
        if confirmed_policy_id is not None:
            slots["policy_id"] = confirmed_policy_id
        try:
            compiled = library.compile_workflow(reference["workflow_id"], reference["workflow_version"],
                                                 slots=slots, scope=chosen)
        except library.WorkflowError as exc:
            raise library.WorkflowError(exc.code, fields=exc.fields) from None
        missing = compiled["missing_fields"]
        if decision == "needs_input":
            missing = sorted({*missing, "intent_confirmation"})
        elif missing:
            decision = "needs_input"
        else:
            candidate = compiled
    elif decision == "needs_input":
        missing = ["workflow"]
    identity = {"validator_version": validator_version, "proposal": proposal, "catalog_hash": catalog_hash,
                "scope": chosen, "allowed_target_ids": sorted(targets), "allowed_policy_ids": sorted(policies),
                "confirmed_policy_id": confirmed_policy_id}
    response = {"schema": "capability.workflow_proposal/v1", "validator_version": validator_version,
                "accepted": False, "model_decision": proposal["decision"], "decision": decision,
                "workflow": reference, "missing_fields": missing, "candidate": candidate,
                "catalog_hash": catalog_hash, "proposal_hash": _hash(identity),
                "validation_scope": "structure_only", "requires_confirmation": True}
    return json.loads(_canonical(response))
