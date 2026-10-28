"""Versioned, static candidates only. Authorization and execution belong to plans."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from pydantic import ValidationError


WORKFLOW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$")


class WorkflowError(ValueError):
    def __init__(self, code: str, *, fields: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.fields = fields


@dataclass(frozen=True)
class _Step:
    capability_id: str
    version: int
    model_key: str
    group: str | None = None


@dataclass(frozen=True)
class _Recipe:
    workflow_id: str
    title: str
    steps: tuple[_Step, ...]
    global_read: bool = True
    effect: bool = False


_JOBS = _Step("inspection.run.query", 1, "inspection.list_jobs")
_TEMPLATES = _Step("inspection.template.read", 1, "inspection.list_policies")
_MATRIX = _Step("operations.capability_matrix.read", 1, "operations.capability_matrix")
_RECIPES = (
    _Recipe("inspection.jobs.list", "Saved inspection jobs", (_JOBS,)),
    _Recipe("inspection.templates.list", "Inspection templates", (_TEMPLATES,)),
    _Recipe("operations.capabilities.list", "Engine capabilities", (_MATRIX,)),
    _Recipe("inspection.context.read", "Inspection context", (
        _Step(_MATRIX.capability_id, 1, _MATRIX.model_key, "matrix"),
        _Step(_TEMPLATES.capability_id, 1, _TEMPLATES.model_key, "templates"),
        _Step(_JOBS.capability_id, 1, _JOBS.model_key, "jobs"),
    ), global_read=False),
    _Recipe("inspection.collect", "Prepare inspection collection", (
        _Step("inspection.run.execute", 2, "inspection.run_frozen"),
    ), global_read=False, effect=True),
)
_FROZEN_FIELDS = frozenset({"policy_revision", "runtime_digest", "target_fingerprint"})
# Frozen v1 identities. A semantic change requires a new recipe version, not a replacement hash.
_PUBLISHED_HASHES = (
    ("inspection.jobs.list", 1, "35b844072e1937d6226361b9d74b44da7e5ca0e8358db7e2d43fbefdf8ce5453"),
    ("inspection.templates.list", 1, "aaf90e4f66069c1ca282b40f2d55b5c83a783a53c7a4852dd18126604bcef6ed"),
    ("operations.capabilities.list", 1, "6293c4bd02bf18aa642f614ecf7099bc9d767addbc0e565aa73e01dfc7ca39b3"),
    ("inspection.context.read", 1, "eee95f21c7ffa0f1749536897e4141cc408f18aeb170baaea27aba1d3e060f57"),
    ("inspection.collect", 1, "c6162a7a6c67e1c181305220ca978f423e1913ecb0698f875bcec5586d238c30"),
)


def _json(value) -> str:
    budget = [0, 0]

    def check(item, depth=0):
        budget[0] += 1
        if depth > 12 or budget[0] > 50000:
            raise WorkflowError("workflow_invalid_json")
        if type(item) is dict:
            if len(item) > 128:
                raise WorkflowError("workflow_input_too_large")
            for key, child in item.items():
                if type(key) is not str:
                    raise WorkflowError("workflow_invalid_json")
                check(key, depth + 1)
                check(child, depth + 1)
        elif type(item) is list:
            if len(item) > 1000:
                raise WorkflowError("workflow_input_too_large")
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            budget[1] += len(item)
            if len(item) > 16000 or budget[1] > 200000:
                raise WorkflowError("workflow_input_too_large")
        elif item is not None and type(item) not in (str, int, float, bool):
            raise WorkflowError("workflow_invalid_json")

    check(value)
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowError("workflow_invalid_json") from exc
    if len(result) > 200000:
        raise WorkflowError("workflow_input_too_large")
    return result


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _recipe(workflow_id, version) -> _Recipe:
    if type(workflow_id) is not str or type(version) is not int:
        raise WorkflowError("workflow_invalid_reference")
    for recipe in _RECIPES:
        if recipe.workflow_id == workflow_id and version == 1:
            return recipe
    raise WorkflowError("workflow_not_found")


def _model(step):
    # Reuse the exact registry input types, never the handlers or live registry.
    from app.services.capability_handler import PARAM_SCHEMAS

    return PARAM_SCHEMAS[step.model_key]


def _slot_schema(step) -> dict:
    schema = _model(step).model_json_schema()
    for field in _FROZEN_FIELDS:
        schema["properties"].pop(field, None)
    if step == _JOBS or step.model_key == _JOBS.model_key:
        from app.services.inspection.jobs import ALL_STATUSES
        from app.services.run_status import OUTCOMES

        schema["properties"]["status"]["x-csv-values"] = sorted(ALL_STATUSES)
        schema["properties"]["outcome"]["x-csv-values"] = sorted(OUTCOMES)
    return schema


def _definition_json(workflow_id: str, version: int) -> str:
    recipe = _recipe(workflow_id, version)
    schemas = [_slot_schema(step) for step in recipe.steps]
    if len(recipe.steps) == 1:
        slots = schemas[0]
    else:
        slots = {"type": "object", "additionalProperties": False,
                 "properties": {step.group: schema for step, schema in zip(recipe.steps, schemas)}}
    value = {
        "schema": "capability.workflow/v1", "workflow_id": workflow_id,
        "workflow_version": version, "title": recipe.title,
        "slots_schema": slots,
        "scope_modes": ["global", "explicit"] if recipe.global_read else ["explicit"],
        "max_targets": 100, "failure_policy": "stop", "valid_for_seconds": 3600,
        "request_approval": recipe.effect,
        "runtime_checks_required": True,
        "release_gate": "inspection_plan" if recipe.effect else None,
        "steps": [{"seq": i, "capability_id": step.capability_id,
                   "definition_version": step.version, "slot_group": step.group,
                   "depends_on": [i - 1] if i > 1 else [],
                   "input_refs": {}, "fanout": 1, "retries": 0}
                  for i, step in enumerate(recipe.steps, 1)],
    }
    value["definition_hash"] = _hash(value)
    expected = next((digest for name, number, digest in _PUBLISHED_HASHES
                     if name == workflow_id and number == version), None)
    if value["definition_hash"] != expected:
        raise WorkflowError("workflow_definition_changed")
    return _json(value)


def get_workflow(workflow_id: str, workflow_version: int) -> dict:
    """Return a detached JSON tree after checking the pinned identity on every call."""
    _recipe(workflow_id, workflow_version)
    return json.loads(_definition_json(workflow_id, workflow_version))


def list_workflows() -> dict:
    return {"schema": "capability.workflow_catalog/v1",
            "items": [get_workflow(recipe.workflow_id, 1) for recipe in _RECIPES]}


def _keys(value, allowed, path):
    if type(value) is not dict:
        raise WorkflowError("workflow_invalid_input", fields=(path,))
    unknown = set(value) - set(allowed)
    if unknown:
        raise WorkflowError("workflow_unknown_field",
                            fields=tuple(sorted(f"{path}.{key}" for key in unknown)))


def _merge(common, local, path):
    if set(common) & set(local):
        raise WorkflowError("workflow_slot_conflict", fields=(path,))
    return {**common, **local}


def _params(recipe, common, local, path):
    result, missing = [], []
    groups = [step.group for step in recipe.steps] if len(recipe.steps) > 1 else None
    if groups:
        _keys(common, groups, "slots")
        _keys(local, groups, path)
    for step in recipe.steps:
        left = common.get(step.group, {}) if groups else common
        right = local.get(step.group, {}) if groups else local
        prefix = f"{path}.{step.group}" if groups else path
        allowed = set(_model(step).model_fields) - _FROZEN_FIELDS
        _keys(left, allowed, "slots" + (f".{step.group}" if groups else ""))
        _keys(right, allowed, prefix)
        merged = _merge(left, right, prefix)
        try:
            normalized = _model(step).model_validate(merged).model_dump(mode="json")
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
            invalid = [row for row in errors if row["type"] != "missing"]
            if invalid:
                raise WorkflowError("workflow_invalid_params", fields=tuple(
                    prefix + "." + ".".join(str(part) for part in row["loc"])
                    for row in invalid)) from exc
            missing.extend(prefix + "." + ".".join(str(part) for part in row["loc"])
                           for row in errors)
            normalized = None
        if normalized is not None:
            for field in _FROZEN_FIELDS:
                normalized.pop(field, None)
        result.append(normalized)
    return result, missing


def compile_workflow(workflow_id: str, workflow_version: int, *, slots: dict, scope: dict) -> dict:
    """Compile unaccepted inputs. No target discovery, binding, admission or execution."""
    recipe = _recipe(workflow_id, workflow_version)
    _json({"slots": slots, "scope": scope})
    _keys(slots, get_workflow(workflow_id, workflow_version)["slots_schema"]["properties"], "slots")
    _keys(scope, ("mode", "targets"), "scope")
    missing = []
    mode = scope.get("mode")
    targets = []
    if mode is None:
        if scope:
            raise WorkflowError("workflow_invalid_scope")
        missing.append("scope.mode")
    elif mode == "global":
        if not recipe.global_read or "targets" in scope:
            raise WorkflowError("workflow_invalid_scope")
        targets = [{"instance_id": None, "slots": {}}]
    elif mode == "explicit":
        if "targets" not in scope or scope["targets"] == []:
            missing.append("scope.targets")
        elif type(scope["targets"]) is not list or len(scope["targets"]) > 100:
            raise WorkflowError("workflow_invalid_scope")
        else:
            seen = set()
            for index, target in enumerate(scope["targets"]):
                _keys(target, ("instance_id", "slots"), f"scope.targets.{index}")
                instance_id = target.get("instance_id")
                if type(instance_id) is not int or not 1 <= instance_id <= 2**63 - 1:
                    raise WorkflowError("workflow_invalid_scope")
                if instance_id in seen:
                    raise WorkflowError("workflow_duplicate_target")
                seen.add(instance_id)
                if type(target.get("slots", {})) is not dict:
                    raise WorkflowError("workflow_invalid_scope")
                targets.append({"instance_id": instance_id, "slots": target.get("slots", {})})
    else:
        raise WorkflowError("workflow_invalid_scope")

    inputs, effective = [], []
    # Validate common slots even with no targets, without inventing a placeholder target/policy.
    validations = targets or [{"instance_id": None, "slots": {}}]
    definition = get_workflow(workflow_id, workflow_version)
    for index, target in enumerate(validations):
        prefix = f"scope.targets.{index}.slots" if targets and mode == "explicit" else "slots"
        params, needed = _params(recipe, slots, target["slots"], prefix)
        missing.extend(needed)
        if needed or not targets:
            continue
        steps = []
        for skeleton, values in zip(definition["steps"], params):
            step = {key: value for key, value in skeleton.items() if key != "slot_group"}
            step.update(title=recipe.title, params=values)
            steps.append(step)
        first = recipe.steps[0]
        inputs.append({"capability_id": first.capability_id, "definition_version": first.version,
                       "instance_id": target["instance_id"], "steps": steps,
                       "valid_for_seconds": 3600, "request_approval": recipe.effect})
        effective.append({"instance_id": target["instance_id"], "step_params": params})
    binding = {"workflow_id": workflow_id, "workflow_version": workflow_version,
               "definition_hash": definition["definition_hash"], "recipe_snapshot": definition}
    value = {"schema": "capability.workflow_candidate/v1", "accepted": False,
             "workflow_binding": binding, "missing_fields": sorted(set(missing)),
             "scope": scope, "slots": slots, "plan_inputs": [] if missing else inputs,
             "effective_inputs": [] if missing else effective}
    # Hash effective business inputs, not explicit-vs-default spelling of the same slots.
    identity = {"workflow_id": workflow_id, "workflow_version": workflow_version,
                "definition_hash": definition["definition_hash"], "scope_mode": mode,
                "effective_inputs": value["effective_inputs"]}
    if missing:
        identity.update(slots=slots, scope=scope, missing_fields=value["missing_fields"])
    value["candidate_hash"] = _hash(identity)
    return json.loads(_json(value))


def _has(text, english, chinese=()):
    return bool(re.search(r"(?<![a-z0-9_])(?:" + english + r")(?![a-z0-9_])", text)) or any(
        word in text for word in chinese)


def match_workflow(text: str, *, matcher_version: int = 1) -> dict:
    """Conservative operation-first selection; never parses targets or invokes compile."""
    if type(matcher_version) is not int or matcher_version != 1:
        raise WorkflowError("workflow_matcher_not_found")
    if type(text) is not str or not text.strip() or len(text) > 4000:
        raise WorkflowError("workflow_invalid_text")
    if any(unicodedata.category(char).startswith("C") and char not in "\t\n\r" for char in text):
        raise WorkflowError("workflow_invalid_text")
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    if len(normalized) > 4000:
        raise WorkflowError("workflow_invalid_text")
    value = {"schema": "capability.workflow_match/v1", "matcher_version": matcher_version,
             "accepted": False, "normalized_text": normalized, "decision": "not_matched",
             "operation": None, "workflow": None, "missing_fields": [],
             "reason_code": "workflow_not_matched"}

    def finish(decision, reason, *, operation=None, recipe_id=None, missing=()):
        value.update(decision=decision, reason_code=reason, operation=operation,
                     missing_fields=list(missing))
        if recipe_id:
            definition = get_workflow(recipe_id, 1)
            value["workflow"] = {key: definition[key] for key in
                                 ("workflow_id", "workflow_version", "definition_hash")}
        return value

    if _has(normalized, "select|insert|update|delete|drop|alter|truncate|sql|script|shell", ("执行脚本",)):
        return finish("rejected", "workflow_unsupported")
    if _has(normalized, "not|never|dont|don['\u2019]t|can['\u2019]t|cannot|without|unless|if|maybe|except|or",
            ("不", "别", "勿", "无需", "禁止", "避免", "如果", "假如", "也许", "或者", "还是", "是否", "能否")):
        return finish("needs_input", "workflow_intent_needs_confirmation", missing=("intent",))
    if _has(normalized, "pause|resume|stop|cancel|abort|halt", ("停止", "取消", "暂停", "恢复", "终止")):
        return finish("rejected", "workflow_control_not_supported")
    if _has(normalized, "all|every", ("全部", "所有")):
        return finish("needs_input", "workflow_explicit_scope_required", missing=("scope",))
    engines = re.findall(r"(?<![a-z0-9_])(?:oracle|mysql|postgresql|sqlserver)(?![a-z0-9_])", normalized)
    if len(set(engines)) > 1:
        return finish("ambiguous", "workflow_multiple_engines", missing=("engine", "scope"))
    report = _has(normalized, "reports?|results?", ("报告", "结果"))
    generate = _has(normalized, "generate|regenerate|compare|create|export",
                    ("生成", "比较", "对比", "重做报告", "创建", "制作", "导出"))
    if report and generate:
        return finish("rejected", "workflow_unsupported")
    navigate = _has(normalized, "open|navigate", ("打开", "进入", "跳转"))
    prepare = _has(normalized, "prepare", ("准备", "制定"))
    rerun = _has(normalized, "rerun", ("重新运行", "重跑", "再运行"))
    execute = _has(normalized, "run|execute|collect|start", ("运行", "执行", "采集", "启动"))
    read = _has(normalized, "show|view|list|read", ("查看", "查询", "列出", "显示", "只看"))
    if (navigate and (execute or rerun or prepare)) or (report and (execute or rerun or prepare)):
        return finish("ambiguous", "workflow_multiple_operations", missing=("intent",))
    families = []
    if _has(normalized, "jobs?|tasks?", ("任务", "作业")):
        families.append("inspection.jobs.list")
    if _has(normalized, "templates?|policies", ("模板", "策略")):
        families.append("inspection.templates.list")
    if _has(normalized, "capabilities|capability|matrix", ("能力", "矩阵")):
        families.append("operations.capabilities.list")
    context = _has(normalized, "context", ("上下文", "巡检背景"))
    if context:
        families.append("inspection.context.read")
    inspection = _has(normalized, "inspection|inspect", ("巡检",))
    if len(families) > 1 or (families and (execute or rerun or report)):
        return finish("ambiguous", "workflow_multiple_intents", missing=("intent",))
    if report:
        return finish("matched", "workflow_existing_reference_required", operation="navigate" if navigate
                      else "view_existing", missing=() if navigate else ("result_ref",))
    if families:
        recipe_id = families[0]
        if not (read or navigate or prepare):
            return finish("needs_input", "workflow_intent_needs_confirmation",
                          recipe_id=recipe_id, missing=("intent", "scope"))
        return finish("matched", "workflow_selected", operation="navigate" if navigate else "prepare",
                      recipe_id=recipe_id, missing=() if navigate else ("scope",))
    if inspection and navigate:
        return finish("matched", "workflow_navigation_only", operation="navigate")
    if inspection and (execute or rerun or prepare):
        if read:
            return finish("ambiguous", "workflow_multiple_operations", missing=("intent",))
        return finish("matched", "workflow_selected", operation="rerun" if rerun else "prepare",
                      recipe_id="inspection.collect", missing=("scope.targets", "policy_id") +
                      (("source_plan_id",) if rerun else ()))
    if inspection or _has(normalized, "check", ("检查",)):
        return finish("needs_input", "workflow_intent_needs_confirmation", missing=("intent", "scope"))
    return value
