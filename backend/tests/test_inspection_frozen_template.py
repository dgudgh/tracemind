"""A queued inspection must run the template revision that was reviewed.

``jobs.execute`` used to re-read the live template, so editing SQL or thresholds while a job
sat in the queue silently changed what the job would run - under the revision the operator
had already approved. The stored snapshot was only used for integrity checks, never as the
execution input.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.analyzers.rules import Finding
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import jobs, runner


@pytest.fixture
def context(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 20, 9)
    monkeypatch.setattr(jobs, "utcnow", lambda: now)
    with Session(engine, autoflush=False) as db:
        instance = DBInstance(
            instance_name="frozen-target", host="original.invalid", port=3306,
            db_type="mysql", username="synthetic", password_cipher="not-used", enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key="frozen-mysql", name="Frozen policy", enabled=1,
            db_types_json=["mysql"],
            item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
            threshold_config_json={"connection_used_pct": 80},
        )
        db.add_all([instance, policy])
        db.commit()
        yield SimpleNamespace(db=db, instance=instance, policy=policy, now=now)
    engine.dispose()


def capture_policy(monkeypatch):
    seen = {}

    def fake_collect(db, instance, policy=None):
        seen["policy"] = policy
        return [Finding("synthetic", "check", "normal", "one", "1", "", "ok", "", {
            "execution_status": "success", "sql": "SELECT 1", "rows": [{"one": 1}],
        })]

    monkeypatch.setattr(runner, "collect_findings", fake_collect)
    return seen


def run_job(context, job):
    job_id = job.id
    assert jobs.claim(context.db, job_id, "owner")
    jobs.execute(context.db, job, "owner")
    context.db.expire_all()
    return context.db.get(type(job), job_id)


def test_execute_uses_the_frozen_item_config_not_the_live_edit(context, monkeypatch):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    original_items = [{"key": "mysql.connection.used_pct", "enabled": True}]

    # Someone edits the template while the job waits in the queue.
    context.policy.item_config_json = [{"key": "mysql.replication.lag_seconds", "enabled": True}]
    context.policy.threshold_config_json = {"connection_used_pct": 10}
    context.db.commit()

    seen = capture_policy(monkeypatch)
    finished = run_job(context, job)

    assert seen["policy"].item_config_json == original_items
    assert seen["policy"].threshold_config_json == {"connection_used_pct": 80}
    assert finished.status == "success"


def test_frozen_policy_reports_the_queued_revision_not_the_current_one(context):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    queued_revision = job.policy_revision
    context.policy.name = "Renamed after enqueue"
    context.policy.item_config_json = []
    context.db.commit()

    context.db.refresh(job)
    frozen = jobs.frozen_policy(job)
    assert frozen.name == "Frozen policy"
    assert frozen.item_config_json == [{"key": "mysql.connection.used_pct", "enabled": True}]
    assert jobs._digest(job.policy_snapshot_json) == queued_revision


def test_disabling_the_template_still_refuses_the_queued_job(context, monkeypatch):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    context.policy.enabled = 0
    context.db.commit()

    seen = capture_policy(monkeypatch)
    finished = run_job(context, job)

    assert "policy" not in seen  # collection never started
    assert finished.status == "failed"
    assert "模板" in (finished.failure_reason or "")


def test_deleting_the_template_still_refuses_the_queued_job(context, monkeypatch):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    context.db.delete(context.policy)
    context.db.commit()

    seen = capture_policy(monkeypatch)
    finished = run_job(context, job)

    assert "policy" not in seen
    assert finished.status == "failed"


def test_a_job_without_a_snapshot_cannot_execute(context):
    job = jobs.enqueue(context.db, context.instance, "mysql", context.policy)
    job.policy_snapshot_json = {}
    job.policy_revision = jobs._digest({})
    context.db.commit()

    with pytest.raises(jobs.JobError, match="缺少模板快照"):
        jobs.frozen_policy(job)
