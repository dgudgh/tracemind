"""Job control: cancellation that separates "accepted" from "stopped", and one retry chain.

Two lies this module refuses to tell:

* a running cancellation is not a stopped run - the target may still be executing, so the job
  stays ``running`` and the response says so;
* a refused commit is not a lost heartbeat - if the owner was cancelled, the job is closed as
  cancelled here and now, instead of being rewritten later by the reaper.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers the whole model metadata
from app.analyzers.rules import Finding
from app.api.v1.inspection import (
    api_cancel_inspection_job,
    api_get_inspection_job,
    api_retry_inspection_job,
    api_run_inspection,
)
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.user import User
from app.services.inspection import jobs
from app.tasks import master_scheduler


@pytest.fixture
def context():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        instance = DBInstance(
            instance_name="control-target", host="127.0.0.1", port=3306,
            db_type="mysql", username="monitor", password_cipher="env://TEST_DB_PASSWORD",
            enabled=1,
        )
        policy = DBInspectionPolicy(
            policy_key="control-policy", name="Control policy", enabled=1,
            db_types_json=["mysql"],
            item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
            threshold_config_json={"connection_used_pct": 80},
        )
        other = DBInspectionPolicy(
            policy_key="other-policy", name="Other policy", enabled=1,
            db_types_json=["mysql"],
            item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
            threshold_config_json={"connection_used_pct": 95},
        )
        db.add_all([instance, policy, other])
        db.add_all([
            User(id=7, username="operator", password_hash="x", role="admin", status="active"),
            User(id=8, username="other", password_hash="x", role="admin", status="active"),
        ])
        db.commit()
        yield SimpleNamespace(db=db, instance=instance, policy=policy, other=other)
    engine.dispose()


def _request(user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(auth={"user_id": user_id, "role": "admin", "source": "db"}),
        headers={"Authorization": "Bearer test-session"},
    )


@pytest.fixture(autouse=True)
def offline_collection(monkeypatch):
    """No target database is contacted: these tests are about control, not collection."""
    monkeypatch.setattr(jobs.runner, "prepare_findings", lambda *args, **kwargs: (None, [ok_finding()]))


def ok_finding() -> Finding:
    return Finding(
        "synthetic", "check", "normal", "one", "1", "", "ok", "",
        {"execution_status": "success", "sql": "SELECT 1", "rows": [{"one": 1}]},
    )


def enqueue(context, **kwargs) -> DBInspectionJob:
    return jobs.enqueue(context.db, context.instance, "mysql", context.policy, **kwargs)


def run_endpoint(context, user_id: int = 7, idempotency_key=None, policy_key=None):
    payload = SimpleNamespace(policy_key=policy_key) if policy_key else None
    return api_run_inspection(
        "mysql", context.instance.id, _request(user_id), payload=payload,
        idempotency_key=idempotency_key, db=context.db,
    )


def refresh(context, job) -> DBInspectionJob:
    context.db.expire_all()
    return context.db.get(DBInspectionJob, job.id)


def test_queued_cancellation_stops_the_run_and_is_recorded(context):
    job = enqueue(context)
    response = api_cancel_inspection_job(job.id, _request(7), db=context.db)

    row = refresh(context, job)
    assert row.status == "cancelled"
    assert row.cancel_requested == 1
    assert row.cancel_requested_at is not None
    assert row.cancel_requested_by == 7
    outcome = response["run"]
    assert outcome["outcome"] == "cancelled"
    assert outcome["outcome_kind"] == "cancelled_before_run"
    assert response["run"]["can_retry"] is True
    assert response["run"]["cancel_attribution"] == "recorded"
    # It never touched the target, so there is nothing to double-check.
    assert outcome["requires_attention"] is False


def test_running_cancellation_is_only_a_request_and_says_so(context):
    job = enqueue(context)
    assert jobs.claim(context.db, job.id, "owner")

    response = api_cancel_inspection_job(job.id, _request(7), db=context.db)

    row = refresh(context, job)
    assert row.status == "running", "a cancel request must not claim the run has stopped"
    assert row.cancel_requested == 1
    assert row.cancel_requested_at is not None
    assert response["run"]["outcome"] is None
    assert "采集可能仍在进行" in response["notice"]
    assert "已停止" not in response["notice"]


def test_refused_commit_closes_the_job_as_cancelled_instead_of_looking_like_a_dead_node(context):
    job = enqueue(context)
    assert jobs.claim(context.db, job.id, "owner")
    jobs.cancel(context.db, job, requested_by=7)
    context.db.expire_all()

    accepted = jobs.commit_results(context.db, job.id, "owner", [ok_finding()])

    assert accepted is False
    context.db.expire_all()
    row = context.db.get(DBInspectionJob, job.id)
    assert context.db.query(DBCheckResult).count() == 0, "cancelled runs must not publish rows"
    assert row.status == "cancelled"
    assert row.owner_token is None and row.lease_until is None
    detail = api_get_inspection_job(job.id, _request(), db=context.db)
    assert detail["run"]["outcome_kind"] == "cancelled_during_run"
    # And the reaper has nothing left to "interrupt": the cause is recorded, not invented.
    assert jobs.reap_interrupted(context.db) == 0
    context.db.expire_all()
    assert context.db.get(DBInspectionJob, job.id).status == "cancelled"


def test_late_cancellation_cannot_rewrite_a_finished_run(context):
    job = enqueue(context)
    assert jobs.claim(context.db, job.id, "owner")
    assert jobs.commit_results(context.db, job.id, "owner", [ok_finding()])
    before = refresh(context, job)
    status, finished_at = before.status, before.finished_at

    with pytest.raises(HTTPException) as conflict:
        api_cancel_inspection_job(job.id, _request(7), db=context.db)

    assert conflict.value.status_code == 409
    after = refresh(context, job)
    assert after.status == status
    assert after.finished_at == finished_at


def test_same_idempotency_key_returns_the_same_run(context):
    first = run_endpoint(context, idempotency_key="stable-key")
    second = run_endpoint(context, idempotency_key="stable-key")

    assert first["job"]["id"] == second["job"]["id"]
    assert context.db.query(DBInspectionJob).count() == 1
    assert first["run"]["run"]["job_id"] == second["run"]["run"]["job_id"]


def test_same_key_with_different_thresholds_is_a_conflict(context):
    run_endpoint(context, idempotency_key="shared-key")
    before = context.db.query(DBInspectionJob).count()

    with pytest.raises(HTTPException) as conflict:
        run_endpoint(context, idempotency_key="shared-key", policy_key="other-policy")

    assert conflict.value.status_code == 409
    assert context.db.query(DBInspectionJob).count() == before


def test_two_users_sharing_a_key_do_not_collide(context):
    run_endpoint(context, user_id=7, idempotency_key="same-key")
    run_endpoint(context, user_id=8, idempotency_key="same-key")

    rows = context.db.query(DBInspectionJob).all()
    assert len(rows) == 2
    assert len({row.idempotency_key for row in rows}) == 2, (
        "the key must be salted with the requester, or one user's request silently becomes "
        "another user's run"
    )
    assert {row.requested_by for row in rows} == {7, 8}


def test_manual_run_records_who_asked_for_it(context):
    response = run_endpoint(context, user_id=7)
    row = context.db.get(DBInspectionJob, response["job"]["id"])

    assert row.requested_by is not None, (
        "a None actor makes jobs.validate_actor skip every check, so a revoked user keeps running"
    )
    assert row.requested_by == 7


def test_revoked_executor_cannot_retry_and_produces_nothing(context):
    job = enqueue(context)
    assert jobs.claim(context.db, job.id, "owner")
    jobs.finish_error(context.db, job.id, "owner", "failed", "synthetic", code="collector_error")
    context.db.expire_all()

    # Still an authenticated account, but it no longer holds inspection.execute.
    user = context.db.get(User, 7)
    user.role = "user"
    context.db.commit()

    with pytest.raises(HTTPException) as denied:
        api_retry_inspection_job(job.id, _request(7), db=context.db)

    assert denied.value.status_code == 409
    assert context.db.query(DBInspectionJob).count() == 1
    assert context.db.query(DBCheckResult).count() == 0
    # The refusal is classified, not parsed out of the message.
    with pytest.raises(jobs.JobError) as error:
        jobs.enqueue(
            context.db, context.instance, "mysql", context.policy,
            requested_by=7, parent_job=context.db.get(DBInspectionJob, job.id),
        )
    assert error.value.classification == "permission_revoked"


def test_disabled_requester_is_classified_as_inactive(context):
    job = enqueue(context)
    assert jobs.claim(context.db, job.id, "owner")
    jobs.finish_error(context.db, job.id, "owner", "failed", "synthetic", code="collector_error")
    context.db.expire_all()
    context.db.get(User, 7).status = "disabled"
    context.db.commit()

    with pytest.raises(jobs.JobError) as error:
        jobs.enqueue(
            context.db, context.instance, "mysql", context.policy,
            requested_by=7, parent_job=context.db.get(DBInspectionJob, job.id),
        )
    assert error.value.classification == "actor_inactive"
    assert context.db.query(DBInspectionJob).count() == 1


def test_retry_inherits_the_frozen_snapshot_and_creates_one_child(context):
    parent = enqueue(context)
    assert jobs.claim(context.db, parent.id, "owner")
    jobs.finish_error(context.db, parent.id, "owner", "failed", "collector down", code="collector_error")
    context.db.expire_all()
    parent = context.db.get(DBInspectionJob, parent.id)

    first = api_retry_inspection_job(parent.id, _request(7), db=context.db)
    second = api_retry_inspection_job(parent.id, _request(7), db=context.db)

    assert first["run"]["job_id"] == second["run"]["job_id"]
    child = context.db.get(DBInspectionJob, first["run"]["job_id"])
    assert child.attempt == parent.attempt + 1 == 2
    assert child.parent_job_id == parent.id
    assert child.policy_revision == parent.policy_revision, (
        "a retry must run the plan the operator reviewed, not whatever the template says now"
    )
    assert context.db.query(DBInspectionJob).count() == 2


def test_retry_beyond_three_attempts_is_refused(context):
    parent = enqueue(context)
    assert jobs.claim(context.db, parent.id, "owner")
    jobs.finish_error(context.db, parent.id, "owner", "failed", "synthetic", code="collector_error")
    context.db.expire_all()
    parent = context.db.get(DBInspectionJob, parent.id)
    parent.attempt = jobs.MAX_ATTEMPTS
    context.db.commit()

    with pytest.raises(HTTPException) as refused:
        api_retry_inspection_job(parent.id, _request(7), db=context.db)

    assert refused.value.status_code == 409
    assert context.db.query(DBInspectionJob).count() == 1


def test_unclaimed_queue_entry_expires_and_is_retryable(context):
    stale = enqueue(context)
    running = enqueue(context)
    assert jobs.claim(context.db, running.id, "owner")
    later = stale.created_at + timedelta(seconds=stale.timeout_seconds + 1)

    assert jobs.reap_expired(context.db, now=later) == 1

    context.db.expire_all()
    expired = context.db.get(DBInspectionJob, stale.id)
    assert expired.status == "expired"
    assert expired.failure_reason
    detail = api_get_inspection_job(stale.id, _request(), db=context.db)
    assert detail["run"]["outcome"] == "expired"
    assert detail["run"]["outcome_kind"] == "expired_in_queue"
    assert detail["run"]["requires_attention"] is True
    assert detail["run"]["can_retry"] is True
    # A run that is genuinely executing is not affected by queue expiry.
    assert context.db.get(DBInspectionJob, running.id).status == "running"


def test_reapers_are_registered_as_scheduled_consumers(context, monkeypatch):
    """The functions existed and were called by nothing but tests - that is the whole defect."""
    from app.tasks import inspection_scheduler

    registered = master_scheduler.due_job_functions()
    assert inspection_scheduler.run_due_inspection_job_recovery in registered
    assert inspection_scheduler.run_due_inspection_job_expiry in registered
    names = {getattr(fn, "__name__", "") for fn in registered}
    assert {"run_due_inspection_job_recovery", "run_due_inspection_job_expiry"} <= names

    calls = []
    monkeypatch.setattr(jobs, "reap_interrupted", lambda db, now=None: calls.append("interrupted") or 0)
    monkeypatch.setattr(jobs, "reap_expired", lambda db, now=None, limit=200: calls.append("expired") or 0)
    monkeypatch.setattr(inspection_scheduler, "SessionLocal", lambda: _InertSession())
    inspection_scheduler.run_due_inspection_job_recovery()
    inspection_scheduler.run_due_inspection_job_expiry()
    assert calls == ["interrupted", "expired"]


class _InertSession:
    def rollback(self):
        pass

    def close(self):
        pass
