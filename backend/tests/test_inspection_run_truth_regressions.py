"""Regressions QA found in the batch-1 read model, plus the "never converges" exit.

Three separate wrong answers, all of the same shape - a model stating something it has no
evidence for:

* a legacy result set had its display name replaced by a bare instance id;
* a zero-row run could still be scored 100/normal;
* a run whose executor ignored both the deadline and the cancel request stayed ``running`` for
  ever, because every recovery path required either an expired lease or a committed result.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers the whole model metadata
from app.analyzers.rules import Finding
from app.api.v1.inspection import api_get_inspection_job, api_list_inspection_jobs
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import jobs, result_writer, run_readback


@pytest.fixture
def context():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        instances = [
            DBInstance(
                instance_name=name, host=f"10.0.0.{index}", port=3306, db_type="mysql",
                username="monitor", password_cipher="env://TEST_DB_PASSWORD", enabled=1,
            )
            for index, name in ((1, "mysql-prod-101"), (2, "mysql-prod-102"))
        ]
        policy = DBInspectionPolicy(
            policy_key="qa-policy", name="QA policy", enabled=1, db_types_json=["mysql"],
            item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
        )
        db.add_all([*instances, policy])
        db.commit()
        yield SimpleNamespace(db=db, instances=instances, policy=policy, engine=engine)
    engine.dispose()


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(auth={"user_id": 7, "role": "admin", "source": "db"}), headers={},
    )


def list_jobs(context, **kwargs):
    params = {
        "instance_id": None, "db_type": None, "status": None, "outcome": None,
        "trigger_type": None, "include_legacy": False, "page": 1, "page_size": 50,
    }
    params.update(kwargs)
    return api_list_inspection_jobs(_request(), db=context.db, **params)


def _legacy_row(context, task_id: str, instance) -> DBCheckResult:
    """A result set written before target snapshots existed: no `inspection_target_snapshot`."""
    return DBCheckResult(
        task_id=task_id, instance_id=instance.id, db_type="mysql",
        check_category="overview", check_item="legacy", check_status="normal",
        raw_data={},
    )


# ---------------------------------------------------------------- FIND-QA-02


def test_legacy_result_set_falls_back_to_the_instance_name(context):
    row = _legacy_row(context, "20261019120000000001", context.instances[0])
    context.db.add(row)
    context.db.commit()

    summary = result_writer.list_tasks(context.db)[0]

    assert summary["instance_name"] == "mysql-prod-101"
    # The evidence is still honestly missing: a current name is not proof of what ran.
    assert summary["target_snapshot_status"] == "historical_context_missing"


def test_legacy_name_fallback_is_one_query_for_the_whole_page(context):
    for index, legacy_instance in enumerate(context.instances + context.instances):
        context.db.add(_legacy_row(context, f"2026101912000000000{index}", legacy_instance))
    context.db.commit()

    statements: list[str] = []
    event.listen(
        context.engine, "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )

    summaries = result_writer.list_tasks(context.db)

    instance_queries = [sql for sql in statements if "FROM db_instance" in sql]
    assert len(summaries) == 4
    assert len(instance_queries) == 1, instance_queries
    assert " IN (" in instance_queries[0].upper()
    assert {summary["instance_name"] for summary in summaries} == {
        "mysql-prod-101", "mysql-prod-102",
    }


def test_recorded_snapshot_wins_and_needs_no_instance_query(context):
    context.db.add_all([
        _legacy_row(context, "20261019120000000011", context.instances[0]),
        DBCheckResult(
            task_id="20261019120000000012", instance_id=context.instances[1].id,
            db_type="mysql", check_category="overview", check_item="new", check_status="normal",
            raw_data={jobs.TARGET_SNAPSHOT_KEY: {
                "id": context.instances[1].id, "name": "recorded-name", "host": "10.0.0.2",
                "port": 3306, "db_type": "mysql", "status": "recorded",
            }},
        ),
    ])
    context.db.commit()

    statements: list[str] = []
    event.listen(
        context.engine, "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )

    by_task = {item["task_id"]: item for item in result_writer.list_tasks(context.db)}

    assert by_task["20261019120000000012"]["instance_name"] == "recorded-name"
    assert by_task["20261019120000000012"]["target_snapshot_status"] == "recorded"
    assert by_task["20261019120000000011"]["instance_name"] == "mysql-prod-101"
    # Only the row without a snapshot triggers the batched fallback.
    assert len([sql for sql in statements if "FROM db_instance" in sql]) == 1


def test_legacy_summary_and_task_list_agree_on_the_name(context):
    context.db.add(_legacy_row(context, "20261019120000000021", context.instances[0]))
    context.db.commit()

    source = next(
        item for item in run_readback.run_sources(context.db, 10)
        if item["job_id"] == "20261019120000000021"
    )
    assert source["instance_name"] == "mysql-prod-101"
    assert source["linkage"] == "legacy_result_only"


# ---------------------------------------------------------------- FIND-QA-03


def _job(context, *, status: str, stage: str, started_at=None) -> DBInspectionJob:
    now = datetime(2026, 10, 20, 10, 0, 0)
    row = DBInspectionJob(
        id=f"job-{status}-{stage}-{context.db.query(DBInspectionJob).count()}",
        instance_id=context.instances[0].id, db_type="mysql", requested_by=None,
        trigger_type="manual", policy_id=context.policy.id,
        policy_snapshot_json={"policy_key": "qa-policy", "name": "QA policy"},
        policy_revision="0" * 64, target_fingerprint="0" * 64, attempt=1,
        status=status, stage=stage, progress_json=[], timeout_seconds=60,
        cancel_requested=0, created_at=now, started_at=started_at,
    )
    context.db.add(row)
    context.db.commit()
    return row


def test_zero_row_success_is_not_scored_as_healthy(context):
    """`success` with no rows used to score 100/normal while saying results_published=False."""
    row = _job(context, status="success", stage="completed")

    items = list_jobs(context, outcome="success")["items"]
    item = next(entry for entry in items if entry["job_id"] == row.id)
    detail = api_get_inspection_job(row.id, _request(), db=context.db)

    assert item["result_count"] == detail["results"]["total_count"] == 0
    assert item["results_published"] is False
    # The score/risk pair only exists on the item (and in the report, which is absent here):
    # no surface may turn "0 rows" into 100/normal.
    assert item["score"] is None
    assert item["risk_level"] is None
    assert "score" not in detail["run"] and "risk_level" not in detail["run"]
    assert detail["report"] is None
    assert detail["evidence"]["state"] == "missing"


def test_score_and_risk_stay_none_when_the_caller_has_no_counts():
    counts = {"critical": 0, "warning": 0, "info": 0, "normal": 0}
    assert result_writer.score_for_counts(counts, task_status="success", result_count=0) is None
    assert result_writer.risk_level_for(counts, None, result_count=0) is None
    assert result_writer.score_for_counts(
        {"critical": 0, "warning": 0, "info": 0, "normal": 3},
        task_status="success", result_count=3,
    ) == 100
    assert result_writer.risk_level_for(counts, None, result_count=3) == "warning"


# ---------------------------------------------------------------- FIND-QA-05


def _claimed_run(context, *, timeout_seconds: int = 60, now=None) -> DBInspectionJob:
    now = now or jobs.utcnow()
    row = jobs.enqueue(
        context.db, context.instances[0], "mysql", context.policy,
        timeout_seconds=timeout_seconds,
    )
    assert jobs.claim(context.db, row.id, "owner", now=now)
    context.db.expire_all()
    return context.db.get(DBInspectionJob, row.id)


def _finding() -> Finding:
    return Finding(
        "synthetic", "check", "normal", "one", "1", "", "ok", "",
        {"execution_status": "success", "sql": "SELECT 1", "rows": [{"one": 1}]},
    )


def test_job_running_past_its_deadline_is_terminated_and_cannot_publish(context):
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=60, now=now)
    later = now + timedelta(seconds=61)

    assert jobs.reap_overdue(context.db, later) == 1

    context.db.expire_all()
    reaped = context.db.get(DBInspectionJob, row.id)
    assert reaped.status == "timed_out"
    assert reaped.owner_token is None and reaped.lease_until is None
    assert "需人工核对" in reaped.failure_reason
    detail = api_get_inspection_job(row.id, _request(), db=context.db)
    # We know the deadline passed, not what the target did.
    assert detail["run"]["outcome"] == "unknown"
    assert detail["run"]["outcome_kind"] == "timeout"
    assert detail["run"]["requires_attention"] is True
    # And the late executor can no longer publish results for it.
    assert jobs.commit_results(context.db, row.id, "owner", [_finding()]) is False
    assert context.db.query(DBCheckResult).count() == 0


def test_cancel_grace_expiry_terminates_as_cancelled_not_interrupted(context):
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=3600, now=now)
    jobs.cancel(context.db, row, requested_by=7, now=now)
    context.db.expire_all()

    # Inside the grace period: the executor still has a chance to acknowledge.
    assert jobs.reap_overdue(context.db, now + timedelta(seconds=jobs.CANCEL_GRACE_SECONDS)) == 0
    context.db.expire_all()
    assert context.db.get(DBInspectionJob, row.id).status == "running"

    later = now + timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1)
    assert jobs.reap_overdue(context.db, later) == 1

    context.db.expire_all()
    reaped = context.db.get(DBInspectionJob, row.id)
    assert reaped.status == "cancelled"
    assert "强制终结" in reaped.failure_reason
    detail = api_get_inspection_job(row.id, _request(), db=context.db)
    assert detail["run"]["outcome"] == "cancelled"
    assert detail["run"]["outcome_kind"] == "cancelled_force_closed"
    # The target may still be working, so this is not a clean "cancelled before it ran".
    assert detail["run"]["requires_attention"] is True
    assert detail["run"]["can_retry"] is True
    assert detail["notice"]


def test_runs_inside_their_limits_are_not_reaped(context):
    now = jobs.utcnow()
    fresh = _claimed_run(context, timeout_seconds=900, now=now)
    cancelled_recently = _claimed_run(context, timeout_seconds=3600, now=now)
    jobs.cancel(context.db, cancelled_recently, requested_by=7, now=now)

    assert jobs.reap_overdue(context.db, now + timedelta(seconds=10)) == 0

    context.db.expire_all()
    assert context.db.get(DBInspectionJob, fresh.id).status == "running"
    assert context.db.get(DBInspectionJob, cancelled_recently.id).status == "running"


def test_overdue_reaper_is_a_scheduled_consumer(context, monkeypatch):
    from app.tasks import inspection_scheduler

    registered = master_scheduler_jobs()
    assert inspection_scheduler.run_due_inspection_job_overdue in registered
    assert "run_due_inspection_job_overdue" in {fn.__name__ for fn in registered}

    calls = []
    monkeypatch.setattr(jobs, "reap_overdue", lambda db, now=None, **kwargs: calls.append("overdue") or 0)
    monkeypatch.setattr(inspection_scheduler, "SessionLocal", lambda: _InertSession())
    inspection_scheduler.run_due_inspection_job_overdue()
    assert calls == ["overdue"]


def master_scheduler_jobs():
    from app.tasks import master_scheduler

    return master_scheduler.due_job_functions()


class _InertSession:
    def rollback(self):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------- FIND-QA2-07
#
# The three reapers are scheduled recovery -> expiry -> overdue, so `reap_interrupted` is the
# one that sees a run whose executor died *inside* the cancel grace window. It used to write
# "interrupted / lease_lost" and move on; `reap_overdue` then found zero running rows, so the
# cancel left no trace anywhere. The operator pressed cancel, read "heartbeat lost", and had no
# way to know whether the cancel had taken effect.


def _claimed_then_cancelled(context, *, timeout_seconds: int = 3600, now=None) -> DBInspectionJob:
    now = now or jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=timeout_seconds, now=now)
    jobs.cancel(context.db, row, requested_by=7, now=now)
    context.db.expire_all()
    return context.db.get(DBInspectionJob, row.id)


def test_a_cancel_pending_when_the_lease_died_keeps_the_cancel_story(context):
    now = jobs.utcnow()
    row = _claimed_then_cancelled(context, now=now)

    # The lease (45s) and the cancel grace (60s) both expire before this tick.
    later = now + timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1)
    assert jobs.reap_interrupted(context.db, later) == 1
    # Recovery took the row, so overdue has nothing left to close. This is precisely why the
    # cancel has to be recorded by recovery and not deferred to overdue.
    assert jobs.reap_overdue(context.db, later) == 0

    context.db.expire_all()
    reaped = context.db.get(DBInspectionJob, row.id)
    # The executor died. That is the stored fact and it must NOT be rewritten as "cancelled".
    assert reaped.status == "interrupted"
    # Both truths have to be readable in the stored reason.
    assert "心跳中断" in reaped.failure_reason
    assert "取消" in reaped.failure_reason

    detail = api_get_inspection_job(row.id, _request(), db=context.db)
    assert detail["run"]["outcome"] == "unknown"  # not "cancelled"
    assert detail["run"]["outcome_kind"] == "cancelled_force_closed"
    assert detail["run"]["requires_attention"] is True
    # And the operator-facing notice mentions the cancel *and* the dead node.
    assert "取消" in detail["notice"]
    assert "心跳中断" in detail["notice"]


def test_a_plain_heartbeat_loss_still_reports_lease_lost(context):
    """The cancel branch must not leak into runs nobody cancelled."""
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=3600, now=now)

    assert jobs.reap_interrupted(context.db, now + timedelta(seconds=jobs.LEASE_SECONDS + 1)) == 1

    context.db.expire_all()
    reaped = context.db.get(DBInspectionJob, row.id)
    assert reaped.status == "interrupted"
    assert reaped.failure_reason == jobs.LEASE_LOST_REASON
    assert "取消" not in reaped.failure_reason
    detail = api_get_inspection_job(row.id, _request(), db=context.db)
    assert detail["run"]["outcome_kind"] == "lease_lost"


# ---------------------------------------------------------------- FIND-QA2-08
#
# `float(row.timeout_seconds or 0)` turned a missing deadline into "already overdue": the next
# tick killed a run that never had a limit. `0` is reachable today (legacy/imported rows); `None`
# is what a nullable column would hand us, so the helper is covered directly for both.


def test_timeout_helper_reads_zero_and_null_as_no_deadline():
    assert jobs._timeout_seconds(0) is None
    assert jobs._timeout_seconds(None) is None
    assert jobs._timeout_seconds(-30) is None
    assert jobs._timeout_seconds("not-a-number") is None
    assert jobs._timeout_seconds(900) == 900
    assert jobs._timeout_seconds("60") == 60


def test_a_running_job_with_a_zero_timeout_is_not_timed_out_by_arithmetic(context):
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=900, now=now)
    # A row that predates the column, or arrived through an import: `0` rather than 900.
    context.db.query(DBInspectionJob).filter_by(id=row.id).update(
        {"timeout_seconds": 0}, synchronize_session=False,
    )
    context.db.commit()
    context.db.expire_all()

    assert jobs.reap_overdue(context.db, now + timedelta(seconds=1)) == 0

    context.db.expire_all()
    assert context.db.get(DBInspectionJob, row.id).status == "running"


def test_a_queued_job_with_a_zero_timeout_is_not_expired_by_arithmetic(context):
    now = jobs.utcnow()
    row = jobs.enqueue(
        context.db, context.instances[0], "mysql", context.policy, timeout_seconds=900,
    )
    context.db.query(DBInspectionJob).filter_by(id=row.id).update(
        {"timeout_seconds": 0}, synchronize_session=False,
    )
    context.db.commit()
    context.db.expire_all()

    assert jobs.reap_expired(context.db, now=now + timedelta(seconds=1)) == 0

    context.db.expire_all()
    assert context.db.get(DBInspectionJob, row.id).status == "queued"


def test_a_real_deadline_still_fires(context):
    """The guard above must not disarm the reaper for jobs that do have a deadline."""
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=60, now=now)
    assert jobs.reap_overdue(context.db, now + timedelta(seconds=61)) == 1
    context.db.expire_all()
    assert context.db.get(DBInspectionJob, row.id).status == "timed_out"


# ---------------------------------------------------------------- FIND-QA2-09
#
# `status=running` with `started_at IS NULL` and a live lease was reachable by neither reaper:
# recovery wants an expired lease, overdue used to require `started_at`. Such a row stayed
# running for ever.


def test_a_running_job_with_no_started_at_is_still_reachable_by_the_deadline(context):
    now = jobs.utcnow()
    row = _claimed_run(context, timeout_seconds=60, now=now)
    context.db.query(DBInspectionJob).filter_by(id=row.id).update(
        {"started_at": None}, synchronize_session=False,
    )
    context.db.commit()
    context.db.expire_all()

    # The lease is still valid, so recovery correctly leaves it alone.
    assert jobs.reap_interrupted(context.db, now + timedelta(seconds=10)) == 0
    context.db.expire_all()
    assert context.db.get(DBInspectionJob, row.id).status == "running"

    # The deadline is measured from the enqueue time instead of from nowhere.
    assert jobs.reap_overdue(context.db, now + timedelta(seconds=61)) == 1

    context.db.expire_all()
    reaped = context.db.get(DBInspectionJob, row.id)
    assert reaped.status == "timed_out"
    assert "需人工核对" in reaped.failure_reason


def test_oldest_effective_deadline_is_reaped_first(context):
    """Ordering must not depend on how a dialect sorts NULLs.

    ``enqueue`` does not take a clock, so the enqueue times are pinned by hand; the point is
    that a NULL ``started_at`` row is ordered by its ``created_at`` rather than sorted to the
    front (SQLite/MySQL) or the back (Oracle), which would let ``limit`` starve it.
    """
    now = jobs.utcnow()
    oldest = _claimed_run(context, timeout_seconds=60, now=now - timedelta(seconds=600))
    context.db.query(DBInspectionJob).filter_by(id=oldest.id).update(
        {"started_at": None, "created_at": now - timedelta(seconds=600)},
        synchronize_session=False,
    )
    newest = _claimed_run(context, timeout_seconds=60, now=now)
    context.db.query(DBInspectionJob).filter_by(id=newest.id).update(
        {"created_at": now}, synchronize_session=False,
    )
    context.db.commit()
    context.db.expire_all()

    assert jobs.reap_overdue(context.db, now + timedelta(seconds=61), limit=1) == 1

    context.db.expire_all()
    assert context.db.get(DBInspectionJob, oldest.id).status == "timed_out"
    assert context.db.get(DBInspectionJob, newest.id).status == "running"
