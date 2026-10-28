"""Real HTTP/approval/queue/report tables; engine collectors are isolated fixtures."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import main
from app.api.v1.assistant import router
from app.core import auth, database
from app.models.access_control import RoleActionPermission
from app.models.inspection_job import DBInspectionJob
from app.models.user import User
from app.services import capability_registry as registry, plan_queue
from app.services.inspection import run_readback
from test_inspection_plan_adapter import env as collection_env, KEYS  # noqa: F401


@pytest.fixture
def collection_api(collection_env, monkeypatch, tmp_path):
    env = collection_env
    monkeypatch.setattr(database, "SessionLocal", env.factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "workflow-http.pem"))
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    role_id = env.grant.role_id
    env.db.add_all([RoleActionPermission(role_id=role_id, action_key="asset.read"),
                    RoleActionPermission(role_id=role_id, action_key="inspection.read")])
    human = User(username="workflow-http-independent-approver", password_hash="unused", role="admin", status="active")
    env.db.add(human)
    env.db.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_middleware(main.AuthMiddleware)

    def isolated_db():
        with env.factory() as db:
            yield db

    app.dependency_overrides[database.get_db] = isolated_db
    with TestClient(app) as client:
        yield SimpleNamespace(env=env, human=human, client=client)


@pytest.mark.parametrize("engine", ["mysql", "oracle", "postgresql", "sqlserver"])
def test_recipe_collection_http_approval_worker_and_saved_domain_report(collection_api, engine):
    ctx = collection_api
    env = ctx.env
    env.instance.db_type = engine
    env.policy.db_types_json = [engine]
    env.policy.item_config_json = [{"key": KEYS[engine], "enabled": True}]
    env.db.commit()
    registry.sync_definitions(env.db, actor={"user_id": ctx.human.id})
    owner = {"Authorization": "Bearer " + auth.issue_user_token(env.actor, source="db", db=env.db)["token"]}
    approver = {"Authorization": "Bearer " + auth.issue_user_token(ctx.human, source="db", db=env.db)["token"]}
    prefix = "/api/v1/assistant"
    response = ctx.client.post(prefix + "/workflows/inspection.collect/versions/1/prepare",
        headers={**owner, "Idempotency-Key": "http-collect-" + engine}, json={
            "slots": {"policy_id": env.policy.id},
            "scope": {"mode": "explicit", "targets": [{"instance_id": env.instance.id}]},
        })
    assert response.status_code == 200, response.text
    assert env.collections == []
    item = response.json()["items"][0]
    plan_id = item["plan_id"]
    approval_request = ctx.client.post(prefix + f"/plans/{plan_id}/approval/request", headers=owner,
                                       json={"expected_revision": 1})
    assert approval_request.status_code == 200, approval_request.text
    denied = ctx.client.post(prefix + "/batches", headers={**owner, "Idempotency-Key": "not-approved"},
                             json={"plan_ids": [plan_id]})
    assert denied.status_code == 409, denied.text
    read = ctx.client.get(prefix + f"/plans/{plan_id}/approval", headers=approver)
    assert read.status_code == 200, read.text
    assert read.json()["plan"]["workflow_binding"]["workflow_id"] == "inspection.collect"
    approval = ctx.client.post(prefix + f"/plans/{plan_id}/approval/approve", headers=approver,
        json={"expected_revision": 1, "expected_binding_hash": item["binding_hash"]})
    assert approval.status_code == 200, approval.text
    queued = ctx.client.post(prefix + "/batches", headers={**owner, "Idempotency-Key": "http-collect-admit"},
                             json={"plan_ids": [plan_id]})
    assert queued.status_code == 202, queued.text
    assert plan_queue.process_one(env.factory, worker_id="workflow-http-collector")["outcome"] == "succeeded"
    persisted = ctx.client.get(prefix + "/plans/" + plan_id, headers=owner)
    assert persisted.status_code == 200, persisted.text
    saved = persisted.json()
    assert saved["outcome"] == "succeeded" and saved["workflow_binding"]["workflow_id"] == "inspection.collect"
    job_id = saved["steps"][0]["result_ref"]["inline"]["job_id"]
    env.db.expire_all()
    job = env.db.get(DBInspectionJob, job_id)
    assert job.status == "success" and job.requested_by == env.actor.id
    report = run_readback.build_run_read(env.db, job_id, instance_id=env.instance.id)
    assert report["results"]["total_count"] >= 1 and report["report"]
    assert report["evidence"]["state"] != "missing" and env.collections == [engine]
    assert plan_queue.process_one(env.factory, worker_id="no-second-collection") is None
