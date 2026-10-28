"""Versioned recipes over actual JWT, ownership, persistence and the existing worker."""
import pytest

from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_preparation import DBCapabilityPreparation
from app.models.inspection_policy import DBInspectionPolicy
from app.models.access_control import RoleActionPermission, UserAssetScope
from app.services import access_control, capability_registry as registry, plan_queue
from test_plan_admission_http import api, base_api  # noqa: F401
from test_assistant_api import assert_rejected, signing_key_path  # noqa: F401


WORKFLOW = "inspection.jobs.list"


def payload(*, targets=(10, 20), slots=None):
    return {"operation": "prepare", "slots": slots or {},
            "scope": {"mode": "explicit", "targets": [{"instance_id": value} for value in targets]}}


def prepare(api, body=None, *, workflow=WORKFLOW, version=1, key="http-workflow-prepare", user=2):
    return api.request("POST", f"/workflows/{workflow}/versions/{version}/prepare", user=user,
                       headers={"Idempotency-Key": key}, json=payload() if body is None else body)


def test_catalog_and_matching_are_authorized_reads_not_seeding_or_execution(api):
    with api.factory() as db:
        definitions = [(row.capability_id, row.definition_hash, row.synced_at)
                       for row in db.query(DBCapabilityDefinition).all()]
    catalog = api.request("GET", "/workflows")
    assert catalog.status_code == 200, catalog.text
    values = catalog.json()
    assert values["schema"] == "capability.workflow_catalog/v1"
    assert WORKFLOW in {item["workflow_id"] for item in values["items"]}
    assert "inspection.collect" not in {item["workflow_id"] for item in values["items"]}
    detail = api.request("GET", f"/workflows/{WORKFLOW}/versions/1")
    assert detail.status_code == 200 and detail.json()["workflow_version"] == 1
    for text, decision in (("查看巡检任务", "matched"), ("不要执行巡检", "needs_input"),
                           ("打开并运行巡检", "ambiguous")):
        result = api.request("POST", "/workflows/match", json={"text": text, "matcher_version": 1})
        assert result.status_code == 200, result.text
        assert result.json()["decision"] == decision and result.json()["accepted"] is False
    assert_rejected(api.request("GET", f"/workflows/{WORKFLOW}/versions/999"), 404, "workflow_not_found")
    assert_rejected(api.request("GET", "/workflows/inspection.collect/versions/1"), 403)
    with api.factory() as db:
        assert definitions == [(row.capability_id, row.definition_hash, row.synced_at)
                               for row in db.query(DBCapabilityDefinition).all()]
        assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


def test_prepare_persists_exact_recipe_and_only_its_own_target_before_explicit_admission(api):
    response = prepare(api)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["schema"] == "capability.workflow_preparation/v1" and value["accepted"] is False
    assert value["operation"] == "prepare" and value["source_plan_id"] is None and value["total"] == 2
    assert [item["instance_id"] for item in value["items"]] == [10, 20]
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityPreparation).count() == 1
        assert db.query(DBCapabilityBatch).count() == db.query(DBCapabilityExecution).count() == 0
    for item in value["items"]:
        read = api.request("GET", "/plans/" + item["plan_id"])
        assert read.status_code == 200, read.text
        binding = read.json()["workflow_binding"]
        assert binding["workflow_id"] == WORKFLOW and binding["workflow_version"] == 1
        assert binding["definition_hash"] == value["workflow"]["definition_hash"]
        assert binding["input"]["scope"] == {"mode": "explicit", "targets": [{"instance_id": item["instance_id"]}]}


def test_original_key_recovery_is_owner_only_and_never_creates_another_plan_or_execution(api):
    first = prepare(api).json()
    again = prepare(api)
    assert again.status_code == 200, again.text
    get = api.request("GET", "/workflows/preparation", headers={"Idempotency-Key": "http-workflow-prepare"})
    assert get.status_code == 200, get.text
    assert get.json()["preparation_id"] == first["preparation_id"]
    assert [row["plan_id"] for row in get.json()["items"]] == [row["plan_id"] for row in first["items"]]
    assert_rejected(api.request("GET", "/workflows/preparation", user=3,
                               headers={"Idempotency-Key": "http-workflow-prepare"}), 404)
    assert_rejected(prepare(api, payload(targets=(20, 10))), 409, "idempotency_conflict")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2 and db.query(DBCapabilityPreparation).count() == 1
        assert db.query(DBCapabilityExecution).count() == 0


def test_workflow_receipt_cannot_be_relabelled_as_legacy_batch_preparation(api):
    saved = prepare(api)
    assert saved.status_code == 200, saved.text
    assert_rejected(api.request("GET", "/batches/preparation", headers={
        "Idempotency-Key": "http-workflow-prepare"}), 409, "preparation_binding_changed")
    original = api.request("GET", "/workflows/preparation", headers={"Idempotency-Key": "http-workflow-prepare"})
    assert original.status_code == 200 and original.json()["preparation_id"] == saved.json()["preparation_id"]


def test_scoped_operator_can_prepare_and_revise_shared_template_only_for_authorized_target(api, monkeypatch):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    with api.factory() as db:
        db.add(RoleActionPermission(role_id=api.roles["viewer"], action_key="inspection.execute"))
        db.query(UserAssetScope).filter_by(user_id=4).one().access_level = "operate"
        db.get(DBInspectionPolicy, 1).item_config_json = [{"key": "mysql.connection.used_pct", "enabled": True}]
        db.commit()
        registry.sync_definitions(db, actor={"user_id": 1})
    body = payload(targets=(10,), slots={"policy_id": 1})
    response = prepare(api, body, workflow="inspection.collect", user=4)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    revised = api.request("POST", f"/workflows/plans/{item['plan_id']}/revisions", user=4, json={
        "expected_revision": 1, "slots": {"policy_id": 1, "timeout_seconds": 901}, "scope": body["scope"],
    })
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2 and revised.json()["approval_state"] == "pending"
    assert_rejected(prepare(api, payload(targets=(20,), slots={"policy_id": 1}),
                            workflow="inspection.collect", user=4, key="foreign-template-target"), 403)


def test_explicit_global_read_runs_only_after_original_queue_admission_and_survives_readback(api):
    response = prepare(api, {"scope": {"mode": "global"}, "slots": {}, "operation": "prepare"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["total"] == 1 and value["items"][0]["instance_id"] is None
    plan_id = value["items"][0]["plan_id"]
    admitted = api.request("POST", "/batches", headers={"Idempotency-Key": "global-workflow-admit"}, json={"plan_ids": [plan_id]})
    assert admitted.status_code == 202, admitted.text
    assert plan_queue.process_one(api.factory, worker_id="workflow-http-global")["outcome"] == "succeeded"
    result = api.request("GET", "/plans/" + plan_id)
    assert result.status_code == 200 and result.json()["outcome"] == "succeeded"
    assert result.json()["workflow_binding"]["input"]["scope"] == {"mode": "global"}


def test_scoped_recipe_checks_every_target_and_never_turns_global_into_a_target_grant(api):
    assert_rejected(prepare(api, user=4), 403)
    assert_rejected(prepare(api, {"scope": {"mode": "global"}, "slots": {}}, user=4), 403)
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0
    valid = prepare(api, payload(targets=(10,)), user=4)
    assert valid.status_code == 200, valid.text
    assert valid.json()["items"][0]["instance_id"] == 10


@pytest.mark.parametrize("patch", [
    {"steps": []}, {"handler": "arbitrary"}, {"approval": True}, {"role": "admin"},
    {"scope": {"mode": "global", "targets": None}},
    {"scope": {"mode": "explicit", "targets": []}},
    {"scope": {"mode": "explicit", "targets": [{"instance_id": True}]}},
    {"scope": {"mode": "explicit", "targets": [{"instance_id": 10, "sql": "not executable"}]}},
])
def test_untrusted_workflow_input_never_becomes_arbitrary_dispatch(api, patch):
    assert_rejected(prepare(api, {**payload(), **patch}), 400)
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


def test_slot_errors_or_a_bad_second_target_leave_no_partial_preparation(api):
    assert_rejected(prepare(api, payload(slots={"sql": "not executable"})), 400)
    assert_rejected(prepare(api, payload(targets=(10, 999))), 404, "target_unavailable")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


def test_receipt_headers_and_queries_are_unambiguous_and_read_only(api):
    response = api.client.get("/api/v1/assistant/workflows/preparation", headers=[
        ("Authorization", "Bearer " + api.tokens[2]), ("Idempotency-Key", "one"), ("Idempotency-Key", "two")])
    assert_rejected(response, 400, "invalid_idempotency_key")
    assert_rejected(api.request("GET", "/workflows/preparation?instance_id=10", headers={"Idempotency-Key": "missing"}), 400, "invalid_query")
    assert_rejected(api.request("GET", "/workflows/preparation"), 400, "invalid_idempotency_key")


def test_workflow_audit_failure_is_not_a_success_and_rolls_back_all_new_rows(api, monkeypatch):
    monkeypatch.setattr(access_control, "append_security_audit", lambda *args, **kwargs: None)
    response = prepare(api)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] in {"plan_audit_unavailable", "workflow_audit_unavailable"}
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


def test_fixed_recipe_revision_is_explicit_and_receipt_detects_frozen_binding_change(api):
    original = prepare(api, payload(targets=(10,))).json()
    plan_id = original["items"][0]["plan_id"]
    revised = api.request("POST", f"/workflows/plans/{plan_id}/revisions", json={
        "expected_revision": 1, "slots": {"page_size": 3},
        "scope": {"mode": "explicit", "targets": [{"instance_id": 10}]},
    })
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2 and revised.json()["workflow_binding"]["input"]["slots"]["page_size"] == 3
    assert_rejected(api.request("GET", "/workflows/preparation", headers={"Idempotency-Key": "http-workflow-prepare"}), 409)
    assert_rejected(api.request("POST", f"/plans/{plan_id}/revisions", json={
        "expected_revision": 2, "capability_id": "inspection.run.query", "definition_version": 1,
        "instance_id": 10, "params": {"page_size": 4},
    }), 409, "workflow_revision_requires_recipe")


def test_unknown_fields_and_nonfinite_json_never_echo_rejected_values(api):
    response = api.request("POST", "/workflows/match", content='{"text":"private text","matcher_version":NaN}',
                           headers={"Content-Type": "application/json"})
    assert_rejected(response, 400)
    assert "private text" not in response.text
    assert_rejected(api.request("POST", "/workflows/match", json={"text": "查看任务", "actor_id": 1}), 400)


def test_explicit_rerun_keeps_saved_result_and_never_replays_a_queued_source(api):
    body = payload(targets=(10,))
    original = prepare(api, body).json()
    parent = original["items"][0]["plan_id"]
    rerun = {**body, "operation": "rerun", "source_plan_id": parent}
    queued = api.request("POST", "/batches", headers={"Idempotency-Key": "rerun-parent-admission"},
                         json={"plan_ids": [parent]})
    assert queued.status_code == 202, queued.text
    assert_rejected(prepare(api, rerun, key="queued-source"), 409, "workflow_rerun_source_unsettled")
    assert plan_queue.process_one(api.factory, worker_id="http-workflow-parent")["outcome"] == "succeeded"
    saved = api.request("GET", "/plans/" + parent).json()
    child_response = prepare(api, rerun, key="explicit-rerun")
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()["items"][0]["plan_id"]
    assert child != parent and child_response.json()["source_plan_id"] == parent
    replay = prepare(api, rerun, key="explicit-rerun")
    assert replay.status_code == 200 and replay.json()["items"][0]["plan_id"] == child
    assert_rejected(prepare(api, rerun, key="duplicate-child"), 409, "parent_plan_already_retried")
    assert api.request("GET", "/plans/" + parent).json() == saved
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityPreparation).count() == 2
        assert db.query(DBCapabilityExecution).count() == 1
        assert db.get(DBCapabilityPlan, child).parent_plan_id == parent


def test_fixed_read_composite_preserves_no_approval_rule_and_uses_original_worker(api):
    result = prepare(api, payload(targets=(10,)), workflow="inspection.context.read")
    assert result.status_code == 200, result.text
    frozen = result.json()["items"][0]
    plan_id = frozen["plan_id"]
    request = api.request("POST", f"/plans/{plan_id}/approval/request", json={"expected_revision": 1})
    assert_rejected(request, 400, "approval_not_required")
    queued = api.request("POST", "/batches", headers={"Idempotency-Key": "approved-composite"},
                         json={"plan_ids": [plan_id]})
    assert queued.status_code == 202, queued.text
    finished = plan_queue.process_one(api.factory, worker_id="http-workflow-composite")
    assert finished["outcome"] == "succeeded", finished
    persisted = api.request("GET", "/plans/" + plan_id).json()
    assert persisted["outcome"] == "succeeded"
    assert [step["depends_on"] for step in persisted["steps"]] == [[], [1], [2]]
    assert all(step["status"] == "succeeded" and step["result_ref"] for step in persisted["steps"])
    assert plan_queue.process_one(api.factory, worker_id="no-browser-replay") is None
