"""Scheduled inspections must run through the durable queue, not beside it.

Before this change `jobs.py` was fully implemented and fully tested but had zero
production callers: scheduled inspections called the runner directly, so cancel,
retry, leases and progress simply did not exist for them. These tests pin the
wiring down, and pin the step that is easiest to lose when moving a run onto a
new path - policy threshold binding happens *after* collection and changes the
findings themselves.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.analyzers.rules import Finding
from app.core.database import Base
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import jobs, runner
from app.tasks import inspection_scheduler


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _instance(db) -> DBInstance:
    instance = DBInstance(
        instance_name="queue-target", db_type="mysql", host="127.0.0.1", port=3306,
        username="root", password_cipher="unused", enabled=1,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _policy(db) -> DBInspectionPolicy:
    policy = DBInspectionPolicy(
        policy_key="queue-policy", name="队列模板", enabled=1, read_only=0,
        db_types_json=["mysql"],
        item_config_json=[{"key": "mysql.connection.used_pct", "enabled": True}],
        threshold_config_json={"connection_usage_pct": 80},
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def test_scheduled_run_terminalizes_a_job_row(sessions):
    """The scheduler must leave a terminal job row behind, not just a policy status."""
    leases: list[tuple] = []
    real_guard = jobs.lease_guard

    def spy_guard(*args, **kwargs):
        leases.append(args)
        return real_guard(*args, **kwargs)

    with sessions() as db:
        instance = _instance(db)
        policy = _policy(db)
        # Stub the collection pipeline so this test is about queue bookkeeping,
        # not about how a real collector happens to fail.
        with (
            patch.object(jobs, "lease_guard", side_effect=spy_guard),
            patch.object(runner, "prepare_findings", return_value=(instance, [])),
        ):
            status, _reason = inspection_scheduler._run_queued_inspection(db, instance, policy)

    assert leases, "the queued run never held a lease, so it could not have been claimed safely"

    # Empty findings are a genuine failure, not a crash: the queue still terminalizes.
    assert status == "failed", status
    with sessions() as db:
        rows = db.query(DBInspectionJob).all()
        assert len(rows) == 1, rows
        assert rows[0].status == "failed"
        assert rows[0].finished_at is not None
        assert rows[0].owner_token is None
        assert rows[0].stage == "completed"


def test_queue_path_applies_policy_thresholds(sessions):
    """Threshold binding must survive the move onto the queued path.

    `run_inspection` applied thresholds after collecting; a path that only reuses
    the collector would silently publish raw metrics with the collector's own
    verdict. The queue therefore calls `prepare_findings`, and this asserts the
    threshold actually rewrote the finding before it was saved.
    """
    raw = Finding(
        check_category="mysql", check_item="connection used pct", check_status="normal",
        metric_name="mysql.connection.used_pct", metric_value="95", threshold_value="80",
        finding="连接使用率 95%", suggestion="检查连接数",
    )
    with sessions() as db:
        instance = _instance(db)
        policy = _policy(db)
        with patch.object(runner, "collect_findings", return_value=[raw]):
            inspection_scheduler._run_queued_inspection(db, instance, policy)

    with sessions() as db:
        saved = db.query(DBCheckResult).all()
        assert saved, "the queued run persisted no check results"
        assert saved[0].check_status != "normal", (
            "value 95 over threshold 80 kept a normal verdict - the queued path "
            "collected findings but skipped policy threshold binding"
        )


def test_lease_guard_renews_while_work_runs(sessions):
    """A 45s lease must not expire under a longer inspection, or results are refused."""
    with patch.object(jobs, "heartbeat") as beat:
        with jobs.lease_guard("job-id", "token", interval=0.01):
            import time

            time.sleep(0.05)
    assert beat.called, "lease was never renewed during a run longer than the interval"
