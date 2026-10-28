"""Real JWT, resource scope and versioned control responses over durable plans."""
from types import SimpleNamespace

import pytest

from app.models.access_control import RoleActionPermission
from app.models.capability_control import DBCapabilityControlCommand
from app.services import access_control, plan_queue
from test_plan_admission_http import api as batch_api, base_api, prepare, admit  # noqa: F401
from test_assistant_api import signing_key_path, assert_rejected  # noqa: F401


@pytest.fixture
def api(batch_api):
    with batch_api.factory() as db:
        db.add(RoleActionPermission(role_id=batch_api.roles["viewer"], action_key="control.*"))
        db.commit()
    return batch_api


def queued(api, *, user=2, key="control-http"):
    prepared = prepare(api, user=user, key=key + "-prepare", targets=[{"instance_id": 10}]).json()
    accepted = admit(api, prepared, user=user, key=key + "-enqueue")
    assert accepted.status_code == 202, accepted.text
    batch_id = accepted.json()["batch_id"]
    v2 = api.request("GET", "/batches/" + batch_id, user=user,
                     headers={"X-Assistant-Batch-Version": "2"})
    assert v2.status_code == 200, v2.text
    return SimpleNamespace(batch_id=batch_id, execution_id=v2.json()["items"][0]["execution_id"])


def control(api, item, action, version, *, user=2, key=None, **overrides):
    return api.request("POST", f"/executions/{item.execution_id}/{action}", user=user,
                       headers={"Idempotency-Key": key or f"{item.execution_id}:{action}:{version}"},
                       json={"expected_control_version": version, "reason": "isolated human control", **overrides})


def test_pause_then_resume_uses_current_control_version_and_never_claims_external_stop(api):
    item = queued(api)
    paused = control(api, item, "pause", 0)
    assert paused.status_code == 200, paused.text
    value = paused.json()
    assert value["scope"] == "local_step_boundary"
    assert value["observed_state"] == value["queue_state"] == "paused"
    assert value["external_stop"] == value["external_fence"] == "unsupported"
    assert plan_queue.process_one(api.factory, worker_id="paused-http") is None
    assert_rejected(api.request("GET", "/batches/" + item.batch_id), 409, "batch_control_requires_v2")
    v2 = api.request("GET", "/batches/" + item.batch_id, headers={"X-Assistant-Batch-Version": "2"})
    assert v2.status_code == 200 and v2.json()["status"] == "paused"
    assert_rejected(control(api, item, "resume", 0), 409, "control_version_conflict")
    resumed = control(api, item, "resume", value["control_version"])
    assert resumed.status_code == 200, resumed.text
    finished = plan_queue.process_one(api.factory, worker_id="resume-http")
    assert finished["outcome"] == "succeeded", finished
    view = api.request("GET", f"/executions/{item.execution_id}/control")
    assert view.status_code == 200 and view.json()["observed_state"] == "completed"


def test_same_command_replay_never_increments_version_and_changed_input_conflicts(api):
    item = queued(api)
    first = control(api, item, "pause", 0, key="one-control-key")
    assert first.status_code == 200, first.text
    again = control(api, item, "pause", 0, key="one-control-key")
    assert again.status_code == 200 and again.json()["reused"]
    assert again.json()["command"]["command_id"] == first.json()["command"]["command_id"]
    assert_rejected(control(api, item, "stop", 0, key="one-control-key"), 409, "idempotency_conflict")
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 1


def test_original_command_key_recovers_exact_saved_receipt_even_after_a_later_command(api):
    item = queued(api)
    first = control(api, item, "pause", 0, key="lost-pause-key")
    assert first.status_code == 200, first.text
    later = control(api, item, "resume", first.json()["control_version"], key="later-resume-key")
    assert later.status_code == 200, later.text
    receipt = api.request("GET", f"/executions/{item.execution_id}/control",
                          headers={"Idempotency-Key": "lost-pause-key"})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["command"]["command_id"] == first.json()["command"]["command_id"]
    assert receipt.json()["control_version"] == later.json()["control_version"]
    assert_rejected(api.request("GET", f"/executions/{item.execution_id}/control",
                               headers={"Idempotency-Key": "never-sent-key"}), 404, "control_command_not_found")
    other = queued(api, key="different-execution")
    assert_rejected(api.request("GET", f"/executions/{other.execution_id}/control",
                               headers={"Idempotency-Key": "lost-pause-key"}), 404, "control_command_not_found")
    assert_rejected(api.request("GET", f"/executions/{item.execution_id}/control", user=3,
                               headers={"Idempotency-Key": "lost-pause-key"}), 404, "plan_not_found")
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 2


@pytest.mark.parametrize("headers", [
    {"Idempotency-Key": ""}, {"Idempotency-Key": "bad key"},
    [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
])
def test_control_receipt_rejects_ambiguous_headers_without_mutation(api, headers):
    item = queued(api)
    # The convenience fixture merges mappings; use raw header pairs for duplicates.
    if isinstance(headers, list):
        response = api.client.get(f"/api/v1/assistant/executions/{item.execution_id}/control",
                                  headers=[("Authorization", "Bearer " + api.tokens[2]), *headers])
    else:
        response = api.request("GET", f"/executions/{item.execution_id}/control", headers=headers)
    assert_rejected(response, 400, "invalid_idempotency_key")
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 0


def test_owner_and_operate_scope_are_not_granted_by_control_permission(api):
    item = queued(api)
    assert_rejected(control(api, item, "pause", 0, user=3), 404, "plan_not_found")
    scoped = queued(api, user=4, key="read-scope-control")
    assert_rejected(control(api, scoped, "pause", 0, user=4), 403)
    assert_rejected(control(api, item, "pause", 0, user=None), 401)
    assert_rejected(control(api, item, "pause", 0, user=5), 403)
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 0


def test_audit_unavailable_is_retryable_service_failure_without_a_saved_command(api, monkeypatch):
    item = queued(api)
    monkeypatch.setattr(access_control, "append_security_audit", lambda *args, **kwargs: None)
    assert_rejected(control(api, item, "pause", 0), 503, "control_audit_unavailable")
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 0
    view = api.request("GET", f"/executions/{item.execution_id}/control")
    assert view.status_code == 200 and view.json()["control_version"] == 0
    assert view.json()["control_enabled"] is False


def test_control_input_cannot_override_owner_scope_epoch_handler_or_frozen_result(api):
    item = queued(api)
    for extra in ({"owner_token": "forged"}, {"fencing_epoch": 999}, {"observed_state": "stopped"},
                  {"handler": "not-a-handler"}, {"sql": "not executable"}, {"actor": {"user_id": 1}}):
        assert_rejected(control(api, item, "pause", 0, **extra), 400)
    assert_rejected(api.request("POST", f"/executions/{item.execution_id}/pause", json={
        "expected_control_version": 0, "reason": "missing key",
    }), 400, "invalid_idempotency_key")
    with api.factory() as db:
        assert db.query(DBCapabilityControlCommand).count() == 0


@pytest.mark.parametrize("value", ["0", "3", "2,1", "v2"])
def test_batch_version_is_explicit_and_invalid_versions_are_not_downgraded(api, value):
    item = queued(api)
    assert_rejected(api.request("GET", "/batches/" + item.batch_id,
                               headers={"X-Assistant-Batch-Version": value}), 400, "invalid_batch_schema_version")
