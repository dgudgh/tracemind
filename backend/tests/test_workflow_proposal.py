"""Pure C11 boundary tests; catalog and choices are trusted synthetic snapshots."""
import ast
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services import workflow_library as library, workflow_proposal as proposals
from app.services.inspection import jobs
from app.services.run_status import OUTCOMES


GLOBAL = {"mode": "global"}
ONE = {"mode": "explicit", "targets": [{"instance_id": 10}]}
TWO = {"mode": "explicit", "targets": [{"instance_id": 20}, {"instance_id": 10}]}


def model(name="inspection.jobs.list", *, slots=None, decision="matched", version=1):
    return json.dumps({"decision": decision, "workflow_id": name, "workflow_version": version,
                       "slots": {} if slots is None else slots})


def validate(raw=None, **kwargs):
    inputs = {"catalog": library.list_workflows(), "scope": GLOBAL,
              "allowed_target_ids": [10, 20], "allowed_policy_ids": [7, 8]}
    inputs.update(kwargs)
    return proposals.validate_proposal(model() if raw is None else raw, **inputs)


def rejects(code, callback):
    with pytest.raises(library.WorkflowError) as caught:
        callback()
    assert caught.value.code == code
    return caught.value


@pytest.mark.parametrize("name", [
    "inspection.jobs.list", "inspection.templates.list", "operations.capabilities.list",
    "inspection.context.read", "inspection.collect",
])
def test_five_recipes_reuse_original_compiler_and_capability_versions(name):
    extra = {"confirmed_policy_id": 7} if name == "inspection.collect" else {}
    value = validate(model(name), scope=ONE, **extra)
    assert value["schema"] == "capability.workflow_proposal/v1"
    assert value["decision"] == value["model_decision"] == "matched"
    assert value["accepted"] is False and value["requires_confirmation"] is True
    assert value["validation_scope"] == "structure_only"
    expected = library.compile_workflow(name, 1, slots={"policy_id": 7} if extra else {}, scope=ONE)
    assert value["candidate"] == expected and value["candidate"]["accepted"] is False
    assert value["workflow"]["definition_hash"] == expected["workflow_binding"]["definition_hash"]
    if name == "inspection.collect":
        assert value["candidate"]["plan_inputs"][0]["definition_version"] == 2


def test_explicit_target_order_and_per_target_confirmed_template_choices_are_preserved():
    scope = deepcopy(TWO)
    scope["targets"][0]["slots"] = {"policy_id": 8}
    scope["targets"][1]["slots"] = {"policy_id": 7}
    value = validate(model("inspection.collect", slots={"timeout_seconds": 901}), scope=scope)
    plans = value["candidate"]["plan_inputs"]
    assert [item["instance_id"] for item in plans] == [20, 10]
    assert [item["steps"][0]["params"]["policy_id"] for item in plans] == [8, 7]
    assert all(item["steps"][0]["params"]["timeout_seconds"] == 901 for item in plans)


def test_missing_confirmation_does_not_guess_single_allowed_template_or_target():
    value = validate(model("inspection.collect"), scope=ONE, allowed_policy_ids=[7])
    assert value["decision"] == "needs_input" and value["candidate"] is None
    assert value["missing_fields"] == ["scope.targets.0.slots.policy_id"]
    for scope in ({}, {"mode": "explicit"}, {"mode": "explicit", "targets": []}):
        value = validate(model(), scope=scope, allowed_target_ids=[10])
        assert value["decision"] == "needs_input" and value["candidate"] is None
        assert any(field.startswith("scope.") for field in value["missing_fields"])


@pytest.mark.parametrize("raw,decision", [
    ('{"decision":"needs_input"}', "needs_input"),
    ('{"decision":"not_matched"}', "not_matched"),
    ('{"decision":"not_matched","workflow_id":null,"workflow_version":null,"slots":{}}', "not_matched"),
    (model(decision="needs_input"), "needs_input"),
])
def test_negative_decisions_never_have_actionable_candidates_even_when_params_are_complete(raw, decision):
    value = validate(raw)
    assert value["decision"] == decision and value["candidate"] is None
    assert value["accepted"] is False
    if value["workflow"]:
        assert "intent_confirmation" in value["missing_fields"]


@pytest.mark.parametrize("field", [
    "scope", "targets", "instance_id", "policy_id", "actor", "role", "approved", "approval_state",
    "sql", "handler", "url", "source_plan_id", "sourceplan", "operation", "steps", "reason",
    "confidence", "definition_hash", "metadata", "provider", "accepted", "schema",
])
def test_model_root_cannot_set_context_approval_code_or_provenance(field):
    raw = json.loads(model())
    raw[field] = "untrusted payload"
    rejects("workflow_proposal_forbidden_field", lambda: validate(json.dumps(raw)))


@pytest.mark.parametrize("slots", [
    {"policy_id": 7}, {"instance_id": 10}, {"targets": [10]}, {"policy_revision": "0" * 64},
    {"runtime_digest": "0" * 64}, {"target_fingerprint": "0" * 64},
    {"sql": "select 1"}, {"handler": "inspection.run_frozen"}, {"url": "https://example.invalid"},
    {"request_approval": False}, {"trigger_type": "manual"},
])
def test_model_slots_cannot_choose_even_an_allowed_and_already_confirmed_policy_or_free_text(slots):
    rejects("workflow_proposal_forbidden_field", lambda: validate(model("inspection.collect", slots=slots),
            scope=ONE, confirmed_policy_id=7))


@pytest.mark.parametrize("slots", [
    {"jobs": {"trigger_type": "manual"}}, {"jobs": {"sql": "select 1"}},
    {"matrix": {"policy_id": 7}}, {"templates": {"handler": "anything"}},
])
def test_nested_model_slot_groups_have_no_injection_side_channel(slots):
    rejects("workflow_proposal_forbidden_field", lambda: validate(model("inspection.context.read", slots=slots), scope=ONE))


def test_domain_filter_vocabulary_is_original_not_plan_layer_aliases():
    for status in jobs.ALL_STATUSES:
        assert validate(model(slots={"status": status}))["decision"] == "matched"
    for outcome in OUTCOMES:
        assert validate(model(slots={"outcome": outcome}))["decision"] == "matched"
    for slots in ({"outcome": "succeeded"}, {"status": "completed"}, {"outcome": "select 1"},
                  {"status": "https://x.invalid"}, {"page": True}, {"page": "1"}, {"page_size": 201}):
        rejects("workflow_invalid_params", lambda: validate(model(slots=slots)))
    assert validate(model(slots={"status": "success, failed"}))["decision"] == "matched"


@pytest.mark.parametrize("name,slots", [
    ("inspection.templates.list", {"db_type": "https://x"}),
    ("inspection.templates.list", {"enabled": 1}),
    ("operations.capabilities.list", {"engine": "SELECT"}),
    ("inspection.collect", {"timeout_seconds": 29}),
])
def test_other_models_keep_original_strict_patterns_and_bounds(name, slots):
    rejects("workflow_invalid_params", lambda: validate(model(name, slots=slots), scope=ONE,
            **({"confirmed_policy_id": 7} if name == "inspection.collect" else {})))


def test_authorized_snapshot_is_a_whitelist_not_an_invitation_to_choose_any_static_recipe():
    catalog = library.list_workflows()
    catalog["items"] = [item for item in catalog["items"] if item["workflow_id"] == "inspection.jobs.list"]
    assert validate(catalog=catalog)["decision"] == "matched"
    rejects("workflow_proposal_not_allowed", lambda: validate(model("inspection.collect"), catalog=catalog, scope=ONE))
    rejects("workflow_proposal_not_allowed", lambda: validate(catalog={"schema": "capability.workflow_catalog/v1", "items": []}))
    rejects("workflow_proposal_not_allowed", lambda: validate(model("inspection.report.generate")))
    rejects("workflow_proposal_not_allowed", lambda: validate(model(version=2)))
    empty = {"schema": "capability.workflow_catalog/v1", "items": []}
    assert validate('{"decision":"not_matched"}', catalog=empty)["candidate"] is None


@pytest.mark.parametrize("change,code", [
    ("hash", "workflow_proposal_catalog_changed"), ("step", "workflow_proposal_catalog_changed"),
    ("duplicate", "workflow_proposal_catalog_changed"), ("extra", "workflow_proposal_invalid_catalog"),
    ("schema", "workflow_proposal_invalid_catalog"), ("version", "workflow_proposal_invalid_catalog"),
])
def test_catalog_integrity_is_checked_without_live_authorization(change, code):
    catalog = library.list_workflows()
    if change == "hash":
        catalog["items"][0]["definition_hash"] = "0" * 64
    elif change == "step":
        catalog["items"][0]["steps"][0]["capability_id"] = "inspection.run.execute"
    elif change == "duplicate":
        catalog["items"].append(deepcopy(catalog["items"][0]))
    elif change == "extra":
        catalog["actor"] = {"role": "admin"}
    elif change == "schema":
        catalog["schema"] = "capability.registry/v1"
    else:
        catalog["items"][0]["workflow_version"] = 2
    rejects(code, lambda: validate(catalog=catalog))


@pytest.mark.parametrize("field,values", [
    ("allowed_target_ids", [True]), ("allowed_target_ids", ["10"]), ("allowed_target_ids", [10, 10]),
    ("allowed_target_ids", [0]), ("allowed_target_ids", [2**63]), ("allowed_target_ids", (10,)),
    ("allowed_policy_ids", [False]), ("allowed_policy_ids", [7, 7]), ("allowed_policy_ids", "7"),
    ("allowed_target_ids", list(range(1, 1002))),
])
def test_allowed_choices_are_bounded_strict_id_sets(field, values):
    rejects("workflow_proposal_invalid_selection", lambda: validate(**{field: values}))


@pytest.mark.parametrize("scope,code", [
    ({"mode": "global", "targets": []}, "workflow_proposal_invalid_scope"),
    ({"mode": "all"}, "workflow_proposal_invalid_scope"),
    ({"mode": "global", "actor": 1}, "workflow_proposal_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": True}]}, "workflow_proposal_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": "10"}]}, "workflow_proposal_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 30}]}, "workflow_proposal_selection_not_allowed"),
    ({"mode": "explicit", "targets": [{"instance_id": 10}] * 2}, "workflow_proposal_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"sql": "select 1"}}]}, "workflow_proposal_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"policy_id": 9}}]}, "workflow_proposal_selection_not_allowed"),
    ({"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"policy_id": True}}]}, "workflow_proposal_selection_not_allowed"),
    ({"mode": "explicit", "targets": [{"instance_id": n} for n in range(1, 102)]}, "workflow_proposal_invalid_scope"),
])
def test_scope_can_only_use_confirmed_whitelisted_targets_and_templates(scope, code):
    rejects(code, lambda: validate(scope=scope))


def test_scope_whitelist_is_still_checked_when_model_abstains():
    rejects("workflow_proposal_selection_not_allowed", lambda: validate('{"decision":"not_matched"}',
            scope=ONE, allowed_target_ids=[]))
    rejects("workflow_proposal_selection_not_allowed", lambda: validate('{"decision":"needs_input"}', confirmed_policy_id=9))


def test_common_and_local_template_confirmation_cannot_override_each_other_or_read_intent():
    scope = {"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"policy_id": 7}}]}
    rejects("workflow_proposal_selection_conflict", lambda: validate(model("inspection.collect"),
            scope=scope, confirmed_policy_id=7))
    rejects("workflow_proposal_selection_conflict", lambda: validate(confirmed_policy_id=7))
    rejects("workflow_proposal_selection_conflict", lambda: validate(scope=scope))
    for value in (True, "7", 0, 9):
        rejects("workflow_proposal_selection_not_allowed", lambda: validate(model("inspection.collect"),
                scope=ONE, confirmed_policy_id=value))


@pytest.mark.parametrize("raw", [
    ' {"decision":"not_matched","decision":"matched"}',
    '{"decision":"needs_input","slots":{"page":1,"page":2}}',
    '{"decision":"needs_input","slots":{"jobs":{"page":1,"p\\u0061ge":2}}}',
])
def test_duplicate_keys_at_every_depth_are_rejected(raw):
    rejects("workflow_proposal_duplicate_key", lambda: validate(raw))


@pytest.mark.parametrize("raw", [
    None, b'{}', "", " ", "[]", "null", "true", "1", '{"decision":',
    '```json\n{"decision":"not_matched"}\n```', '{"decision":"not_matched"} trailing',
    '{"decision":"not_matched"} {"decision":"not_matched"}',
    '{"decision":"needs_input","slots":{"page":NaN}}',
    '{"decision":"needs_input","slots":{"page":Infinity}}',
    '{"decision":"needs_input","slots":{"page":-Infinity}}',
    '{"decision":"needs_input","slots":{"page":1e9999}}',
    '{"decision":"needs_input\\u0000"}', '{"decision":"needs_input\\u202e"}',
    '{"decision":"needs_input\\ud800"}', '{"decision":"needs_input"}\ud800',
])
def test_raw_is_strict_complete_json_not_a_repair_or_markup_parser(raw):
    # Call the public function directly so None is not the test helper's default sentinel.
    rejects("workflow_proposal_invalid_json", lambda: proposals.validate_proposal(raw,
        catalog=library.list_workflows(), scope=GLOBAL, allowed_target_ids=[], allowed_policy_ids=[]))


@pytest.mark.parametrize("data,code", [
    ({"decision": "success"}, "workflow_proposal_invalid_decision"),
    ({"decision": True}, "workflow_proposal_invalid_decision"),
    ({"decision": "matched"}, "workflow_proposal_invalid_reference"),
    ({"decision": "needs_input", "workflow_id": None}, "workflow_proposal_invalid_reference"),
    ({"decision": "needs_input", "workflow_version": 1}, "workflow_proposal_invalid_reference"),
    ({"decision": "needs_input", "slots": {"page": 1}}, "workflow_proposal_invalid_decision"),
    ({"decision": "not_matched", "workflow_id": "inspection.jobs.list", "workflow_version": 1}, "workflow_proposal_invalid_decision"),
    ({"decision": "needs_input", "slots": []}, "workflow_proposal_invalid_slots"),
])
def test_decision_reference_and_slot_combinations_are_explicit(data, code):
    rejects(code, lambda: validate(json.dumps(data)))


@pytest.mark.parametrize("version", [True, "1", 1.0, 0, -1, 2147483648])
def test_model_recipe_version_is_strict_integer_not_latest(version):
    rejects("workflow_proposal_invalid_reference", lambda: validate(model(version=version)))


@pytest.mark.parametrize("version", [True, "1", 0, 2])
def test_validator_versions_do_not_silently_upgrade(version):
    rejects("workflow_proposal_validator_not_found", lambda: validate(validator_version=version))


def test_input_budgets_are_checked_before_json_decoding_and_recursive_walk(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must reject before decoding oversized/deep text")
    with monkeypatch.context() as fixture:
        fixture.setattr(proposals.json, "loads", forbidden)
        for raw in (" " * 16385, "界" * 6000, "[" * 100 + "]" * 100):
            rejects("workflow_proposal_budget_exceeded", lambda: proposals._decode(raw))
    raw = json.dumps({"decision": "needs_input", "slots": {str(index): 1 for index in range(65)}})
    rejects("workflow_proposal_budget_exceeded", lambda: validate(raw))
    deep = {}
    for _ in range(20):
        deep = {"nested": deep}
    rejects("workflow_proposal_budget_exceeded", lambda: validate(catalog=deep))
    rejects("workflow_proposal_budget_exceeded", lambda: validate(catalog={"x": "a" * 4097}))
    rejects("workflow_proposal_budget_exceeded", lambda: validate(catalog={"x": [[1] * 1000 for _ in range(13)]}))
    rejects("workflow_proposal_budget_exceeded", lambda: validate('{"decision":"needs_input","slots":{"page":' + "9" * 100 + '}}'))


def test_prescan_ignores_brackets_and_escaped_quotes_inside_strings():
    raw = '{"decision":"needs_input","slots":{"x":"' + "[" * 30 + '\\"' + "]" * 30 + '"}}'
    rejects("workflow_proposal_invalid_decision", lambda: validate(raw))


def test_errors_do_not_echo_unknown_keys_credentials_or_raw_model_text():
    secret = "synthetic-secret-do-not-echo"
    raw = json.dumps({"decision": "matched", secret: "credential"})
    error = rejects("workflow_proposal_forbidden_field", lambda: validate(raw))
    assert secret not in str(error) and secret not in repr(error.fields)
    error = rejects("workflow_invalid_params", lambda: validate(model(slots={"status": secret})))
    assert error.__cause__ is None and error.__suppress_context__ is True


def test_existing_provider_style_text_wrapper_is_read_as_text_not_as_authority():
    class ProviderText(str):
        metadata = {"approved": True, "provider": "untrusted-hint"}
    plain = validate(model())
    assert validate(ProviderText(model())) == plain
    assert "metadata" not in plain and plain["accepted"] is False


def test_json_objects_and_finite_budget_can_never_select_control_or_external_actions():
    for name in ("execution.pause", "execution.resume", "execution.stop", "inspection.report.read", "https://x"):
        with pytest.raises(library.WorkflowError):
            validate(model(name))
    for value in ({"x": float("nan")}, {"x": {1: "bad-key"}}, {"x": object()}):
        rejects("workflow_proposal_invalid_json", lambda: validate(catalog=value))


def test_return_values_are_detached_and_canonical_inputs_have_stable_hashes():
    catalog, scope, target_ids, policy_ids = library.list_workflows(), deepcopy(TWO), [20, 10], [8, 7]
    value = validate(model("inspection.collect"), catalog=catalog, scope=scope,
                     allowed_target_ids=target_ids, allowed_policy_ids=policy_ids, confirmed_policy_id=7)
    saved = deepcopy(value)
    catalog["items"].clear()
    scope["targets"].reverse()
    target_ids.clear()
    policy_ids.clear()
    assert value == saved
    again = validate(model("inspection.collect"), scope=TWO, confirmed_policy_id=7)
    assert again == saved
    value["candidate"]["plan_inputs"][0]["steps"][0]["params"]["policy_id"] = 99
    assert validate(model("inspection.collect"), scope=TWO, confirmed_policy_id=7) == saved
    raw = json.dumps(json.loads(model("inspection.collect")), indent=4, sort_keys=True)
    assert validate(raw, scope=TWO, confirmed_policy_id=7) == saved
    reversed_catalog = library.list_workflows()
    reversed_catalog["items"].reverse()
    assert validate(raw, catalog=reversed_catalog, scope=TWO, confirmed_policy_id=7) == saved
    changed = deepcopy(TWO)
    changed["targets"].reverse()
    assert validate(raw, scope=changed, confirmed_policy_id=7)["proposal_hash"] != saved["proposal_hash"]


def test_a_full_hundred_confirmed_targets_remain_bounded_and_in_original_order():
    scope = {"mode": "explicit", "targets": [{"instance_id": identity} for identity in range(100, 0, -1)]}
    value = validate(model("inspection.context.read"), scope=scope, allowed_target_ids=list(range(1, 101)))
    assert value["decision"] == "matched"
    assert [item["instance_id"] for item in value["candidate"]["plan_inputs"]] == list(range(100, 0, -1))


def test_runtime_definition_drift_is_not_bypassed_by_a_previously_trusted_snapshot(monkeypatch):
    from app.services.capability_handler import JobListParams
    catalog = library.list_workflows()
    original = JobListParams.model_json_schema
    def changed(*args, **kwargs):
        schema = original(*args, **kwargs)
        schema["properties"]["page"]["default"] = 2
        return schema
    monkeypatch.setattr(JobListParams, "model_json_schema", changed)
    rejects("workflow_definition_changed", lambda: validate(catalog=catalog))


def test_module_has_no_provider_database_or_execution_imports_and_calls_stay_pure(monkeypatch):
    import socket
    import time
    import requests
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session
    from app.services import capability_handler, workflow_service, ai_config, plan_service

    parsed = ast.parse(inspect.getsource(proposals))
    imports = [node.module for node in ast.walk(parsed) if isinstance(node, ast.ImportFrom)]
    assert set(imports) <= {"__future__", "app.services"}
    names = {alias.name for node in ast.walk(parsed) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not names.intersection({"workflow_service", "ai_config", "plan_service", "capability_registry"})
    def forbidden(*args, **kwargs):
        raise AssertionError("pure proposal validator crossed an IO/control boundary")
    for target, methods in (
        (Engine, ("connect", "begin")), (Session, ("execute", "query", "get", "commit", "flush")),
        (socket.socket, ("connect", "connect_ex")), (time, ("time", "monotonic", "perf_counter")),
        (requests, ("get", "post", "request")), (requests.Session, ("request",)),
        (ai_config, ("call_ai_provider", "get_default_ai_config")),
        (workflow_service, ("list_workflows", "prepare_workflow", "revise_workflow")),
        (plan_service, ("create_plan", "execute_plan")),
    ):
        for name in methods:
            monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(capability_handler, "HANDLERS", {key: forbidden for key in capability_handler.HANDLERS})
    assert validate(model("inspection.collect"), scope=ONE, confirmed_policy_id=7)["accepted"] is False


def test_new_interpreters_have_no_io_and_identical_hashes_across_hash_seeds():
    root = Path(__file__).resolve().parents[2]
    script = """
import json, socket, sys
from sqlalchemy.engine import Engine
def denied(*args, **kwargs):
    raise AssertionError('no database or network allowed')
Engine.connect = denied
socket.socket.connect = denied
from app.services import workflow_library, workflow_proposal
raw = json.dumps({'decision': 'matched', 'workflow_id': 'inspection.collect', 'workflow_version': 1,
                  'slots': {'timeout_seconds': 900}})
value = workflow_proposal.validate_proposal(raw, catalog=workflow_library.list_workflows(),
    scope={'mode': 'explicit', 'targets': [{'instance_id': 10}]}, allowed_target_ids=[10],
    allowed_policy_ids=[7], confirmed_policy_id=7)
sys.stdout.write(json.dumps(value, sort_keys=True))
"""
    env = {**os.environ, "PYTHONPATH": str(root / "backend"), "DATABASE_URL": "sqlite://", "DB_TYPE": "sqlite",
           "APP_MODE": "quality_test", "DEBUG": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    values = []
    for seed in ("1", "98765"):
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
            env={**env, "PYTHONHASHSEED": seed}, capture_output=True, text=True, timeout=30, check=True)
        values.append(json.loads(result.stdout))
    assert values[0] == values[1]
