"""Pure C10 candidates: actual input models, fixed identities, no live services."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services import workflow_library as library
from app.services.capability_handler import JobListParams, PARAM_SCHEMAS
from app.services.inspection import jobs
from app.services import run_status


GLOBAL = {"mode": "global"}
TARGET = {"mode": "explicit", "targets": [{"instance_id": 10}]}
HASHES = {
    "inspection.jobs.list": "35b844072e1937d6226361b9d74b44da7e5ca0e8358db7e2d43fbefdf8ce5453",
    "inspection.templates.list": "aaf90e4f66069c1ca282b40f2d55b5c83a783a53c7a4852dd18126604bcef6ed",
    "operations.capabilities.list": "6293c4bd02bf18aa642f614ecf7099bc9d767addbc0e565aa73e01dfc7ca39b3",
    "inspection.context.read": "eee95f21c7ffa0f1749536897e4141cc408f18aeb170baaea27aba1d3e060f57",
    "inspection.collect": "c6162a7a6c67e1c181305220ca978f423e1913ecb0698f875bcec5586d238c30",
}


def compile_recipe(name="inspection.jobs.list", *, slots=None, scope=None):
    return library.compile_workflow(name, 1, slots={} if slots is None else slots,
                                    scope=GLOBAL if scope is None else scope)


def fails(code, callback):
    with pytest.raises(library.WorkflowError) as caught:
        callback()
    assert caught.value.code == code
    return caught.value


def test_five_exact_recipes_pin_current_registry_capability_versions_and_definition_hashes():
    from app.services import capability_registry as registry

    catalog = library.list_workflows()
    assert catalog["schema"] == "capability.workflow_catalog/v1"
    assert {row["workflow_id"] for row in catalog["items"]} == set(HASHES)
    for row in catalog["items"]:
        assert row["workflow_version"] == 1
        assert row["definition_hash"] == HASHES[row["workflow_id"]]
        assert row["failure_policy"] == "stop"
        assert row["runtime_checks_required"] is True
        for step in row["steps"]:
            entry = registry._entry(step["capability_id"])
            assert step["definition_version"] == entry["version"]
            assert entry["handler"] in PARAM_SCHEMAS
    collect = library.get_workflow("inspection.collect", 1)
    assert collect["steps"][0]["definition_version"] == 2
    assert collect["release_gate"] == "inspection_plan"
    assert collect["request_approval"] is True


def test_all_public_trees_are_detached_and_binding_snapshot_is_not_shared():
    original = library.list_workflows()
    mutated = library.list_workflows()
    mutated["items"][0]["slots_schema"]["properties"]["page"]["default"] = 999
    mutated["items"].clear()
    assert library.list_workflows() == original
    slots, scope = {"policy_id": 7}, deepcopy(TARGET)
    candidate = compile_recipe("inspection.collect", slots=slots, scope=scope)
    frozen = deepcopy(candidate)
    slots["policy_id"] = 8
    scope["targets"][0]["instance_id"] = 20
    assert candidate == frozen
    candidate["workflow_binding"]["recipe_snapshot"]["steps"].clear()
    candidate["plan_inputs"][0]["steps"][0]["params"]["policy_id"] = 9
    assert compile_recipe("inspection.collect", slots={"policy_id": 7}, scope=TARGET) == frozen


@pytest.mark.parametrize("change", ["schema", "status", "outcome"])
def test_runtime_definition_drift_is_closed_even_after_previous_read(monkeypatch, change):
    old = library.get_workflow("inspection.jobs.list", 1)
    saved = deepcopy(old)
    if change == "schema":
        original = JobListParams.model_json_schema

        def changed_schema(*args, **kwargs):
            schema = original(*args, **kwargs)
            schema["properties"]["page"]["default"] = 2
            return schema

        monkeypatch.setattr(JobListParams, "model_json_schema", changed_schema)
    elif change == "status":
        monkeypatch.setattr(jobs, "ALL_STATUSES", (*jobs.ALL_STATUSES, "new_status"))
    else:
        monkeypatch.setattr(run_status, "OUTCOMES", run_status.OUTCOMES | {"new_outcome"})
    fails("workflow_definition_changed", lambda: library.get_workflow("inspection.jobs.list", 1))
    fails("workflow_definition_changed", lambda: compile_recipe())
    fails("workflow_definition_changed", lambda: library.match_workflow("list jobs"))
    assert old == saved


@pytest.mark.parametrize("name,version,code", [
    ("unknown", 1, "workflow_not_found"), ("inspection.collect", 2, "workflow_not_found"),
    ("inspection.collect", True, "workflow_invalid_reference"),
    ("inspection.collect", "1", "workflow_invalid_reference"),
    (None, 1, "workflow_invalid_reference"),
])
def test_references_have_no_latest_or_boolean_versions(name, version, code):
    fails(code, lambda: library.get_workflow(name, version))


@pytest.mark.parametrize("name", list(HASHES)[:3])
def test_three_global_read_candidates_are_unaccepted_and_use_actual_defaults(name):
    value = compile_recipe(name)
    assert value["accepted"] is False and value["missing_fields"] == []
    assert len(value["plan_inputs"]) == 1
    plan = value["plan_inputs"][0]
    assert plan["instance_id"] is None and plan["request_approval"] is False
    step = plan["steps"][0]
    model = PARAM_SCHEMAS[next(recipe.steps[0].model_key for recipe in library._RECIPES
                              if recipe.workflow_id == name)]
    assert step["params"] == model.model_validate({}).model_dump(mode="json")
    assert step["input_refs"] == {} and step["retries"] == 0 and step["fanout"] == 1


@pytest.mark.parametrize("field,values", [
    ("status", jobs.ALL_STATUSES), ("outcome", sorted(run_status.OUTCOMES)),
])
def test_job_filters_use_exact_domain_vocabulary_and_csv_validation(field, values):
    for value in values:
        candidate = compile_recipe(slots={field: value})
        assert candidate["plan_inputs"][0]["steps"][0]["params"][field] == value
    csv = ", ".join(values)
    assert compile_recipe(slots={field: csv})["missing_fields"] == []
    for invalid in ("succeeded", "completed", "plan_only", "unsupported", "SUCCESS"):
        fails("workflow_invalid_params", lambda: compile_recipe(slots={field: invalid}))


@pytest.mark.parametrize("slots", [
    {"page": True}, {"page": "1"}, {"page": 0}, {"page": 1000001},
    {"page_size": 201}, {"page_size": 0}, {"status": ["success"]},
    {"trigger_type": "x" * 21},
])
def test_actual_job_model_rejects_bad_types_and_bounds(slots):
    fails("workflow_invalid_params", lambda: compile_recipe(slots=slots))


@pytest.mark.parametrize("name,slots", [
    ("inspection.templates.list", {"enabled": 1}),
    ("inspection.templates.list", {"db_type": "MYSQL"}),
    ("operations.capabilities.list", {"engine": "host"}),
    ("inspection.collect", {"policy_id": True}),
    ("inspection.collect", {"policy_id": "7"}),
    ("inspection.collect", {"policy_id": 0}),
    ("inspection.collect", {"policy_id": 7, "timeout_seconds": 29}),
    ("inspection.collect", {"policy_id": 7, "timeout_seconds": 3601}),
])
def test_actual_template_matrix_collect_input_models_are_not_widened(name, slots):
    fails("workflow_invalid_params", lambda: compile_recipe(name, slots=slots, scope=TARGET))


@pytest.mark.parametrize("field", [
    "sql", "handler", "steps", "actor", "role", "definition_hash", "workflow_binding",
    "request_approval", "policy_revision", "runtime_digest", "target_fingerprint", "url",
])
def test_no_arbitrary_execution_or_server_owned_slot_entry(field):
    fails("workflow_unknown_field", lambda: compile_recipe("inspection.collect",
          slots={"policy_id": 7, field: "untrusted"}, scope=TARGET))


def test_missing_collect_parameters_never_produce_even_partial_plan_inputs():
    for scope, expected in (({}, "scope.mode"), ({"mode": "explicit"}, "scope.targets"),
                            ({"mode": "explicit", "targets": []}, "scope.targets"),
                            (TARGET, "scope.targets.0.slots.policy_id")):
        candidate = compile_recipe("inspection.collect", scope=scope)
        assert expected in candidate["missing_fields"]
        assert candidate["plan_inputs"] == [] and candidate["effective_inputs"] == []
        assert candidate["accepted"] is False
    mixed = {"mode": "explicit", "targets": [
        {"instance_id": 10, "slots": {"policy_id": 7}}, {"instance_id": 20},
    ]}
    assert compile_recipe("inspection.collect", scope=mixed)["plan_inputs"] == []


def test_effective_defaults_and_slot_placement_have_same_business_hash():
    first = compile_recipe("inspection.collect", slots={"policy_id": 7}, scope=TARGET)
    second = compile_recipe("inspection.collect", slots={"timeout_seconds": 900}, scope={
        "mode": "explicit", "targets": [{"instance_id": 10, "slots": {"policy_id": 7}}],
    })
    assert first["candidate_hash"] == second["candidate_hash"]
    params = first["plan_inputs"][0]["steps"][0]["params"]
    assert params == {"policy_id": 7, "timeout_seconds": 900}
    assert first["plan_inputs"][0]["request_approval"] is True
    assert first["plan_inputs"][0]["valid_for_seconds"] == 3600
    assert first["candidate_hash"] != compile_recipe("inspection.collect", slots={"policy_id": 8},
                                                    scope=TARGET)["candidate_hash"]


def test_same_slot_cannot_be_silently_overridden_even_by_identical_value():
    scope = {"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"page": 1}}]}
    fails("workflow_slot_conflict", lambda: compile_recipe(slots={"page": 1}, scope=scope))
    scope["targets"][0]["slots"] = {"jobs": {"page": 1}}
    fails("workflow_slot_conflict", lambda: compile_recipe("inspection.context.read",
          slots={"jobs": {"page": 1}}, scope=scope))


@pytest.mark.parametrize("scope,code", [
    ({"mode": "global", "targets": []}, "workflow_invalid_scope"),
    ({"mode": "all"}, "workflow_invalid_scope"),
    ({"mode": "explicit", "all": True}, "workflow_unknown_field"),
    ({"targets": []}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": {}}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": True}]}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": "10"}]}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 0}]}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 2**63}]}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 10, "slots": []}]}, "workflow_invalid_scope"),
    ({"mode": "explicit", "targets": [{"instance_id": 10, "actor": 1}]}, "workflow_unknown_field"),
    ({"mode": "explicit", "targets": [{"instance_id": 10}] * 2}, "workflow_duplicate_target"),
    ({"mode": "explicit", "targets": [{"instance_id": n} for n in range(1, 102)]}, "workflow_invalid_scope"),
])
def test_explicit_scope_is_bounded_strict_and_never_discovered(scope, code):
    fails(code, lambda: compile_recipe(scope=scope))


@pytest.mark.parametrize("name", ["inspection.collect", "inspection.context.read"])
def test_collection_and_context_require_explicit_scope(name):
    fails("workflow_invalid_scope", lambda: compile_recipe(name))


def test_fixed_three_read_dag_and_100_targets_preserve_order_and_identity():
    slots = {"matrix": {"engine": "mysql"}, "templates": {"db_type": "mysql", "enabled": True},
             "jobs": {"page_size": 20, "outcome": "unknown"}}
    scope = {"mode": "explicit", "targets": [{"instance_id": n} for n in range(100, 0, -1)]}
    value = compile_recipe("inspection.context.read", slots=slots, scope=scope)
    assert len(value["plan_inputs"]) == 100
    assert [row["instance_id"] for row in value["plan_inputs"]] == list(range(100, 0, -1))
    for row in value["plan_inputs"]:
        steps = row["steps"]
        assert [step["capability_id"] for step in steps] == [
            "operations.capability_matrix.read", "inspection.template.read", "inspection.run.query"]
        assert [step["depends_on"] for step in steps] == [[], [1], [2]]
        assert steps[2]["params"]["outcome"] == "unknown"
    reversed_scope = deepcopy(scope)
    reversed_scope["targets"].reverse()
    assert value["candidate_hash"] != compile_recipe("inspection.context.read", slots=slots,
                                                     scope=reversed_scope)["candidate_hash"]


@pytest.mark.parametrize("slots", [{"matrix": None}, {"jobs": []}, {"matrix": {"sql": "select 1"}}])
def test_compound_groups_are_strict(slots):
    with pytest.raises(library.WorkflowError):
        compile_recipe("inspection.context.read", slots=slots, scope=TARGET)


@pytest.mark.parametrize("text,decision,operation,recipe", [
    ("查看巡检任务", "matched", "prepare", "inspection.jobs.list"),
    ("list jobs", "matched", "prepare", "inspection.jobs.list"),
    ("VIEW TEMPLATES", "matched", "prepare", "inspection.templates.list"),
    ("查询能力矩阵", "matched", "prepare", "operations.capabilities.list"),
    ("查看巡检上下文", "matched", "prepare", "inspection.context.read"),
    ("运行巡检", "matched", "prepare", "inspection.collect"),
    ("prepare inspection", "matched", "prepare", "inspection.collect"),
    ("重新运行巡检", "matched", "rerun", "inspection.collect"),
    ("open inspection templates", "matched", "navigate", "inspection.templates.list"),
    ("打开巡检页面", "matched", "navigate", None),
    ("查看已有巡检报告", "matched", "view_existing", None),
    ("打开报告页面", "matched", "navigate", None),
    ("任务", "needs_input", None, "inspection.jobs.list"),
    ("检查一下", "needs_input", None, None),
    ("不要巡检，只看报告", "needs_input", None, None),
    ("don\u2019t run inspection", "needs_input", None, None),
    ("无需运行巡检", "needs_input", None, None),
    ("禁止执行巡检", "needs_input", None, None),
    ("是否运行巡检", "needs_input", None, None),
    ("if mysql is slow run inspection", "needs_input", None, None),
    ("巡检还是查看报告", "needs_input", None, None),
    ("run inspection on all targets", "needs_input", None, None),
    ("run mysql oracle inspection", "ambiguous", None, None),
    ("运行MySQL和Oracle巡检", "ambiguous", None, None),
    ("查看模板和任务", "ambiguous", None, None),
    ("查看报告和任务", "ambiguous", None, None),
    ("打开并运行巡检", "ambiguous", None, None),
    ("打开并准备巡检", "ambiguous", None, None),
    ("查看任务然后运行巡检", "ambiguous", None, None),
    ("查看并运行巡检", "ambiguous", None, None),
    ("重新生成报告", "rejected", None, None),
    ("创建报告", "rejected", None, None),
    ("compare reports", "rejected", None, None),
    ("select * from secrets", "rejected", None, None),
    ("停止运行巡检", "rejected", None, None),
    ("取消运行巡检", "rejected", None, None),
    ("恢复执行巡检", "rejected", None, None),
    ("resume inspection", "rejected", None, None),
    ("天气怎么样", "not_matched", None, None),
])
def test_operation_first_matching_never_executes_or_turns_views_into_collection(text, decision, operation, recipe):
    value = library.match_workflow(text)
    assert value["decision"] == decision and value["operation"] == operation
    assert (value["workflow"]["workflow_id"] if value["workflow"] else None) == recipe
    assert value["accepted"] is False and "plan_inputs" not in value
    if operation == "view_existing":
        assert value["missing_fields"] == ["result_ref"]
    if operation == "rerun":
        assert "source_plan_id" in value["missing_fields"]


def test_matching_is_normalized_deterministic_and_never_parses_targets_or_policy():
    first = library.match_workflow("  ＬＩＳＴ\nＪＯＢＳ ")
    assert first == library.match_workflow("list jobs")
    assert first == library.match_workflow("list jobs")
    explicit_text = library.match_workflow("run inspection instance 10 policy 7")
    assert explicit_text["missing_fields"] == ["scope.targets", "policy_id"]
    assert "slots" not in explicit_text


@pytest.mark.parametrize("text", [None, 7, "", " ", "x" * 4001, "run\x00inspection", "view\u202ejobs"])
def test_text_budget_and_controls_are_strict(text):
    fails("workflow_invalid_text", lambda: library.match_workflow(text))


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_matcher_version_never_silently_falls_back(version):
    fails("workflow_matcher_not_found", lambda: library.match_workflow("list jobs", matcher_version=version))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), (1,), object(), {1: "x"}])
def test_non_json_input_is_rejected(value):
    fails("workflow_invalid_json", lambda: compile_recipe(slots={"page": value}))


def test_depth_width_string_and_node_budgets_are_enforced_before_validation():
    deep = {}
    for _ in range(20):
        deep = {"nested": deep}
    fails("workflow_invalid_json", lambda: compile_recipe(slots=deep))
    fails("workflow_input_too_large", lambda: compile_recipe(slots={str(n): 1 for n in range(129)}))
    fails("workflow_input_too_large", lambda: compile_recipe(slots={"x": "a" * 16001}))
    fails("workflow_input_too_large", lambda: compile_recipe(slots={"x": [1] * 1001}))
    fails("workflow_input_too_large", lambda: compile_recipe(slots={str(n): "a" * 16000 for n in range(13)}))
    fails("workflow_invalid_json", lambda: compile_recipe(slots={"x": [[0] * 1000 for _ in range(51)]}))


def test_public_functions_do_not_use_actor_database_network_clock_model_or_executor(monkeypatch):
    import socket
    import time
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session
    from app.services import capability_handler, capability_registry, plan_service

    def forbidden(*args, **kwargs):
        raise AssertionError("static library crossed runtime boundary")

    for target, names in ((Engine, ("connect", "begin")),
                          (Session, ("execute", "query", "get", "commit", "flush")),
                          (socket.socket, ("connect", "connect_ex")),
                          (time, ("time", "monotonic", "perf_counter")),
                          (capability_registry, ("effective_capability", "execute_read", "execute_effect")),
                          (plan_service, ("create_plan", "execute_plan"))):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(capability_handler, "HANDLERS", {key: forbidden for key in capability_handler.HANDLERS})
    assert len(library.list_workflows()["items"]) == 5
    assert library.get_workflow("inspection.collect", 1)["workflow_version"] == 1
    assert library.match_workflow("run inspection")["accepted"] is False
    assert compile_recipe("inspection.collect", slots={"policy_id": 7}, scope=TARGET)["accepted"] is False


def test_fresh_import_has_no_database_or_network_access_and_cross_process_hash_is_stable():
    root = Path(__file__).resolve().parents[2]
    script = """
import json
import socket
from sqlalchemy.engine import Engine
def denied(*args, **kwargs):
    raise AssertionError('unexpected IO during import or compilation')
Engine.connect = denied
socket.socket.connect = denied
from app.services import workflow_library as library
value = library.compile_workflow('inspection.collect', 1, slots={'policy_id': 7},
    scope={'mode': 'explicit', 'targets': [{'instance_id': 10}]})
import sys
sys.stdout.write(json.dumps({'catalog': library.list_workflows(), 'candidate': value}, sort_keys=True))
"""
    env = {**os.environ, "PYTHONPATH": str(root / "backend"), "DATABASE_URL": "sqlite://",
           "DB_TYPE": "sqlite", "APP_MODE": "quality_test", "DEBUG": "false",
           "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    for seed in ("1", "98765"):
        completed = subprocess.run([sys.executable, "-c", script], cwd=root,
                                   env={**env, "PYTHONHASHSEED": seed}, capture_output=True,
                                   text=True, timeout=30, check=True)
        results.append(json.loads(completed.stdout))
    assert results[0] == results[1]
    assert results[0]["catalog"] == library.list_workflows()
    assert results[0]["candidate"] == compile_recipe("inspection.collect", slots={"policy_id": 7}, scope=TARGET)
