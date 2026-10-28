"""Approve, durably admit and collect through the real inspection queue and report readers."""
import pytest

from app.models.capability_plan import DBCapabilityPlan
from app.models.check_result import DBCheckResult
from app.models.inspection_job import DBInspectionJob
from app.models.user import User
from app.core.config import settings
from app.services import capability_registry as registry, plan_governance as governance, plan_queue, plan_service as plans
from app.services.inspection import run_readback
from test_inspection_plan_adapter import env as source_env, KEYS  # noqa: F401


@pytest.fixture
def env(source_env, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "isolated-workflow-signing.pem"))
    return source_env


def prepare(env, monkeypatch, *, engine="mysql", released=True):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", released)
    env.instance.db_type = engine
    env.policy.db_types_json = [engine]
    env.policy.item_config_json = [{"key": KEYS[engine], "enabled": True}]
    admin = User(username="independent-plan-approver", password_hash="fixture-unused", role="admin", status="active")
    env.db.add(admin)
    env.db.commit()
    requester, approver = {"user_id": env.actor.id}, {"user_id": admin.id}
    registry.sync_definitions(env.db, actor=approver)
    saved = plans.create_plan(env.db, actor=requester, capability_id="inspection.run.execute",
                              definition_version=2, instance_id=env.instance.id,
                              params={"policy_id": env.policy.id})
    return requester, approver, saved


def admit(env, requester, approver, saved):
    governance.request_approval(env.db, saved["plan_id"], actor=requester, expected_revision=1)
    approved = governance.approve_plan(env.db, saved["plan_id"], actor=approver, expected_revision=1,
                                       expected_binding_hash=saved["binding_hash"])
    assert approved["approval_state"] == "approved"
    return plan_queue.enqueue_batch(env.db, [saved["plan_id"]], actor=requester, idempotency_key="inspection-workflow")


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
def test_four_engine_fixtures_use_approval_durable_worker_actual_job_results_and_rule_report(env, monkeypatch, engine):
    requester, approver, saved = prepare(env, monkeypatch, engine=engine)
    assert saved["approval_state"] == "pending" and saved["status"] == "validated"
    assert env.collections == []
    receipt = admit(env, requester, approver, saved)
    assert receipt["status"] == "queued" and receipt["outcome"] is None
    finished = plan_queue.process_one(env.factory, worker_id="inspection-integration")
    assert finished["outcome"] == "succeeded", finished
    env.db.expire_all()
    plan = plans.read_plan(env.db, saved["plan_id"], actor=requester)
    ref = plan["steps"][0]["result_ref"]
    assert plan["outcome"] == "succeeded" and ref["kind"] == "inspection_job"
    job = env.db.get(DBInspectionJob, ref["inline"]["job_id"])
    assert job.requested_by == requester["user_id"] and job.status == "success"
    read = run_readback.build_run_read(env.db, job.id, instance_id=env.instance.id)
    assert read["results"]["total_count"] >= 1
    assert read["report"] and read["evidence"]["state"] != "missing"
    assert env.collections == [engine]
    assert plan_queue.process_one(env.factory, worker_id="not-a-retry") is None


@pytest.mark.parametrize("statuses,outcome", [(["failed"], "failed"), (["success", "failed"], "partial")])
def test_collector_failures_and_partial_results_survive_to_the_plan_and_batch(env, monkeypatch, statuses, outcome):
    env.result_statuses = statuses
    requester, approver, saved = prepare(env, monkeypatch)
    receipt = admit(env, requester, approver, saved)
    finished = plan_queue.process_one(env.factory, worker_id="inspection-failure")
    assert finished["outcome"] == outcome, finished
    env.db.expire_all()
    batch = plan_queue.read_batch(env.db, receipt["batch_id"], actor=requester)
    assert batch["outcome"] == outcome
    read = plans.read_plan(env.db, saved["plan_id"], actor=requester)
    assert read["outcome"] == outcome
    assert read["steps"][0]["result_ref"]["inline"]["outcome"] == outcome


def test_collection_release_remains_explicit_and_ordinary_read_dispatch_cannot_bypass_it(env, monkeypatch):
    requester, _approver, saved = prepare(env, monkeypatch, released=False)
    assert saved["status"] == "settled" and saved["outcome"] == "blocked"
    assert saved["outcome_kind"] == "capability_not_released"
    assert env.collections == [] and env.db.query(DBInspectionJob).count() == 0
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    with pytest.raises(registry.CapabilityError, match="explicit_plan_required"):
        registry.execute_read(env.db, "inspection.run.execute", 2, actor=requester,
                               instance_id=env.instance.id, params={"policy_id": env.policy.id})


def test_template_edit_after_approval_prevents_admission_with_no_collection(env, monkeypatch):
    requester, approver, saved = prepare(env, monkeypatch)
    governance.request_approval(env.db, saved["plan_id"], actor=requester, expected_revision=1)
    governance.approve_plan(env.db, saved["plan_id"], actor=approver, expected_revision=1,
                            expected_binding_hash=saved["binding_hash"])
    env.policy.threshold_config_json = {"fixture_changed": 7}
    env.db.commit()
    with pytest.raises(plan_queue.QueueError, match="inspection_policy_changed"):
        plan_queue.enqueue_batch(env.db, [saved["plan_id"]], actor=requester, idempotency_key="edited-template")
    assert env.collections == [] and env.db.query(DBCheckResult).count() == 0
    assert env.db.get(DBCapabilityPlan, saved["plan_id"]).outcome is None
