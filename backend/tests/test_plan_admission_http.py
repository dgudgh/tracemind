"""Persist all explicit targets before admission; HTTP acceptance is not completion."""
import pytest
import os
from pathlib import Path
import subprocess
import sys

from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_preparation import DBCapabilityPreparation
from app.models.capability_control import DBCapabilityControlCommand, DBCapabilityTargetFence, DBCapabilityReconciliation
from app.services import plan_queue
from test_assistant_api import api as base_api, signing_key_path, assert_rejected  # noqa: F401


@pytest.fixture
def api(base_api):
    for model in (DBCapabilityBatch, DBCapabilityExecution, DBCapabilityPreparation,
                  DBCapabilityControlCommand, DBCapabilityTargetFence, DBCapabilityReconciliation):
        model.__table__.create(base_api.engine, checkfirst=True)
    return base_api


def prepare(api, *, user=2, key="prepare-two-targets", targets=None):
    return api.request("POST", "/batches/prepare", user=user, headers={"Idempotency-Key": key}, json={
        "capability_id": "inspection.run.query", "definition_version": 1,
        "targets": targets if targets is not None else [{"instance_id": 10}, {"instance_id": 20}],
    })


def admit(api, prepared, *, key="accept-two-targets", user=2):
    return api.request("POST", "/batches", user=user, headers={"Idempotency-Key": key},
                       json={"plan_ids": [item["plan_id"] for item in prepared["items"]]})


def test_http_admits_all_targets_then_worker_runs_without_another_browser_request(api):
    response = prepare(api)
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["accepted"] is False and prepared["total"] == 2
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityExecution).count() == 0
    accepted = admit(api, prepared)
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    assert receipt["status"] == "queued" and receipt["outcome"] is None
    assert receipt["settled_plans"] == 0 and receipt["queue_counts"] == {"queued": 2}
    # The rest of the work is exclusively the durable worker, not a client target loop.
    first = plan_queue.process_one(api.factory, worker_id="http-worker")
    second = plan_queue.process_one(api.factory, worker_id="http-worker")
    assert first["outcome"] == second["outcome"] == "succeeded"
    readback = api.request("GET", "/batches/" + receipt["batch_id"])
    assert readback.status_code == 200, readback.text
    assert readback.json()["outcome"] == "succeeded" and readback.json()["settled_plans"] == 2
    assert len({item["plan_id"] for item in readback.json()["items"]}) == 2


def test_prepare_idempotency_binds_the_complete_ordered_target_set(api):
    first = prepare(api).json()
    again = prepare(api).json()
    assert [item["plan_id"] for item in first["items"]] == [item["plan_id"] for item in again["items"]]
    assert all(item["reused"] for item in again["items"])
    assert_rejected(prepare(api, targets=[{"instance_id": 10}]), 409, "idempotency_conflict")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2


def test_bad_second_target_rolls_back_the_entire_preparation(api):
    assert_rejected(prepare(api, targets=[{"instance_id": 10}, {"instance_id": 999}]), 404, "target_unavailable")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 0


def test_real_scope_filters_every_target_not_only_first_or_hint(api):
    assert_rejected(prepare(api, user=4), 403)
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 0
    scoped = prepare(api, user=4, targets=[{"instance_id": 10}]).json()
    admitted = admit(api, scoped, user=4)
    assert admitted.status_code == 202, admitted.text
    assert api.request("GET", "/batches/" + admitted.json()["batch_id"], user=4).status_code == 200
    assert_rejected(api.request("GET", "/batches/" + admitted.json()["batch_id"], user=3), 404, "batch_not_found")


def test_batch_binding_blocks_revision_and_cancellation_is_only_delivery_cancellation(api):
    saved = prepare(api).json()
    admitted = admit(api, saved).json()
    plan_id = saved["items"][0]["plan_id"]
    assert_rejected(api.request("POST", f"/plans/{plan_id}/revisions", json={
        "expected_revision": 1, "capability_id": "inspection.run.query", "definition_version": 1,
        "instance_id": 10, "params": {"page_size": 9},
    }), 409, "plan_batch_binding_active")
    cancelled = api.request("POST", f"/batches/{admitted['batch_id']}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["outcome"] == "cancelled"
    assert all(item["status"] == "validated" and item["outcome"] is None for item in cancelled.json()["items"])
    assert plan_queue.process_one(api.factory, worker_id="after-cancel") is None


def test_batch_submission_requires_key_and_rejects_raw_commands(api):
    assert_rejected(api.request("POST", "/batches", json={"plan_ids": ["pl" + "0" * 26]}), 400, "invalid_idempotency_key")
    assert_rejected(api.request("POST", "/batches/prepare", user=None, json={}), 401)
    response = prepare(api, targets=[{"instance_id": 10, "params": {"sql": "not executable"}}])
    assert_rejected(response, 400, "plan_raw_payload_rejected")


def test_independent_cli_processes_recover_all_admitted_members_from_persisted_database(api):
    prepared = prepare(api, key="independent-worker-prepare").json()
    receipt = admit(api, prepared, key="independent-worker-submit").json()
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, DATABASE_URL=str(api.engine.url), DB_TYPE="sqlite", APP_MODE="quality_test",
               DEBUG="false", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root / "backend"))
    # Each invocation is a new interpreter; no HTTP request or in-memory target list
    # survives into either worker. Only the committed platform database is shared.
    for attempt in (1, 2):
        result = subprocess.run([sys.executable, "scripts/capability_worker.py", "--once",
                                 "--worker-id", f"independent-cli-{attempt}"],
                                cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    final = api.request("GET", "/batches/" + receipt["batch_id"])
    assert final.status_code == 200, final.text
    assert final.json()["outcome"] == "succeeded" and final.json()["settled_plans"] == 2
    with api.factory() as db:
        rows = db.query(DBCapabilityExecution).filter_by(batch_id=receipt["batch_id"]).all()
        assert {row.worker_id for row in rows} == {"independent-cli-1", "independent-cli-2"}
