"""Pure bounded-plan validation; schemas are code-owned, not request declarations.

Capability-specific parameter constraints, authorization and execution belong to the
caller. References select exactly one declared output field, never a path or code.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re


MAX_STEPS = 20
MAX_JSON_BYTES = 16 * 1024
STEP_FIELDS = frozenset({
    "seq", "title", "capability_id", "definition_version", "params",
    "depends_on", "input_refs", "fanout", "retries",
})
REF_FIELDS = frozenset({"seq", "schema", "field", "type"})
VALUE_TYPES = {"integer": int, "string": str, "boolean": bool}
FIELD_PATTERN = re.compile(r"(?!__)[A-Za-z_][A-Za-z0-9_]*\Z")
EXPRESSION_KEYS = frozenset({"expression", "expr", "$ref", "$expr", "$eval"})


class PlanDAGError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _validate_payload(value) -> None:
    # Import only the shared data validator, never resolve a handler or registry row.
    from app.services.capability_registry import CapabilityError, validate_params

    try:
        validate_params(value)
    except CapabilityError as exc:
        raise PlanDAGError(exc.code) from exc

    def reject_expressions(item):
        if isinstance(item, dict):
            if any(key.lower() in EXPRESSION_KEYS for key in item):
                raise PlanDAGError("plan_expression_rejected")
            for child in item.values():
                reject_expressions(child)
        elif isinstance(item, list):
            for child in item:
                reject_expressions(child)
        elif isinstance(item, str) and (
            "${" in item or "{{" in item or item.startswith(("$.", "$["))
        ):
            raise PlanDAGError("plan_expression_rejected")

    reject_expressions(value)


def _check_size(value) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PlanDAGError("plan_expression_rejected") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise PlanDAGError("plan_raw_payload_rejected")


def _budget_limit(budget: dict, key: str, default: int, minimum: int, code: str) -> int:
    value = budget.get(key, default)
    if type(value) is not int or value < minimum:
        raise PlanDAGError(code)
    return value


def _normalize_step(step) -> dict:
    if not isinstance(step, dict):
        raise PlanDAGError("plan_expression_rejected")
    if type(step.get("seq")) is not int or step["seq"] < 1:
        raise PlanDAGError("plan_typed_reference_violation")
    if not isinstance(step.get("capability_id"), str) or not step["capability_id"]:
        raise PlanDAGError("plan_typed_reference_violation")
    if type(step.get("definition_version")) is not int or step["definition_version"] < 1:
        raise PlanDAGError("stale_definition_version")
    title = step.get("title", step["capability_id"])
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise PlanDAGError("plan_expression_rejected")
    if not isinstance(step.get("params", {}), dict):
        raise PlanDAGError("plan_expression_rejected")
    if not isinstance(step.get("input_refs", {}), dict):
        raise PlanDAGError("plan_typed_reference_violation")
    depends_on = step.get("depends_on", [])
    if not isinstance(depends_on, list) or any(type(seq) is not int for seq in depends_on):
        raise PlanDAGError("plan_expression_rejected")
    if len(depends_on) != len(set(depends_on)):
        raise PlanDAGError("plan_typed_reference_violation")
    _validate_payload(step)
    if set(step) - STEP_FIELDS:
        raise PlanDAGError("plan_expression_rejected")
    normalized = deepcopy(step)
    for key, default in (
        ("title", step["capability_id"]), ("params", {}), ("depends_on", []),
        ("input_refs", {}), ("fanout", 1), ("retries", 0),
    ):
        normalized.setdefault(key, default)
    return normalized


def _schema(step: dict, schemas: dict[int, dict]) -> dict:
    schema = schemas.get(step["seq"])
    if not isinstance(schema, dict) or schema.get("capability_id") != step["capability_id"]:
        raise PlanDAGError("plan_typed_reference_violation")
    return schema


def _check_dependencies(step: dict, sequences) -> None:
    for source in step["depends_on"]:
        if source not in sequences:
            raise PlanDAGError("plan_typed_reference_violation")
        if source >= step["seq"]:
            raise PlanDAGError("plan_cycle")


def _field_name(value) -> bool:
    return isinstance(value, str) and FIELD_PATTERN.fullmatch(value) is not None


def _references(step: dict, schemas: dict[int, dict]):
    target = _schema(step, schemas)
    for param, ref in step["input_refs"].items():
        if (not _field_name(param) or param in step["params"]
                or not isinstance(ref, dict) or set(ref) != REF_FIELDS):
            raise PlanDAGError("plan_typed_reference_violation")
        source_seq = ref["seq"]
        if (type(source_seq) is not int or source_seq < 1 or source_seq >= step["seq"]
                or source_seq not in step["depends_on"]):
            raise PlanDAGError("plan_typed_reference_violation")
        source = schemas.get(source_seq)
        value_type = ref["type"]
        if (not isinstance(source, dict) or not _field_name(ref["field"])
                or not isinstance(ref["schema"], str) or not ref["schema"]
                or ref["schema"] != source.get("output_schema")
                or not isinstance(value_type, str) or value_type not in VALUE_TYPES
                or value_type != target.get("input_types", {}).get(param)
                or value_type != source.get("output_fields", {}).get(ref["field"])):
            raise PlanDAGError("plan_typed_reference_violation")
        yield param, ref


def validate_steps(steps, *, budget: dict, schemas: dict[int, dict]) -> list[dict]:
    """Return a detached plan with defaults, preserving the required 1..N order.

    The 16 KiB cap covers the entire normalized steps JSON, not each parameter.
    Budgets default to 20 steps, fanout 1 and retries 0; steps never exceed 20.
    Version freshness is the caller's check, after its idempotency lookup.
    """
    if not isinstance(budget, dict) or not isinstance(schemas, dict):
        raise PlanDAGError("plan_expression_rejected")
    max_steps = _budget_limit(budget, "max_steps", MAX_STEPS, 1, "plan_too_many_steps")
    if max_steps > MAX_STEPS:
        raise PlanDAGError("plan_too_many_steps")
    max_fanout = _budget_limit(budget, "max_fanout", 1, 1, "plan_fanout_exceeded")
    max_retries = _budget_limit(budget, "max_retries", 0, 0, "plan_retry_exceeded")
    normalized = normalize_steps(steps)
    if len(normalized) > max_steps:
        raise PlanDAGError("plan_too_many_steps")
    sequences = {step["seq"] for step in normalized}
    for step in normalized:
        _schema(step, schemas)
        _check_dependencies(step, sequences)
        for key, limit, minimum, code in (
            ("fanout", max_fanout, 1, "plan_fanout_exceeded"),
            ("retries", max_retries, 0, "plan_retry_exceeded"),
        ):
            if type(step[key]) is not int or not minimum <= step[key] <= limit:
                raise PlanDAGError(code)
        for _ in _references(step, schemas):
            pass
    _check_size(normalized)
    return normalized


def normalize_steps(steps) -> list[dict]:
    """Stable request identity before consulting today's budget or output schema."""
    if not isinstance(steps, list) or not steps:
        raise PlanDAGError("plan_expression_rejected")
    if len(steps) > MAX_STEPS:
        raise PlanDAGError("plan_too_many_steps")
    normalized = [_normalize_step(step) for step in steps]
    if [step["seq"] for step in normalized] != list(range(1, len(normalized) + 1)):
        raise PlanDAGError("plan_typed_reference_violation")
    _check_size(normalized)
    return normalized


def resolve_inputs(step, outputs: dict[int, dict], schemas: dict[int, dict]) -> dict:
    """Copy literal params and strictly typed, single-field dependency outputs.

    The caller supplies validated steps and outputs from the same plan. Optional
    runtime schema tags, if present, must also match the code-owned declaration.
    """
    if not isinstance(outputs, dict) or not isinstance(schemas, dict):
        raise PlanDAGError("plan_typed_reference_violation")
    normalized = _normalize_step(step)
    _check_dependencies(normalized, schemas)
    values = normalized["params"]
    for param, ref in _references(normalized, schemas):
        output = outputs.get(ref["seq"])
        if (not isinstance(output, dict) or ref["field"] not in output
                or ("schema" in output and output["schema"] != ref["schema"])
                or type(output[ref["field"]]) is not VALUE_TYPES[ref["type"]]):
            raise PlanDAGError("plan_typed_reference_violation")
        values[param] = deepcopy(output[ref["field"]])
    _validate_payload(values)
    _check_size(values)
    return values
