import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.notify import service
from app.notify.api import router
from app.notify.models import (
    NotifyChannel,
    NotifyLog,
    NotifyPolicy,
    NotifyPolicyChannel,
)
from app.notify.schemas import ChannelCreate, EventCreate


class BlockingNotifyAdapter:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def send(self, *_args):
        with self.lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return SimpleNamespace(
            success=True,
            status_code=200,
            response_body='{"token":"response-token","nested":{"api_key":"response-key"}}',
            error_message=None,
        )


class NotifyDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_non_http_notification_url_is_rejected_before_storage(self):
        with self.Session() as db:
            with self.assertRaises(ValueError):
                service.create_channel(
                    db,
                    ChannelCreate(
                        channel_name="unsafe webhook",
                        channel_type="webhook",
                        config_json=json.dumps({"url": "file:///tmp/notify"}),
                    ),
                )

            self.assertEqual(db.query(NotifyChannel).count(), 0)

    def test_duplicate_event_key_returns_the_original_event(self):
        with self.Session() as db:
            first = service.create_event(
                db,
                EventCreate(
                    event_key="alert:42",
                    event_type="alert",
                    level="critical",
                    title="Database unavailable",
                ),
            )
            second = service.create_event(
                db,
                EventCreate(
                    event_key="alert:42",
                    event_type="alert",
                    level="critical",
                    title="Database unavailable",
                ),
            )

            self.assertEqual(first.id, second.id)

    def test_concurrent_event_create_recovers_the_unique_conflict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'notify.db'}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            barrier = threading.Barrier(2)
            ids = []
            errors = []

            def create():
                try:
                    with Session() as db:
                        barrier.wait(timeout=5)
                        event = service.create_event(db, EventCreate(
                            event_key="alert:concurrent:42",
                            event_type="alert",
                            level="critical",
                            title="Database unavailable",
                        ))
                        ids.append(event.id)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=create) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(ids), 2)
            self.assertEqual(len(set(ids)), 1)
            engine.dispose()

    def test_concurrent_dispatch_claims_delivery_before_external_send(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'dispatch.db'}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                channel = NotifyChannel(
                    channel_name="dispatch-webhook",
                    channel_type="webhook",
                    config_json=service.encode_channel_config({"url": "https://notify.invalid"}),
                )
                policy = NotifyPolicy(policy_name="critical", level_filter="critical", throttle_minutes=0)
                event = service.create_event(db, EventCreate(
                    event_key="alert:dispatch:42",
                    event_type="alert",
                    level="critical",
                    title="Database unavailable",
                ))
                db.add_all([channel, policy])
                db.commit()
                db.refresh(channel)
                db.refresh(policy)
                db.add(NotifyPolicyChannel(policy_id=policy.id, channel_id=channel.id))
                db.commit()
                event_id = event.id

            adapter = BlockingNotifyAdapter()
            results = []
            errors = []

            def dispatch():
                try:
                    with Session() as db:
                        results.append(service.dispatch_event(db, event_id))
                except Exception as exc:
                    errors.append(exc)

            with patch("app.notify.service.get_adapter", return_value=adapter):
                first = threading.Thread(target=dispatch)
                first.start()
                self.assertTrue(adapter.entered.wait(timeout=5))
                second = threading.Thread(target=dispatch)
                second.start()
                second.join(timeout=5)
                adapter.release.set()
                first.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(sum(result["sent_count"] for result in results), 1)
            with Session() as db:
                logs = db.query(NotifyLog).filter(NotifyLog.event_id == event_id).all()
                self.assertEqual(len(logs), 1)
                serialized = json.dumps({
                    "response": logs[0].response_body,
                    "error": logs[0].error_message,
                })
                self.assertNotIn("response-token", serialized)
                self.assertNotIn("response-key", serialized)
            engine.dispose()

    def test_retry_log_rejects_a_log_after_three_attempts(self):
        retry_log = getattr(service, "retry_log", None)
        self.assertIsNotNone(retry_log)

        with self.Session() as db:
            log = NotifyLog(
                channel_id=1,
                channel_type="webhook",
                recipient="https://example.invalid/notify",
                status="failed",
            )
            log.attempt_count = 3
            db.add(log)
            db.commit()

            with self.assertRaisesRegex(ValueError, "重试次数已达上限"):
                retry_log(db, log.id)

    def test_concurrent_retry_claims_failed_log_before_external_send(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'retry.db'}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                channel = NotifyChannel(
                    channel_name="retry-webhook",
                    channel_type="webhook",
                    config_json=service.encode_channel_config({"url": "https://notify.invalid"}),
                )
                db.add(channel)
                db.commit()
                db.refresh(channel)
                log = NotifyLog(
                    channel_id=channel.id,
                    channel_type="webhook",
                    recipient="",
                    status="failed",
                    attempt_count=1,
                )
                db.add(log)
                db.commit()
                log_id = log.id

            adapter = BlockingNotifyAdapter()
            errors = []
            outcomes = []

            def retry():
                try:
                    with Session() as db:
                        service.retry_log(db, log_id)
                    outcomes.append("retried")
                except ValueError:
                    outcomes.append("rejected")
                except Exception as exc:
                    errors.append(exc)

            with patch("app.notify.service.get_adapter", return_value=adapter):
                first = threading.Thread(target=retry)
                first.start()
                self.assertTrue(adapter.entered.wait(timeout=5))
                second = threading.Thread(target=retry)
                second.start()
                second.join(timeout=5)
                adapter.release.set()
                first.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertCountEqual(outcomes, ["retried", "rejected"])
            self.assertEqual(adapter.calls, 1)
            with Session() as db:
                stored = db.get(NotifyLog, log_id)
                self.assertEqual(stored.attempt_count, 2)
                self.assertEqual(stored.retry_count, 1)
            engine.dispose()

    def test_retry_http_contract_remains_success_and_retry_count_only(self):
        with self.Session() as db:
            channel = NotifyChannel(
                channel_name="http-retry",
                channel_type="webhook",
                config_json=service.encode_channel_config({"url": "https://notify.invalid"}),
            )
            db.add(channel)
            db.commit()
            db.refresh(channel)
            log = NotifyLog(channel_id=channel.id, channel_type="webhook", recipient="", status="failed")
            db.add(log)
            db.commit()
            log_id = log.id

        app = FastAPI()
        app.include_router(router)

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        result = SimpleNamespace(success=True, status_code=200, response_body="ok", error_message=None)
        with TestClient(app) as client, patch("app.notify.service.get_adapter") as get_adapter:
            get_adapter.return_value.send.return_value = result
            response = client.post(f"/notify/logs/{log_id}/retry")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "retry_count": 1})

    def test_event_and_test_channel_serialization_redacts_nested_secrets(self):
        secret_payload = {
            "password": "event-password",
            "nested": [{"Authorization": "Bearer event-token"}],
        }
        with self.Session() as db:
            event = service.create_event(db, EventCreate(
                event_key="alert:redaction:42",
                event_type="alert",
                level="critical",
                title="Database unavailable",
                body='{"client_secret":"body-secret"}',
                extra_json=json.dumps(secret_payload),
            ))
            self.assertNotIn("event-password", event.extra_json)
            self.assertNotIn("event-token", event.extra_json)
            self.assertNotIn("body-secret", event.body)

            channel = NotifyChannel(
                channel_name="redacted-response",
                channel_type="webhook",
                config_json=service.encode_channel_config({"url": "https://notify.invalid"}),
            )
            db.add(channel)
            db.commit()
            db.refresh(channel)

            adapter = SimpleNamespace(
                validate_config=lambda: [],
                send=lambda *_args: SimpleNamespace(
                    success=False,
                    status_code=401,
                    response_body='{"secret":"remote-secret"}',
                    error_message='{"api_key":"remote-key"}',
                ),
            )
            with patch("app.notify.service.get_adapter", return_value=adapter):
                result = service.test_channel(db, SimpleNamespace(
                    channel_id=channel.id,
                    title="test",
                    content="test",
                ))
            serialized = json.dumps(result)
            self.assertNotIn("remote-secret", serialized)
            self.assertNotIn("remote-key", serialized)


if __name__ == "__main__":
    unittest.main()
