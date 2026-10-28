import json
import threading
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.report_center import ReportArtifact, ReportSchedule, ReportSubscription
from app.notify import service
from app.notify.adapters.base import NotifyResult
from app.notify.models import NotifyChannel, NotifyEvent, NotifyLog, NotifyPolicy, NotifyPolicyChannel, NotifyRecipient
from app.notify.schemas import EventCreate
from app.services import report_center


@pytest.fixture
def delivery():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as db:
        smtp = NotifyChannel(channel_name="mail", channel_type="smtp", config_json="{}")
        sms = NotifyChannel(channel_name="phone", channel_type="sms", config_json="{}")
        other = NotifyChannel(channel_name="other-mail", channel_type="smtp", config_json="{}")
        disabled = NotifyChannel(channel_name="disabled", channel_type="smtp", config_json="{}", enabled=0)
        policy = NotifyPolicy(policy_name="global", throttle_minutes=60)
        db.add_all([smtp, sms, other, disabled, policy,
                    NotifyRecipient(name="global", channel_type="smtp", address="global@example.invalid")])
        db.commit()
        db.add(NotifyPolicyChannel(policy_id=policy.id, channel_id=other.id))
        db.commit()
        schedule = report_center.create_schedule(db, {
            "name": "daily", "report_type": "inspection", "schedule_type": "daily", "run_time": "08:00",
        }, actor="tester")
        calls = []

        def send(recipient, title, body):
            calls.append((recipient, title, body))
            return NotifyResult(True, 200, "ok")

        with patch.object(service, "get_adapter") as adapter:
            adapter.return_value.send.side_effect = send
            yield db, schedule, (smtp, sms, other, disabled), adapter, calls
    engine.dispose()


def subscribe(db, schedule, *, channels, recipients, **extra):
    return report_center.create_subscription(db, {
        "name": "subscription", "schedule_id": schedule.id,
        "channels": channels, "recipients": recipients, **extra,
    }, actor="tester")


def generate(db, schedule):
    run = report_center.execute_schedule(db, schedule, planned_at=datetime(2026, 9, 8),
                                         source_loader=lambda *_: [])
    assert run.status == "success"
    return db.get(ReportArtifact, run.artifact_id)


def test_subscriptions_do_not_cross_send_or_lose_targets_on_repeat(delivery):
    db, schedule, (smtp, sms, other, disabled), adapter, calls = delivery
    first = subscribe(db, schedule, channels=[smtp.id, str(smtp.id), sms.channel_name, disabled.id],
                      recipients=["first@example.invalid", "+8613800000000", "first@example.invalid"])
    second = subscribe(db, schedule, channels=[smtp.id], recipients=["second@example.invalid"])
    artifact = generate(db, schedule)
    assert sorted(call[0] for call in calls) == ["+8613800000000", "first@example.invalid", "second@example.invalid"]
    logs = db.query(NotifyLog).all()
    assert {(row.policy_id, row.channel_id, row.recipient) for row in logs} == {
        (-first.id, smtp.id, "first@example.invalid"), (-first.id, sms.id, "+8613800000000"),
        (-second.id, smtp.id, "second@example.invalid"),
    }
    assert all(row.status == "sent" for row in logs)
    report_center._dispatch_subscriptions(db, schedule, artifact)
    service.consume_due_notifications(db, now=datetime.now() + timedelta(days=1))
    assert len(calls) == 3
    assert db.query(NotifyLog).count() == 3
    assert db.query(NotifyEvent).count() == 2


def test_alias_and_empty_channel_policy_fallback_keep_explicit_audience(delivery):
    db, schedule, (smtp, sms, other, disabled), adapter, calls = delivery
    by_type = subscribe(db, schedule, channels=["email"], recipients=["type@example.invalid"])
    fallback = subscribe(db, schedule, channels=[], recipients=["fallback@example.invalid"])
    generate(db, schedule)
    assert {(row.policy_id, row.channel_id, row.recipient) for row in db.query(NotifyLog)} == {
        (-by_type.id, smtp.id, "type@example.invalid"), (-by_type.id, other.id, "type@example.invalid"),
        (-fallback.id, other.id, "fallback@example.invalid"),
    }
    assert all(call[0] != "global@example.invalid" for call in calls)


def test_empty_recipients_never_fall_back_to_global_mail(delivery):
    db, schedule, (smtp, *_), adapter, calls = delivery
    subscribe(db, schedule, channels=[smtp.id], recipients=[])
    subscribe(db, schedule, channels=[], recipients=[])
    generate(db, schedule)
    assert not calls
    assert db.query(NotifyLog).count() == 0


def test_arbitrary_extra_json_does_not_authorize_routing(delivery):
    db, schedule, (smtp, sms, other, _), adapter, calls = delivery
    subscription = subscribe(db, schedule, channels=[smtp.id], recipients=["private@example.invalid"])
    event = service.create_event(db, EventCreate(event_type="report_generated", level="info", title="untrusted",
        extra_json=json.dumps({"subscription_id": subscription.id, "channels": [sms.id],
                               "recipients": ["attacker@example.invalid"], "policy_id": -subscription.id})))
    service.dispatch_event(db, event.id)
    assert [call[0] for call in calls] == ["global@example.invalid"]
    assert db.query(NotifyLog).one().channel_id == other.id
    with pytest.raises(ValueError, match="report service"):
        service.create_event(db, EventCreate(event_key="report-artifact:forged:subscription:1",
                                              event_type="report_generated", level="info", title="forged"))


@pytest.mark.parametrize("change", ["recipient", "channel", "disabled", "deleted", "tenant", "organization", "type"])
def test_retry_rechecks_subscription_boundary(delivery, change):
    db, schedule, (smtp, sms, *_), adapter, calls = delivery
    sub = subscribe(db, schedule, channels=[smtp.id], recipients=["selected@example.invalid"])
    adapter.return_value.send.side_effect = lambda *_: NotifyResult(False, 503, "unavailable")
    artifact = generate(db, schedule)
    log = db.query(NotifyLog).one()
    assert log.status == "failed" and log.next_retry_time is not None
    if change == "recipient":
        sub.recipients = ["new@example.invalid"]
    elif change == "channel":
        sub.channels = [sms.id]
    elif change == "disabled":
        sub.enabled = 0
    elif change == "deleted":
        db.delete(sub)
    elif change == "tenant":
        sub.tenant_id = 42
    elif change == "organization":
        sub.organization_id = 42
    else:
        sub.report_types = ["backup"]
    db.commit()
    adapter.return_value.send.reset_mock()
    service.consume_due_notifications(db, now=log.next_retry_time)
    db.refresh(log)
    assert log.status == "cancelled" and log.next_retry_time is None
    adapter.return_value.send.assert_not_called()


def test_scope_mismatch_never_dispatches(delivery):
    db, schedule, (smtp, *_), adapter, calls = delivery
    subscribe(db, schedule, channels=[smtp.id], recipients=["tenant@example.invalid"], tenant_id=77)
    subscribe(db, schedule, channels=[smtp.id], recipients=["scope@example.invalid"], filter_json={"instance_ids": [9]})
    generate(db, schedule)
    assert not calls


def test_legacy_report_log_cannot_replay_global_recipient(delivery):
    db, schedule, (smtp, sms, other, _), adapter, calls = delivery
    event = NotifyEvent(event_key="report-artifact:legacy:subscription:1", event_type="report_generated",
                        level="info", title="legacy")
    db.add(event)
    db.commit()
    log = NotifyLog(event_id=event.id, policy_id=1, channel_id=other.id, channel_type="smtp",
                    recipient="global@example.invalid", status="failed", attempt_count=1)
    db.add(log)
    db.commit()
    service.consume_due_notifications(db)
    db.refresh(log)
    assert log.status == "cancelled"
    with pytest.raises(ValueError, match="trusted"):
        service.dispatch_event(db, event.id)
    assert not calls


def test_audience_is_queued_before_first_send_and_pending_targets_resume(delivery):
    db, schedule, (smtp, *_), adapter, calls = delivery
    subscribe(db, schedule, channels=[smtp.id], recipients=["one@example.invalid", "two@example.invalid"])
    with patch.object(service, "_deliver_log", side_effect=RuntimeError("interrupted before send")):
        generate(db, schedule)
    assert db.query(NotifyLog).count() == 2
    assert {row.status for row in db.query(NotifyLog)} == {"pending"}
    assert service.consume_due_notifications(db)["processed"] == 2
    assert sorted(call[0] for call in calls) == ["one@example.invalid", "two@example.invalid"]
    assert service.consume_due_notifications(db)["processed"] == 0


def test_partial_failure_retries_only_failed_target(delivery):
    db, schedule, (smtp, *_), adapter, calls = delivery
    subscribe(db, schedule, channels=[smtp.id], recipients=["failed@example.invalid", "ok@example.invalid"])

    def send(recipient, *_):
        calls.append(recipient)
        return NotifyResult(recipient == "ok@example.invalid", 503, "fake result")

    adapter.return_value.send.side_effect = send
    artifact = generate(db, schedule)
    assert sorted(calls) == ["failed@example.invalid", "ok@example.invalid"]
    log = db.query(NotifyLog).filter(NotifyLog.status == "failed").one()
    adapter.return_value.send.side_effect = None
    adapter.return_value.send.return_value = NotifyResult(True, 200, "ok")
    adapter.return_value.send.reset_mock()
    report_center._dispatch_subscriptions(db, schedule, artifact)
    adapter.return_value.send.assert_not_called()
    assert service.consume_due_notifications(db, now=log.next_retry_time)["processed"] == 1
    assert adapter.return_value.send.call_args.args[0] == "failed@example.invalid"
    assert {row.status for row in db.query(NotifyLog).populate_existing()} == {"sent"}


@pytest.mark.parametrize("channels,recipients", [(["missing"], ["x@example.invalid"]), (["email"], ["invalid-address"])])
def test_invalid_target_is_reported_not_silently_dropped(delivery, channels, recipients):
    db, schedule, _, adapter, calls = delivery
    subscribe(db, schedule, channels=channels, recipients=recipients)
    artifact = generate(db, schedule)
    assert artifact.extra_json["subscription_errors"]
    assert not calls


def test_concurrent_subscription_dispatch_uses_existing_nonnull_unique_key():
    engine = create_engine(f"sqlite:///file:subscription-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true",
                           connect_args={"check_same_thread": False, "timeout": 5})
    keeper = engine.connect()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        channel = NotifyChannel(channel_name="mail", channel_type="smtp", config_json="{}")
        db.add(channel)
        db.commit()
        schedule = report_center.create_schedule(db, {"name": "daily", "report_type": "inspection"}, actor="test")
        subscription = subscribe(db, schedule, channels=[channel.id], recipients=["one@example.invalid", "two@example.invalid"])
        with patch.object(report_center, "_dispatch_subscriptions"):
            artifact = generate(db, schedule)
        ids = (subscription.id, schedule.id, artifact.id)
    entered, release = threading.Event(), threading.Event()
    errors, outcomes = [], []

    def send(*_):
        entered.set()
        assert release.wait(5)
        return NotifyResult(True, 200, "ok")

    def dispatch():
        try:
            with factory() as db:
                outcomes.append(service.dispatch_report_subscription(
                    db, db.get(ReportSubscription, ids[0]), db.get(ReportSchedule, ids[1]), db.get(ReportArtifact, ids[2]),
                ))
        except Exception as exc:
            errors.append(exc)

    try:
        with patch.object(service, "get_adapter") as adapter:
            adapter.return_value.send.side_effect = send
            first = threading.Thread(target=dispatch)
            second = threading.Thread(target=dispatch)
            first.start()
            try:
                assert entered.wait(5)
                second.start()
                second.join(5)
            finally:
                release.set()
                first.join(5)
                if second.ident:
                    second.join(5)
            assert not first.is_alive() and not second.is_alive()
            assert errors == []
            assert sorted(item["sent_count"] for item in outcomes) == [0, 2]
            assert adapter.return_value.send.call_count == 2
        with factory() as db:
            logs = db.query(NotifyLog).all()
            assert len(logs) == 2 and all(row.policy_id == -ids[0] for row in logs)
    finally:
        keeper.close()
        engine.dispose()


@pytest.mark.parametrize("pending", [False, True])
def test_subscription_dispatch_counts_success_failure_and_pending_separately(delivery, pending):
    db, schedule, (smtp, *_), adapter, calls = delivery
    sub = subscribe(db, schedule, channels=[smtp.id], recipients=["ok@example.invalid", "failed@example.invalid"])
    with patch.object(report_center, "_dispatch_subscriptions"):
        artifact = generate(db, schedule)
    adapter.return_value.send.side_effect = lambda recipient, *_: NotifyResult(recipient == "ok@example.invalid", 200, "fake")
    if pending:
        with patch.object(service, "_deliver_log", return_value=False):
            result = service.dispatch_report_subscription(db, sub, schedule, artifact)
        assert result["sent_count"] == result["attempted_count"] == result["failed_count"] == 0
        assert result["pending_count"] == 2
        adapter.return_value.send.assert_not_called()
    else:
        result = service.dispatch_report_subscription(db, sub, schedule, artifact)
        assert result["sent_count"] == 1 and result["attempted_count"] == 2
        assert result["failed_count"] == 1 and result["pending_count"] == 0
    repeated = service.dispatch_report_subscription(db, sub, schedule, artifact)
    assert repeated["sent_count"] == repeated["attempted_count"] == repeated["pending_count"] == 0
