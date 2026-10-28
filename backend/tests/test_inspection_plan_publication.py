"""Durable plan acceptance for populated slow SQL; target responses are explicit fixtures."""
import pytest
from sqlalchemy import event

from app.models.check_result import DBCheckResult
from app.models.inspection_job import DBInspectionJob
from app.models.slow_sql import DBSlowSQL
from app.models.user import User
from app.models.report_center import ReportArtifact
from app.core.config import settings
from app.services import capability_registry as registry, plan_governance, plan_queue, plan_service
from app.services.inspection import run_readback
from app.api.v1 import report, report_center
from test_inspection_staged_publication import env as source_env  # noqa: F401


@pytest.fixture
def env(source_env, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "isolated-report-signing.pem"))
    return source_env


def accepted(env, monkeypatch):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    approver = User(username="publication-approver", password_hash="unused", role="admin", status="active")
    env.db.add(approver)
    env.db.commit()
    actor = {"user_id": env.actor.id}
    reviewer = {"user_id": approver.id}
    registry.sync_definitions(env.db, actor=reviewer)
    plan = plan_service.create_plan(
        env.db, actor=actor, capability_id="inspection.run.execute", definition_version=2,
        instance_id=env.instance.id, params={"policy_id": env.policy.id},
    )
    assert not env.source_calls
    plan_governance.request_approval(env.db, plan["plan_id"], actor=actor, expected_revision=1)
    plan_governance.approve_plan(
        env.db, plan["plan_id"], actor=reviewer, expected_revision=1,
        expected_binding_hash=plan["binding_hash"],
    )
    receipt = plan_queue.enqueue_batch(
        env.db, [plan["plan_id"]], actor=actor, idempotency_key="populated-publication",
    )
    return actor, plan, receipt


def test_approved_worker_publication_has_real_slow_sql_ids_and_matching_report(env, monkeypatch):
    actor, plan, receipt = accepted(env, monkeypatch)
    finished = plan_queue.process_one(env.factory, worker_id="populated-inspection")
    assert finished["outcome"] == "succeeded", finished
    with env.factory() as verify:
        saved = plan_service.read_plan(verify, plan["plan_id"], actor=actor)
        batch = plan_queue.read_batch(verify, receipt["batch_id"], actor=actor)
        assert batch["outcome"] == saved["outcome"] == "succeeded"
        ref = saved["steps"][0]["result_ref"]["inline"]
        assert ref["status"] == ref["outcome"] == "success"
        job = verify.get(DBInspectionJob, ref["job_id"])
        assert job.instance_id == env.instance.id and job.requested_by == actor["user_id"]
        read = run_readback.build_run_read(verify, job.id, instance_id=env.instance.id)
        evidence = next(item["raw_data"] for item in read["results"]["items"]
                        if item["metric_name"].endswith(".slow_sql.persistence"))
        rows = verify.query(DBSlowSQL).filter_by(instance_id=env.instance.id).all()
        assert evidence["execution_status"] == "success"
        assert evidence["count"] == len(rows) == 2
        assert set(evidence["record_ids"]) == {row.id for row in rows}
        assert read["run"]["status"] == read["report"]["task"]["task_status"] == "success"
        assert ref["report"]["available"] and ref["report"]["state"] == "synced"
        assert len(ref["report"]["artifacts"]) == 1
        artifact_id = ref["report"]["artifacts"][0]["id"]
        artifact = verify.get(ReportArtifact, artifact_id)
        assert artifact.source_id == job.id and artifact.instance_id == env.instance.id
        assert artifact.signature and artifact.content_markdown
        original_signature, original_retention = artifact.signature, artifact.retention_until
        detail = report.api_inspection_structured_detail(job.id, instance_id=env.instance.id, db=verify)
        assert detail["markdown"] and job.id in detail["markdown"]
        indexed = report_center._collect_and_sync(
            verify, "isolated-acceptance", "inspection", 5, {env.instance.id},
        )
        assert len(indexed) == 1 and indexed[0].source_id == job.id
        assert indexed[0].id == artifact_id
        assert indexed[0].signature == original_signature and indexed[0].retention_until == original_retention
        assert indexed[0].content_markdown and indexed[0].signature
        current = run_readback.build_run_read(verify, job.id, instance_id=env.instance.id)
        assert current["reports"]["state"] == "synced"
        assert current["reports"]["artifacts"][0]["id"] == indexed[0].id
    assert env.source_calls == [env.kind]
    assert plan_queue.process_one(env.factory, worker_id="not-an-implicit-retry") is None


def test_failed_evidence_publication_rolls_back_slow_sql_and_does_not_claim_success(env, monkeypatch):
    actor, plan, receipt = accepted(env, monkeypatch)

    def refuse_result_insert(*_args):
        raise RuntimeError("isolated evidence publication failure")

    event.listen(DBCheckResult, "before_insert", refuse_result_insert)
    try:
        finished = plan_queue.process_one(env.factory, worker_id="publication-failure")
    finally:
        event.remove(DBCheckResult, "before_insert", refuse_result_insert)
    assert finished["outcome"] == "failed", finished
    with env.factory() as verify:
        saved = plan_service.read_plan(verify, plan["plan_id"], actor=actor)
        batch = plan_queue.read_batch(verify, receipt["batch_id"], actor=actor)
        assert saved["outcome"] == batch["outcome"] == "failed"
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        ref = saved["steps"][0]["result_ref"]["inline"]
        read = run_readback.build_run_read(verify, ref["job_id"], instance_id=env.instance.id)
        assert read["run"]["status"] == "failed"
        assert read["results"]["total_count"] == 0
        assert ref["outcome"] == "failed"
    assert env.source_calls == [env.kind]
