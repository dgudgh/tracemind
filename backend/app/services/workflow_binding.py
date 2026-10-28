"""Canonical per-plan recipe bindings; execution still belongs to plan_service.

Integration contract for workflow_service:
    make_binding(candidate, index) -> detached canonical binding for ONE plan.
Pass that binding as workflow_binding= together with candidate.plan_inputs[index]
to create_plan/revise_plan. Never use the invocation candidate_hash as this
binding's candidate_hash: the invocation identity belongs in admission_hash.
"""
from __future__ import annotations

from copy import deepcopy
import re

from app.services import workflow_library as library


class WorkflowBindingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_FIELDS = {"workflow_id", "workflow_version", "definition_hash", "recipe_snapshot", "input", "candidate_hash"}
_FROZEN = {"policy_revision", "runtime_digest", "target_fingerprint"}
_RECIPE_FIELDS = {"schema", "workflow_id", "workflow_version", "title", "slots_schema", "scope_modes",
                  "max_targets", "failure_policy", "valid_for_seconds", "request_approval",
                  "runtime_checks_required", "release_gate", "steps", "definition_hash"}
_STEP_FIELDS = {"seq", "capability_id", "definition_version", "slot_group", "depends_on", "input_refs", "fanout", "retries"}


def _fail(code="workflow_binding_invalid"):
    raise WorkflowBindingError(code)


def _same(left, right):
    return library._json(left) == library._json(right)


def _digest(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _single(candidate):
    """Build only from a complete, freshly compiled single-target candidate."""
    if candidate["missing_fields"] or len(candidate["plan_inputs"]) != 1:
        _fail()
    return {**deepcopy(candidate["workflow_binding"]),
            "input": {"slots": deepcopy(candidate["slots"]), "scope": deepcopy(candidate["scope"])},
            "candidate_hash": candidate["candidate_hash"]}


def make_binding(candidate, index):
    """Return canonical per-plan binding; no operation/source or sibling targets.

    candidate must be the unchanged compile_workflow result. index is a zero-based
    plan_inputs index. Effective defaults/local overrides are folded into slots;
    scope contains exactly this target, or {"mode": "global"}. All return values
    are detached. The caller retains the full invocation hash for admission only.
    Raises WorkflowBindingError(code), never creates/authorizes/executes a plan.
    """
    try:
        if type(candidate) is not dict or type(index) is not int or index < 0:
            _fail()
        ref = candidate["workflow_binding"]
        compiled = library.compile_workflow(ref["workflow_id"], ref["workflow_version"],
                                            slots=candidate["slots"], scope=candidate["scope"])
        if not _same(compiled, candidate) or compiled["missing_fields"] or index >= len(compiled["plan_inputs"]):
            _fail()
        plan = compiled["plan_inputs"][index]
        recipe = compiled["workflow_binding"]["recipe_snapshot"]
        slots = (deepcopy(plan["steps"][0]["params"]) if len(recipe["steps"]) == 1 else
                 {step["slot_group"]: deepcopy(values["params"])
                  for step, values in zip(recipe["steps"], plan["steps"])})
        scope = ({"mode": "global"} if plan["instance_id"] is None else
                 {"mode": "explicit", "targets": [{"instance_id": plan["instance_id"]}]})
        return _single(library.compile_workflow(ref["workflow_id"], ref["workflow_version"], slots=slots, scope=scope))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, WorkflowBindingError):
            raise
        raise WorkflowBindingError(getattr(exc, "code", "workflow_binding_invalid")) from exc


def _frozen_input(binding):
    """Reconstruct from stored JSON only; do not consult today's recipe/models."""
    if type(binding) is not dict or set(binding) != _FIELDS:
        _fail()
    library._json(binding)
    recipe = binding["recipe_snapshot"]
    if (type(recipe) is not dict or set(recipe) != _RECIPE_FIELDS
            or recipe["schema"] != "capability.workflow/v1"
            or type(binding["workflow_id"]) is not str or not binding["workflow_id"]
            or type(binding["workflow_version"]) is not int or binding["workflow_version"] < 1
            or recipe["workflow_id"] != binding["workflow_id"]
            or type(recipe["workflow_version"]) is not int or recipe["workflow_version"] != binding["workflow_version"]
            or not _digest(binding["definition_hash"]) or recipe["definition_hash"] != binding["definition_hash"]
            or library._hash({key: value for key, value in recipe.items() if key != "definition_hash"}) != binding["definition_hash"]
            or recipe["failure_policy"] != "stop" or type(recipe["request_approval"]) is not bool
            or type(recipe["valid_for_seconds"]) is not int or not 60 <= recipe["valid_for_seconds"] <= 86400
            or type(recipe["title"]) is not str or not recipe["title"]
            or type(recipe["steps"]) is not list or not 1 <= len(recipe["steps"]) <= 20
            or type(binding["input"]) is not dict or set(binding["input"]) != {"slots", "scope"}):
        _fail()
    slots, scope = binding["input"]["slots"], binding["input"]["scope"]
    if type(slots) is not dict or type(scope) is not dict:
        _fail()
    if scope == {"mode": "global"}:
        instance_id = None
    else:
        if (set(scope) != {"mode", "targets"} or scope["mode"] != "explicit"
                or type(scope["targets"]) is not list or len(scope["targets"]) != 1
                or type(scope["targets"][0]) is not dict or set(scope["targets"][0]) != {"instance_id"}):
            _fail()
        instance_id = scope["targets"][0]["instance_id"]
        if type(instance_id) is not int or not 1 <= instance_id <= 2**63 - 1:
            _fail()
    if type(recipe["scope_modes"]) is not list or scope["mode"] not in recipe["scope_modes"]:
        _fail()
    groups = [step.get("slot_group") for step in recipe["steps"]]
    if len(groups) > 1 and (any(type(group) is not str for group in groups)
                            or len(set(groups)) != len(groups) or set(slots) != set(groups)):
        _fail()
    steps = []
    for seq, skeleton in enumerate(recipe["steps"], 1):
        if (type(skeleton) is not dict or set(skeleton) != _STEP_FIELDS
                or type(skeleton["seq"]) is not int or skeleton["seq"] != seq
                or type(skeleton["capability_id"]) is not str
                or type(skeleton["definition_version"]) is not int or skeleton["definition_version"] < 1
                or skeleton["depends_on"] != ([seq - 1] if seq > 1 else [])
                or skeleton["input_refs"] != {} or type(skeleton["fanout"]) is not int or skeleton["fanout"] != 1
                or type(skeleton["retries"]) is not int or skeleton["retries"] != 0):
            _fail()
        params = slots if len(groups) == 1 else slots[skeleton["slot_group"]]
        schema = recipe["slots_schema"] if len(groups) == 1 else recipe["slots_schema"]["properties"][skeleton["slot_group"]]
        if type(params) is not dict or set(params) != set(schema["properties"]):
            _fail()
        steps.append({**{key: deepcopy(value) for key, value in skeleton.items() if key != "slot_group"},
                      "title": recipe["title"], "params": deepcopy(params)})
    identity = {"workflow_id": binding["workflow_id"], "workflow_version": binding["workflow_version"],
                "definition_hash": binding["definition_hash"], "scope_mode": scope["mode"],
                "effective_inputs": [{"instance_id": instance_id, "step_params": [step["params"] for step in steps]}]}
    if not _digest(binding["candidate_hash"]) or library._hash(identity) != binding["candidate_hash"]:
        _fail()
    return {"capability_id": steps[0]["capability_id"], "definition_version": steps[0]["definition_version"],
            "instance_id": instance_id, "steps": steps, "valid_for_seconds": recipe["valid_for_seconds"],
            "request_approval": recipe["request_approval"]}


def validate_frozen_binding(binding, *, instance_id, steps):
    """Validate frozen selfhash/candidate/steps without any current catalog read."""
    try:
        expected = _frozen_input(binding)
        actual = deepcopy(steps)
        if type(actual) is not list:
            _fail()
        for step in actual:
            if step.get("capability_id") == "inspection.run.execute" and step.get("definition_version") == 2:
                for field in _FROZEN:
                    if field in step["params"]:
                        if not _digest(step["params"][field]):
                            _fail()
                        del step["params"][field]
        if type(instance_id) is not type(expected["instance_id"]) or instance_id != expected["instance_id"] or not _same(actual, expected["steps"]):
            _fail()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, WorkflowBindingError):
            raise
        raise WorkflowBindingError("workflow_binding_invalid") from exc


def validate_request(binding, *, capability_id, definition_version, instance_id=None, params=None,
                     steps=None, valid_for_seconds=None, request_approval=False):
    """Return canonical binding only when the pre-freeze request equals compile.

    Creation/revision use this before hashes and inspection v2 freezing. Omitted
    TTL means 3600; all other recipe business inputs must be exactly compiled.
    """
    try:
        expected = _frozen_input(binding)
        current = library.compile_workflow(binding["workflow_id"], binding["workflow_version"], **binding["input"])
        if not _same(_single(current), binding):
            _fail("workflow_definition_changed")
        actual_steps = steps
        if steps is None and len(expected["steps"]) == 1:
            actual_steps = [{**expected["steps"][0], "params": params or {}}]
        elif params not in (None, {}):
            _fail("workflow_input_mismatch")
        actual = {"capability_id": capability_id, "definition_version": definition_version, "instance_id": instance_id,
                  "steps": actual_steps, "valid_for_seconds": 3600 if valid_for_seconds is None else valid_for_seconds,
                  "request_approval": request_approval}
        if not _same(expected, actual):
            _fail("workflow_input_mismatch")
        return deepcopy(binding)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, WorkflowBindingError):
            raise
        raise WorkflowBindingError(getattr(exc, "code", "workflow_binding_invalid")) from exc


def check_current(binding, *, instance_id=None, steps=None):
    """Execution gate; raise PlanError so service rollback preserves typed errors.

    A stored plan supplies instance_id and steps. Missing/invalid binding or
    projection is rejected, including check_current(None), never a raw TypeError.
    """
    from app.services.plan_service import PlanError

    try:
        validate_frozen_binding(binding, instance_id=instance_id, steps=steps)
        expected = _frozen_input(binding)
        validate_request(binding, **expected)
    except WorkflowBindingError as exc:
        raise PlanError(exc.code) from exc
