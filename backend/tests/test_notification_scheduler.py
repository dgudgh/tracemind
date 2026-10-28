import threading
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.notify import service
from app.notify.api import router
from app.notify.adapters.base import NotifyResult
from app.notify.models import NotifyChannel, NotifyLog, NotifyPolicy, NotifyPolicyChannel, NotifyRecipient
from app.notify.schemas import EventCreate
from app.tasks import notification_scheduler


@pytest.fixture
def store():
    # Shared in-memory SQLite with independent connections for concurrency tests.
    engine = create_engine(f"sqlite:///file:notify-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true",
                           connect_args={"check_same_thread": False, "timeout": 5})
    keeper = engine.connect()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        channel = NotifyChannel(channel_name="fake", channel_type="webhook", config_json="{}")
        db.add(channel)
        db.commit()
        channel_id = channel.id
    yield factory, channel_id
    keeper.close()
    engine.dispose()


def failed(db, channel_id, **values):
    payload = dict(channel_id=channel_id, channel_type="webhook", recipient="", status="failed", attempt_count=1)
    payload.update(values)
    log = NotifyLog(**payload)
    db.add(log)
    db.commit()
    return log


def test_initial_adapter_setup_failure_is_retried_with_backoff_and_cap(store):
    factory, channel_id = store
    now = datetime.now()
    with factory() as db, patch.object(service, "get_adapter", side_effect=ValueError("invalid config")) as adapter:
        policy = NotifyPolicy(policy_name="all", throttle_minutes=0)
        db.add(policy)
        db.commit()
        db.add(NotifyPolicyChannel(policy_id=policy.id, channel_id=channel_id))
        db.commit()
        event = service.create_event(db, EventCreate(event_type="alert", level="info", title="failure"))
        result = service.dispatch_event(db, event.id)
        assert result["sent_count"] == 0 and result["attempted_count"] == 1
        assert result["failed_count"] == 1 and result["pending_count"] == 0
        log = db.query(NotifyLog).one()
        assert log.status == "failed" and log.attempt_count == 1
        assert log.next_retry_time >= now + timedelta(minutes=5)
        first_due = log.next_retry_time
        repeated = service.dispatch_event(db, event.id)
        assert repeated["sent_count"] == repeated["attempted_count"] == repeated["failed_count"] == 0
        assert service.consume_due_notifications(db, now=first_due - timedelta(seconds=1))["processed"] == 0
        assert service.consume_due_notifications(db, now=first_due)["processed"] == 1
        db.refresh(log)
        assert log.attempt_count == 2 and log.retry_count == 1
        assert log.next_retry_time == first_due + timedelta(minutes=10)
        assert service.consume_due_notifications(db, now=log.next_retry_time)["processed"] == 1
        db.refresh(log)
        assert log.attempt_count == 3 and log.retry_count == 2 and log.next_retry_time is None
        assert service.consume_due_notifications(db, now=first_due + timedelta(days=1))["processed"] == 0
        assert adapter.call_count == 3
        with pytest.raises(ValueError, match="上限"):
            service.retry_log(db, log.id)


def test_batch_is_bounded_and_success_is_not_repeated(store):
    factory, channel_id = store
    with factory() as db, patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.return_value = NotifyResult(True, 200, "ok")
        for _ in range(103):
            failed(db, channel_id)
        assert service.consume_due_notifications(db, limit=0)["processed"] == 0
        assert service.consume_due_notifications(db, limit=10000)["processed"] == 100
        assert service.consume_due_notifications(db)["processed"] == 3
        assert service.consume_due_notifications(db)["processed"] == 0
        assert adapter.return_value.send.call_count == 103


@pytest.mark.parametrize("change", ["disabled", "deleted", "type"])
def test_channel_boundary_checked_before_decoding_config(store, change):
    factory, channel_id = store
    with factory() as db, patch.object(service, "decode_channel_config") as decode:
        log = failed(db, channel_id)
        channel = db.get(NotifyChannel, channel_id)
        if change == "disabled":
            channel.enabled = 0
        elif change == "deleted":
            db.delete(channel)
        else:
            channel.channel_type = "smtp"
        db.commit()
        assert service.consume_due_notifications(db)["processed"] == 1
        db.refresh(log)
        assert log.status == "cancelled" and log.next_retry_time is None
        decode.assert_not_called()


def test_expired_inflight_outcome_is_unknown_and_never_automatically_resent(store):
    factory, channel_id = store
    now = datetime.now()
    with factory() as db, patch.object(service, "get_adapter") as adapter:
        expired = failed(db, channel_id, status="retrying", next_retry_time=now - timedelta(seconds=1))
        active = failed(db, channel_id, status="retrying", next_retry_time=now + timedelta(minutes=1))
        assert service.consume_due_notifications(db, now=now)["processed"] == 1
        db.refresh(expired)
        db.refresh(active)
        assert expired.status == "unknown" and expired.next_retry_time is None
        assert active.status == "retrying"
        assert service.consume_due_notifications(db, now=now)["processed"] == 0
        adapter.assert_not_called()


def test_consumer_and_manual_retry_cannot_send_same_log_concurrently(store):
    factory, channel_id = store
    with factory() as db:
        log_id = failed(db, channel_id).id
    entered, release = threading.Event(), threading.Event()
    errors = []

    def send(*_):
        entered.set()
        assert release.wait(5)
        return NotifyResult(True, 200, "ok")

    def consume():
        try:
            with factory() as db:
                service.consume_due_notifications(db)
        except Exception as exc:
            errors.append(exc)

    with patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.side_effect = send
        thread = threading.Thread(target=consume)
        thread.start()
        try:
            assert entered.wait(5)
            with factory() as db:
                assert service.consume_due_notifications(db)["processed"] == 0
                with pytest.raises(ValueError, match="Only failed"):
                    service.retry_log(db, log_id)
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not errors
        assert adapter.return_value.send.call_count == 1
    with factory() as db:
        log = db.get(NotifyLog, log_id)
        assert log.status == "sent" and log.attempt_count == 2 and log.retry_count == 1


def test_scheduler_wrapper_uses_consumer_and_closes_session(store):
    factory, channel_id = store
    with factory() as db:
        failed(db, channel_id)
    with patch.object(notification_scheduler, "SessionLocal", factory), patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.return_value = NotifyResult(True, 200, "ok")
        assert notification_scheduler.run_due_notification_jobs() == {"processed": 1}
        assert notification_scheduler.run_due_notification_jobs() == {"processed": 0}
        adapter.return_value.send.assert_called_once()


def test_retry_obeys_per_log_lower_cap(store):
    factory, channel_id = store
    with factory() as db, patch.object(service, "get_adapter") as adapter:
        log = failed(db, channel_id, max_retries=1)
        assert service.consume_due_notifications(db)["processed"] == 0
        with pytest.raises(ValueError, match="上限"):
            service.retry_log(db, log.id)
        adapter.assert_not_called()


@pytest.mark.parametrize("change", ["policy", "link", "recipient"])
def test_ordinary_policy_retry_respects_revoked_audience(store, change):
    factory, channel_id = store
    with factory() as db, patch.object(service, "get_adapter") as adapter:
        policy = NotifyPolicy(policy_name="policy", throttle_minutes=0)
        recipient = NotifyRecipient(name="person", channel_type="webhook", address="person")
        db.add_all([policy, recipient])
        db.commit()
        link = NotifyPolicyChannel(policy_id=policy.id, channel_id=channel_id)
        db.add(link)
        db.commit()
        event = service.create_event(db, EventCreate(event_type="alert", level="critical", title="alert"))
        adapter.return_value.send.return_value = NotifyResult(False, 503, "fail")
        service.dispatch_event(db, event.id)
        log = db.query(NotifyLog).one()
        if change == "policy":
            policy.enabled = 0
        elif change == "link":
            db.delete(link)
        else:
            db.delete(recipient)
        db.commit()
        adapter.return_value.send.reset_mock()
        service.retry_log(db, log.id)
        assert log.status == "cancelled"
        adapter.return_value.send.assert_not_called()


def test_claim_sql_preserves_old_attempt_count_for_mysql_and_postgresql(store):
    factory, channel_id = store
    statements = []
    with factory() as db, patch.object(service, "get_adapter") as adapter:
        log = failed(db, channel_id, status="pending", attempt_count=0)

        def capture(_conn, clause, *_):
            if getattr(clause, "is_update", False) and clause._ordered_values:
                statements.extend(str(clause.compile(dialect=dialect)) for dialect in (mysql.dialect(), postgresql.dialect()))

        event.listen(db.bind, "before_execute", capture)
        try:
            adapter.return_value.send.return_value = NotifyResult(True, 200, "ok")
            assert service.consume_due_notifications(db)["processed"] == 1
        finally:
            event.remove(db.bind, "before_execute", capture)
        db.refresh(log)
        assert log.attempt_count == 1 and log.retry_count == 0
        assert len(statements) == 2
        assert all(sql.index("SET retry_count=") < sql.index(", attempt_count=") for sql in statements)


def test_stale_due_snapshot_cannot_repeat_an_already_failed_attempt(store):
    factory, channel_id = store
    with factory() as db:
        failed(db, channel_id)
    barrier, finished = threading.Barrier(2), threading.Event()
    lock = threading.Lock()
    entrants, results, errors = [], [], []
    deliver = service._deliver_log

    def controlled(db, log_id, **kwargs):
        with lock:
            entrants.append(log_id)
            first = len(entrants) == 1
        barrier.wait(5)
        if first:
            try:
                return deliver(db, log_id, **kwargs)
            finally:
                finished.set()
        assert finished.wait(5)
        return deliver(db, log_id, **kwargs)

    def consume():
        try:
            with factory() as db:
                results.append(service.consume_due_notifications(db))
        except Exception as exc:
            errors.append(exc)

    with patch.object(service, "_deliver_log", side_effect=controlled), patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.return_value = NotifyResult(False, 503, "unavailable")
        threads = [threading.Thread(target=consume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        assert sorted(result["processed"] for result in results) == [0, 1]
        adapter.return_value.send.assert_called_once()
    with factory() as db:
        log = db.query(NotifyLog).one()
        assert log.attempt_count == 2 and log.next_retry_time is not None


def test_locked_shared_memory_snapshot_retries_without_losing_delivery(store):
    factory, channel_id = store
    with factory() as db:
        log_id = failed(db, channel_id).id
    locked_error = threading.Event()
    results, errors = [], []

    def observe_error(context):
        if context.statement.startswith("SELECT") and "notify_log" in context.statement:
            locked_error.set()

    def consume():
        try:
            with factory() as db:
                results.append(service.consume_due_notifications(db))
        except Exception as exc:
            errors.append(exc)

    with factory() as locker, patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.return_value = NotifyResult(True, 200, "ok")
        locker.query(NotifyLog).filter(NotifyLog.id == log_id).update({NotifyLog.title: "locked"})
        event.listen(locker.bind, "handle_error", observe_error)
        thread = threading.Thread(target=consume)
        thread.start()
        try:
            assert locked_error.wait(5)
        finally:
            locker.commit()
            thread.join(5)
            event.remove(locker.bind, "handle_error", observe_error)
        assert not thread.is_alive()
        assert not errors
        assert results == [{"processed": 1}]
        adapter.return_value.send.assert_called_once()


def test_dispatch_http_counts_only_successful_deliveries(store):
    factory, channel_id = store
    with factory() as db:
        policy = NotifyPolicy(policy_name="mixed", throttle_minutes=0)
        db.add_all([policy, NotifyRecipient(name="ok", channel_type="webhook", address="ok"),
                    NotifyRecipient(name="fail", channel_type="webhook", address="fail")])
        db.commit()
        db.add(NotifyPolicyChannel(policy_id=policy.id, channel_id=channel_id))
        db.commit()
        notification = service.create_event(db, EventCreate(event_type="alert", level="info", title="mixed"))
        event_id = notification.id

    def override_db():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client, patch.object(service, "get_adapter") as adapter:
        adapter.return_value.send.side_effect = lambda recipient, *_: NotifyResult(recipient == "ok", 200, "fake")
        response = client.post(f"/notify/events/{event_id}/dispatch")
        assert response.status_code == 200
        assert response.json() == {"event_id": event_id, "sent_count": 1, "attempted_count": 2,
                                   "pending_count": 0, "failed_count": 1, "cancelled_count": 0, "unknown_count": 0}
        repeated = client.post(f"/notify/events/{event_id}/dispatch").json()
        assert {key: value for key, value in repeated.items() if key != "event_id"} == {
            "sent_count": 0, "attempted_count": 0, "pending_count": 0,
            "failed_count": 0, "cancelled_count": 0, "unknown_count": 0,
        }
        assert adapter.return_value.send.call_count == 2
