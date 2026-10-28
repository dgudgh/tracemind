"""Data-only DAG checks: no database sessions, handlers, models or Agents."""
from copy import deepcopy
import json

import pytest

from app.services import capability_registry
from app.services.plan_dag import PlanDAGError, resolve_inputs, validate_steps


def make_step(seq=1, **values):
    return {"seq": seq, "capability_id": "inspection.run.query", "definition_version": 1,
            **values}


def make_schemas(count=2):
    return {seq: {
        "capability_id": "inspection.run.query", "definition_version": 1, "kind": "query",
        "input_types": {"page_size": "integer", "status": "string", "enabled": "boolean"},
        "output_schema": "inspection.run_list/v1",
        "output_fields": {"total": "integer", "status": "string", "enabled": "boolean"},
    } for seq in range(1, count + 1)}


def reference(**values):
    return {"seq": 1, "schema": "inspection.run_list/v1", "field": "total",
            "type": "integer", **values}


def linked_steps():
    return [make_step(), make_step(2, depends_on=[1], input_refs={"page_size": reference()})]


def validate(steps, *, budget=None, schemas=None):
    return validate_steps(steps, budget={} if budget is None else budget,
                          schemas=make_schemas(len(steps)) if schemas is None else schemas)


def assert_code(code, callable_, *args, **kwargs):
    with pytest.raises(PlanDAGError) as caught:
        callable_(*args, **kwargs)
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code


def test_normalization_preserves_order_fills_defaults_and_detaches_all_mutable_data():
    steps = [make_step(), make_step(2, depends_on=[1], params={"filters": [{"status": "ok"}]})]
    original = deepcopy(steps)
    schemas = make_schemas()
    original_schemas = deepcopy(schemas)
    result = validate(steps, schemas=schemas)
    assert [step["seq"] for step in result] == [1, 2]
    assert result[0] == make_step(title="inspection.run.query", params={}, depends_on=[],
                                  input_refs={}, fanout=1, retries=0)
    result[1]["params"]["filters"][0]["status"] = "changed"
    result[1]["depends_on"].append(9)
    result[0]["params"]["x"] = 1
    assert result[1]["params"] is not steps[1]["params"]
    assert result[1]["params"] is not result[0]["params"]
    assert steps == original
    assert schemas == original_schemas


@pytest.mark.parametrize("title", ["T", "T" * 200, "\u6d4b" * 200])
def test_title_accepts_nonempty_strings_up_to_200_characters(title):
    assert validate([make_step(title=title)])[0]["title"] == title


@pytest.mark.parametrize("title", ["", " ", "\t\n", "T" * 201, "\u6d4b" * 201, None])
def test_title_rejects_blank_or_over_200_characters(title):
    assert_code("plan_expression_rejected", validate, [make_step(title=title)])


def test_default_title_uses_capability_id_and_has_the_same_length_limit():
    assert validate([make_step()])[0]["title"] == "inspection.run.query"
    capability_id = "x" * 201
    schemas = make_schemas(1)
    schemas[1]["capability_id"] = capability_id
    assert_code("plan_expression_rejected", validate,
                [make_step(capability_id=capability_id)], schemas=schemas)


@pytest.mark.parametrize("steps", [None, {}, (), [], [None]])
def test_rejects_non_step_lists(steps):
    assert_code("plan_expression_rejected", validate_steps, steps, budget={}, schemas={})


@pytest.mark.parametrize("seq", [True, False, 0, -1, 1.0, "1", None])
def test_seq_is_a_positive_strict_integer(seq):
    assert_code("plan_typed_reference_violation", validate, [make_step(seq)])


@pytest.mark.parametrize("seqs", [[1, 1], [1, 3], [2], [2, 3], [2, 1], [1, 3, 2]])
def test_seq_must_be_unique_contiguous_and_in_input_order(seqs):
    assert_code("plan_typed_reference_violation", validate, [make_step(seq) for seq in seqs])


@pytest.mark.parametrize("depends", [[1], [2]])
def test_self_or_existing_forward_dependencies_are_cycles(depends):
    assert_code("plan_cycle", validate,
                [make_step(depends_on=depends), make_step(2)])


@pytest.mark.parametrize("depends", [[0], [-1], [99], [1, 1]])
def test_unknown_or_duplicate_dependencies(depends):
    assert_code("plan_typed_reference_violation", validate,
                [make_step(), make_step(2, depends_on=depends)])


@pytest.mark.parametrize("depends", [[True], [1.0], ["1"], "1", {}, (1,)])
def test_dependency_list_does_not_coerce_integers_or_expressions(depends):
    assert_code("plan_expression_rejected", validate,
                [make_step(), make_step(2, depends_on=depends)])


def test_twenty_steps_allowed_twenty_one_rejected():
    assert len(validate([make_step(seq) for seq in range(1, 21)])) == 20
    assert_code("plan_too_many_steps", validate, [make_step(seq) for seq in range(1, 22)])


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "20", 21, None])
def test_step_budget_is_strict_and_cannot_raise_hard_cap(value):
    assert_code("plan_too_many_steps", validate, [make_step()], budget={"max_steps": value})


def test_lower_step_budget_is_enforced():
    assert_code("plan_too_many_steps", validate, [make_step(), make_step(2)],
                budget={"max_steps": 1})


@pytest.mark.parametrize("key,value,code", [
    ("fanout", 0, "plan_fanout_exceeded"), ("fanout", 2, "plan_fanout_exceeded"),
    ("fanout", True, "plan_fanout_exceeded"), ("fanout", 1.0, "plan_fanout_exceeded"),
    ("retries", 1, "plan_retry_exceeded"), ("retries", -1, "plan_retry_exceeded"),
    ("retries", False, "plan_retry_exceeded"), ("retries", "0", "plan_retry_exceeded"),
])
def test_default_fanout_retry_bounds(key, value, code):
    assert_code(code, validate, [make_step(**{key: value})])


@pytest.mark.parametrize("budget,code", [
    ({"max_fanout": 0}, "plan_fanout_exceeded"),
    ({"max_fanout": True}, "plan_fanout_exceeded"),
    ({"max_retries": -1}, "plan_retry_exceeded"),
    ({"max_retries": False}, "plan_retry_exceeded"),
])
def test_invalid_budget_values(budget, code):
    assert_code(code, validate, [make_step()], budget=budget)


def test_code_owned_budget_values_are_used():
    assert validate([make_step(fanout=2, retries=1)],
                    budget={"max_fanout": 2, "max_retries": 1})[0]["fanout"] == 2


@pytest.mark.parametrize("key,code", [
    ("sql", "plan_raw_payload_rejected"), ("SQL", "plan_raw_payload_rejected"),
    ("script", "plan_raw_payload_rejected"), ("command", "plan_raw_payload_rejected"),
    ("shell", "plan_raw_payload_rejected"), ("cmd", "plan_raw_payload_rejected"),
    ("url", "plan_raw_payload_rejected"), ("endpoint", "plan_raw_payload_rejected"),
    ("handler", "request_supplied_handler_rejected"),
    ("module", "request_supplied_handler_rejected"),
    ("path", "request_supplied_handler_rejected"),
    ("function", "request_supplied_handler_rejected"),
    ("handler_ref", "request_supplied_handler_rejected"),
])
@pytest.mark.parametrize("nested", [False, True])
def test_raw_payload_and_handler_fields_are_recursively_rejected(key, code, nested):
    payload = {key: "untrusted"}
    if nested:
        payload = {"filters": [{"values": payload}]}
    assert_code(code, validate, [make_step(params=payload)])


def test_shared_parameter_validator_is_reused(monkeypatch):
    def reject(value):
        raise capability_registry.CapabilityError("shared_validator_sentinel")

    monkeypatch.setattr(capability_registry, "validate_params", reject)
    assert_code("shared_validator_sentinel", validate, [make_step()])


@pytest.mark.parametrize("values,code", [
    ({"unknown": 1}, "plan_expression_rejected"),
    ({"while": True}, "plan_expression_rejected"),
    ({"handler": "fake"}, "request_supplied_handler_rejected"),
    ({"params": {"expression": "1+1"}}, "plan_expression_rejected"),
    ({"params": {"status": "${steps[1].total}"}}, "plan_expression_rejected"),
    ({"params": {"status": "{{ output.total }}"}}, "plan_expression_rejected"),
    ({"params": {"status": "$.output.total"}}, "plan_expression_rejected"),
    ({"params": []}, "plan_expression_rejected"),
    ({"title": 1}, "plan_expression_rejected"),
])
def test_unknown_fields_and_expression_forms(values, code):
    assert_code(code, validate, [make_step(**values)])


def test_json_limit_is_total_not_per_step_and_counts_normalized_body():
    steps = [make_step(params={"status": "a" * 8100}),
             make_step(2, params={"status": "b" * 8100})]
    assert_code("plan_raw_payload_rejected", validate, steps)
    normalized = validate([make_step(params={"status": ""})])
    overhead = len(json.dumps(normalized, ensure_ascii=True, sort_keys=True,
                              separators=(",", ":")).encode("ascii"))
    steps = [make_step(params={"status": "a" * (16384 - overhead)})]
    assert validate(steps)
    steps[0]["params"]["status"] += "a"
    assert_code("plan_raw_payload_rejected", validate, steps)


def test_reference_total_to_page_size_and_resolve_without_mutation():
    schemas = make_schemas()
    steps = validate(linked_steps(), schemas=schemas)
    steps[1]["params"]["status"] = "ok"
    outputs = {1: {"schema": "inspection.run_list/v1", "total": 12, "ignored": {"x": 1}}}
    before = deepcopy((steps, outputs, schemas))
    values = resolve_inputs(steps[1], outputs, schemas)
    assert values == {"status": "ok", "page_size": 12}
    values["status"] = "changed"
    assert (steps, outputs, schemas) == before


@pytest.mark.parametrize("change", [
    {"schema": "other/v1"}, {"type": "string"}, {"type": "number"},
    {"field": "missing"}, {"field": "items.total"}, {"field": "items[0]"},
    {"field": "__class__"}, {"seq": True}, {"seq": "1"}, {"seq": 2},
    {"seq": 0}, {"extra": "value"}, {"type": []},
])
def test_reference_contract_rejections(change):
    steps = linked_steps()
    steps[1]["input_refs"]["page_size"] = reference(**change)
    assert_code("plan_typed_reference_violation", validate, steps)


@pytest.mark.parametrize("target", ["missing", "page_size.value", "page_size[0]"])
def test_reference_cannot_target_unknown_field_or_path(target):
    steps = linked_steps()
    steps[1]["input_refs"] = {target: reference()}
    assert_code("plan_typed_reference_violation", validate, steps)


def test_reference_must_be_an_explicit_dependency():
    steps = linked_steps()
    steps[1]["depends_on"] = []
    assert_code("plan_typed_reference_violation", validate, steps)


def test_literal_param_and_reference_cannot_share_a_target():
    steps = linked_steps()
    steps[1]["params"] = {"page_size": 10}
    assert_code("plan_typed_reference_violation", validate, steps)


@pytest.mark.parametrize("refs", [[], {"page_size": "1.total"}, {"page_size": {"seq": 1}}])
def test_reference_must_have_exact_structured_shape(refs):
    steps = linked_steps()
    steps[1]["input_refs"] = refs
    assert_code("plan_typed_reference_violation", validate, steps)


def test_source_declared_output_type_must_match_target_type():
    schemas = make_schemas()
    schemas[1]["output_fields"]["total"] = "string"
    assert_code("plan_typed_reference_violation", validate, linked_steps(), schemas=schemas)


@pytest.mark.parametrize("schemas,code", [
    ({}, "plan_typed_reference_violation"),
    ({1: {"capability_id": "other.run.query", "definition_version": 1}},
     "plan_typed_reference_violation"),
])
def test_code_owned_step_identity_is_bound(schemas, code):
    assert_code(code, validate, [make_step()], schemas=schemas)


@pytest.mark.parametrize("version", [True, False, 0, -1, 1.0, "1", None])
def test_step_version_must_be_a_strict_positive_integer(version):
    assert_code("stale_definition_version", validate,
                [make_step(definition_version=version)])


def test_version_freshness_is_deferred_until_after_caller_idempotency_lookup():
    schemas = make_schemas()
    schemas[1]["definition_version"] = 2
    schemas[2]["definition_version"] = 3
    steps = linked_steps()
    result = validate(steps, schemas=schemas)
    assert [step["definition_version"] for step in result] == [1, 1]
    assert resolve_inputs(result[1], {1: {"total": 12}}, schemas) == {"page_size": 12}
    assert steps == linked_steps()


@pytest.mark.parametrize("outputs", [
    {}, {1: {}}, {1: []}, {1: {"total": True}}, {1: {"total": "12"}},
    {1: {"total": 12.0}}, {1: {"total": None}},
    {1: {"total": 12, "schema": "other/v1"}},
])
def test_runtime_output_requires_result_field_schema_and_strict_type(outputs):
    assert_code("plan_typed_reference_violation", resolve_inputs,
                validate(linked_steps())[1], outputs, make_schemas())


@pytest.mark.parametrize("field,type_,value,bad", [
    ("status", "string", "ok", 1), ("enabled", "boolean", True, 1),
    ("page_size", "integer", 12, False),
])
def test_all_supported_reference_types_are_strict(field, type_, value, bad):
    source_field = "total" if field == "page_size" else field
    steps = [make_step(), make_step(2, depends_on=[1], input_refs={
        field: reference(field=source_field, type=type_),
    })]
    step = validate(steps)[1]
    assert resolve_inputs(step, {1: {source_field: value}}, make_schemas()) == {field: value}
    assert_code("plan_typed_reference_violation", resolve_inputs,
                step, {1: {source_field: bad}}, make_schemas())


def test_resolver_rechecks_reference_contract():
    step = validate(linked_steps())[1]
    step["input_refs"]["page_size"]["field"] = "items.total"
    assert_code("plan_typed_reference_violation", resolve_inputs,
                step, {1: {"items.total": 1}}, make_schemas())


def test_resolver_copies_nested_literal_params_and_limits_resolved_size():
    step = validate([make_step(params={"filters": [{"status": "ok"}]})])[0]
    values = resolve_inputs(step, {}, make_schemas())
    values["filters"][0]["status"] = "changed"
    assert step["params"]["filters"][0]["status"] == "ok"
    steps = [make_step(), make_step(2, depends_on=[1], input_refs={
        "status": reference(field="status", type="string"),
    })]
    assert_code("plan_raw_payload_rejected", resolve_inputs,
                validate(steps)[1], {1: {"status": "x" * 16384}}, make_schemas())
