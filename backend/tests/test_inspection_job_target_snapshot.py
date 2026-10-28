"""Job target provenance and lifecycle checks against isolated SQLite only."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.analyzers.rules import Finding
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import jobs, report_builder, result_writer


@pytest.fixture
def context(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 11, 12)
    monkeypatch.setattr(jobs, "utcnow", lambda: now)
    with Session(engine, autoflush=False) as db:
        instance = DBInstance(
            instance_name="original-name", host="original.invalid", port=3306,
            db_type="mysql", username="synthetic", password_cipher="not-for-snapshot", enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key="synthetic-mysql", name="Synthetic policy", enabled=1,
            db_types_json=["mysql"], item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
        )
        db.add_all([instance, policy])
        db.commit()
        yield SimpleNamespace(db=db, instance=instance, policy=policy, now=now)
    engine.dispose()


def enqueue(context, **kwargs):
    return jobs.enqueue(context.db, context.instance, "mysql", context.policy, **kwargs)


def finding():
    return Finding("synthetic", "check", "normal", "one", "1", "", "ok", "", {
        "execution_status": "success", "sql": "SELECT 1", "rows": [{"one": 1}],
    })


def complete(context, job):
    assert jobs.claim(context.db, job.id, "owner")
    assert jobs.commit_results(context.db, job.id, "owner", [finding()])
    return report_builder.build_report_payload(context.db, job.id)["report"]["instance"]


def legacy_job(context):
    job = enqueue(context, idempotency_key="legacy-request")
    snapshot = dict(job.policy_snapshot_json)
    snapshot.pop(jobs.TARGET_SNAPSHOT_KEY)
    job.policy_snapshot_json = snapshot
    job.policy_revision = jobs._digest(snapshot)
    context.db.commit()
    return job


def test_enqueue_freezes_target_in_existing_json_and_covers_it_with_revision(context):
    job = enqueue(context)
    context.db.expire_all()
    frozen = job.policy_snapshot_json[jobs.TARGET_SNAPSHOT_KEY]
    assert frozen == {
        "id": context.instance.id, "name": "original-name", "host": "original.invalid", "port": 3306,
        "db_type": "mysql", "status": "recorded",
    }
    assert jobs._digest(job.policy_snapshot_json) == job.policy_revision
    assert "password_cipher" not in frozen and "username" not in frozen
    assert jobs.job_view(job)["policy_key"] == context.policy.policy_key


def test_claim_and_commit_use_queued_target_not_current_display_configuration(context):
    job = enqueue(context)
    frozen = deepcopy(job.policy_snapshot_json)
    context.instance.instance_name = "renamed-before-claim"
    context.db.commit()
    assert jobs.claim(context.db, job.id, "owner")
    assert job.policy_snapshot_json == frozen
    context.instance.instance_name = "renamed-before-commit"
    context.db.commit()
    assert jobs.commit_results(context.db, job.id, "owner", [finding()])
    result = report_builder.build_report_payload(context.db, job.id)
    assert result["report"]["instance"] == frozen[jobs.TARGET_SNAPSHOT_KEY]
    assert result_writer.list_tasks(context.db)[0]["instance_name"] == "original-name"
    context.db.refresh(job)
    assert job.policy_snapshot_json == frozen


def test_completed_report_keeps_target_after_configuration_deletion(context):
    job = enqueue(context)
    expected = complete(context, job)
    context.db.delete(context.instance)
    context.db.commit()
    assert report_builder.build_report_payload(context.db, job.id)["report"]["instance"] == expected


def test_retargeted_connection_still_fails_fingerprint_validation(context):
    job = enqueue(context)
    frozen = deepcopy(job.policy_snapshot_json)
    assert jobs.claim(context.db, job.id, "owner")
    context.instance.host = "different.invalid"
    context.db.commit()
    with pytest.raises(jobs.JobError, match="目标已变更"):
        jobs.commit_results(context.db, job.id, "owner", [finding()])
    assert context.db.query(DBCheckResult).count() == 0
    context.db.refresh(job)
    assert job.stage == "validating" and job.policy_snapshot_json == frozen


def test_idempotent_resubmission_after_rename_preserves_first_snapshot(context):
    job = enqueue(context, idempotency_key="same-request")
    frozen = deepcopy(job.policy_snapshot_json)
    context.instance.instance_name = "renamed"
    context.db.commit()
    repeated = enqueue(context, idempotency_key="same-request")
    assert repeated.id == job.id and repeated.policy_snapshot_json == frozen
    assert context.db.query(DBInspectionJob).count() == 1
    with pytest.raises(jobs.JobError, match="不同的巡检参数"):
        enqueue(context, idempotency_key="same-request", timeout_seconds=120)


def test_retry_inherits_original_target_and_does_not_mutate_parent(context):
    parent = enqueue(context)
    frozen = deepcopy(parent.policy_snapshot_json)
    assert jobs.claim(context.db, parent.id, "owner")
    jobs.finish_error(context.db, parent.id, "owner", "failed", "synthetic")
    context.instance.instance_name = "new-name-for-retry"
    context.db.commit()
    retry = enqueue(context, parent_job=parent)
    assert retry.attempt == 2 and retry.policy_snapshot_json == frozen
    assert retry.policy_snapshot_json is not parent.policy_snapshot_json
    assert complete(context, retry)["name"] == "original-name"
    context.db.refresh(parent)
    assert parent.policy_snapshot_json == frozen


def test_legacy_job_and_its_retry_never_backfill_from_current_config(context):
    parent = legacy_job(context)
    frozen = deepcopy(parent.policy_snapshot_json)
    context.instance.instance_name = "current-only-name"
    context.db.commit()
    assert enqueue(context, idempotency_key="legacy-request").id == parent.id
    assert jobs.claim(context.db, parent.id, "owner")
    assert parent.policy_snapshot_json == frozen
    jobs.finish_error(context.db, parent.id, "owner", "failed", "synthetic")
    retry = enqueue(context, parent_job=parent)
    assert retry.policy_snapshot_json == frozen
    target = complete(context, retry)
    assert target["status"] == "historical_context_missing"
    assert target["host"] == target["port"] == ""
    assert target["name"] == str(context.instance.id)
    raw = context.db.query(DBCheckResult).filter_by(task_id=retry.id).one().raw_data
    assert raw[jobs.TARGET_SNAPSHOT_KEY]["status"] == "historical_context_missing"


@pytest.mark.parametrize("snapshot", [None, {}, {"status": "recorded"}, "invalid", {"host": "old.invalid"}])
def test_incomplete_legacy_job_target_is_missing_not_recorded(context, snapshot):
    job = legacy_job(context)
    job.policy_snapshot_json = {**job.policy_snapshot_json, jobs.TARGET_SNAPSHOT_KEY: snapshot}
    job.policy_revision = jobs._digest(job.policy_snapshot_json)
    context.db.commit()
    target = complete(context, job)
    assert target["status"] == "historical_context_missing"
    assert result_writer.list_tasks(context.db)[0]["target_snapshot_status"] == "historical_context_missing"


@pytest.mark.parametrize("field,value", [("id", 999), ("db_type", "oracle")])
def test_foreign_target_snapshot_is_rejected_without_retargeting(context, field, value):
    job = enqueue(context)
    snapshot = deepcopy(job.policy_snapshot_json)
    snapshot[jobs.TARGET_SNAPSHOT_KEY][field] = value
    job.policy_snapshot_json = snapshot
    job.policy_revision = jobs._digest(snapshot)
    context.db.commit()
    assert jobs.claim(context.db, job.id, "owner")
    with pytest.raises(jobs.JobError, match="任务身份不一致"):
        jobs.commit_results(context.db, job.id, "owner", [finding()])
    assert context.db.query(DBCheckResult).count() == 0


def test_snapshot_integrity_is_enforced_on_commit_retry_and_resubmission(context):
    job = enqueue(context, idempotency_key="integrity")
    snapshot = deepcopy(job.policy_snapshot_json)
    snapshot[jobs.TARGET_SNAPSHOT_KEY]["name"] = "tampered"
    job.policy_snapshot_json = snapshot
    context.db.commit()
    assert jobs.claim(context.db, job.id, "owner")
    with pytest.raises(jobs.JobError, match="快照校验失败"):
        jobs.commit_results(context.db, job.id, "owner", [finding()])
    jobs.finish_error(context.db, job.id, "owner", "failed", "synthetic")
    with pytest.raises(jobs.JobError, match="原任务快照校验失败"):
        enqueue(context, parent_job=job)
    with pytest.raises(jobs.JobError, match="不同的巡检参数"):
        enqueue(context, idempotency_key="integrity")


def test_host_job_freezes_requested_domain_without_changing_connection_fingerprint(context):
    context.policy.db_types_json = ["host"]
    context.policy.item_config_json = [{"key": "host.cpu.used_pct", "enabled": True}]
    context.db.commit()
    job = jobs.enqueue(context.db, context.instance, "host", context.policy)
    assert job.policy_snapshot_json[jobs.TARGET_SNAPSHOT_KEY]["db_type"] == "host"
    assert job.target_fingerprint == jobs.target_fingerprint(context.instance)
    assert complete(context, job)["db_type"] == "host"


def test_incomplete_previously_recorded_result_is_reported_as_missing(context):
    context.db.add(DBCheckResult(
        task_id="old-buggy-result", instance_id=context.instance.id, db_type="mysql", check_status="normal",
        raw_data={jobs.TARGET_SNAPSHOT_KEY: {
            "id": context.instance.id, "db_type": "mysql", "name": None, "host": None, "port": None, "status": "recorded",
        }},
    ))
    context.db.commit()
    target = report_builder.build_report_payload(context.db, "old-buggy-result")["report"]["instance"]
    assert target["status"] == "historical_context_missing"
    assert context.db.query(DBCheckResult).one().raw_data[jobs.TARGET_SNAPSHOT_KEY]["status"] == "recorded"


def test_cancel_lease_expiry_and_duplicate_commit_do_not_publish_results(context):
    cancelled = enqueue(context)
    jobs.cancel(context.db, cancelled)
    assert jobs.claim(context.db, cancelled.id, "owner") is None
    expired = enqueue(context)
    assert jobs.claim(context.db, expired.id, "owner", now=context.now - timedelta(seconds=60))
    assert jobs.reap_interrupted(context.db) == 1
    assert not jobs.commit_results(context.db, expired.id, "owner", [finding()])
    finished = enqueue(context)
    complete(context, finished)
    assert not jobs.commit_results(context.db, finished.id, "owner", [finding()])
    assert context.db.query(DBCheckResult).count() == 1
