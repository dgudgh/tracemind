from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.alert_event import DBAlertActionLog, DBAlertEvent
from app.services.alert_lifecycle import retry_due_actions


@pytest.fixture
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    yield sessions
    engine.dispose()


def _add_action(db, *, status, next_retry, attempts=1, max_retries=3, dead_letter=None, action_type="notify"):
    row = DBAlertActionLog(
        event_id=1,
        action_type=action_type,
        action_status=status,
        attempt_count=attempts,
        max_retries=max_retries,
        next_retry_time=next_retry,
        dead_letter_at=dead_letter,
        operator="operator",
    )
    db.add(row)
    db.commit()
    return row.id


def test_due_failed_action_is_rearmed_to_pending(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        action_id = _add_action(db, status="failed", next_retry=now - timedelta(seconds=1))
        db.commit()

        result = retry_due_actions(db, now=now)

        assert result["rearmed"] == 1
        assert result["retried"] == [action_id]
        row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
        assert row.action_status == "pending"
        assert row.next_retry_time is None


def test_rearm_is_idempotent(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        _add_action(db, status="failed", next_retry=now - timedelta(seconds=1))
        db.commit()

        assert retry_due_actions(db, now=now)["rearmed"] == 1
        second = retry_due_actions(db, now=now)

        assert second["rearmed"] == 0
        assert second["retried"] == []


def test_not_yet_due_action_is_untouched(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        action_id = _add_action(db, status="failed", next_retry=now + timedelta(seconds=30))
        db.commit()

        assert retry_due_actions(db, now=now)["rearmed"] == 0

        row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
        assert row.action_status == "failed"
        assert row.next_retry_time == now + timedelta(seconds=30)


def test_action_without_due_stamp_is_untouched(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        action_id = _add_action(db, status="failed", next_retry=None)
        db.commit()

        assert retry_due_actions(db, now=now)["rearmed"] == 0
        row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
        assert row.action_status == "failed"


def test_dead_letter_action_needs_operator_not_another_auto_attempt(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        action_id = _add_action(
            db,
            status="failed",
            next_retry=now - timedelta(seconds=1),
            attempts=3,
            max_retries=3,
            dead_letter=now - timedelta(minutes=5),
        )
        db.commit()

        assert retry_due_actions(db, now=now)["rearmed"] == 0
        row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
        assert row.action_status == "failed"


def test_budget_exhausted_action_is_not_rearmed(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        action_id = _add_action(db, status="failed", next_retry=now - timedelta(seconds=1), attempts=5, max_retries=3)
        db.commit()

        assert retry_due_actions(db, now=now)["rearmed"] == 0
        row = db.query(DBAlertActionLog).filter(DBAlertActionLog.id == action_id).first()
        assert row.action_status == "failed"


def test_batch_limit_caps_rearmed_rows(db_factory):
    now = datetime(2026, 9, 22, 12, 0, 0)
    with db_factory() as db:
        db.add(DBAlertEvent(id=1, event_uid="e1", source="manual", title="t", instance_id=101))
        for index in range(5):
            _add_action(db, status="failed", next_retry=now - timedelta(seconds=index + 1))
        db.commit()

        result = retry_due_actions(db, now=now, limit=2)

        assert result["rearmed"] == 2
        assert db.query(DBAlertActionLog).filter(DBAlertActionLog.action_status == "failed").count() == 3


def test_consumer_is_wired_into_master_scheduler():
    """Asserts real membership, not a mention in the source.

    Grepping the source would pass on a commented-out entry or a docstring - both
    of which mean exactly the unwired state this consumer was added to remove.
    """
    from app.tasks.alert_action_scheduler import run_due_alert_action_jobs
    from app.tasks.master_scheduler import due_job_functions

    jobs = due_job_functions()

    assert run_due_alert_action_jobs in jobs, (
        "retry_due_actions exists but no scheduler runs it - that is the "
        "'written but never consumed' gap this consumer was added to close"
    )
    assert all(callable(job) for job in jobs)
