import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.itsm import service
from app.itsm.api import router
from app.itsm.adapters.base import TicketResult
from app.itsm.models import ItsmConfig, ItsmTicket, ItsmTicketSyncLog
from app.itsm.schemas import ItsmConfigCreate, TicketCreate
from app.models.alert_event import DBAlertEvent


class FakeTicketAdapter:
    def __init__(self):
        self.create_calls = 0
        self.external_status = "In Progress"
        self.close_result = TicketResult(True, "EXT-42", status="Closed", response_body="closed")

    def create_ticket(self, payload):
        self.create_calls += 1
        return TicketResult(True, "EXT-42", "https://itsm.example/EXT-42", "New", "created")

    def get_ticket(self, external_id):
        return TicketResult(True, external_id, status=self.external_status, response_body=self.external_status)

    def close_ticket(self, external_id, payload):
        return self.close_result


class BlockingTicketAdapter(FakeTicketAdapter):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def create_ticket(self, payload):
        with self.lock:
            self.create_calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return TicketResult(True, "EXT-CONCURRENT", status="New", response_body="created")


class RaisingTicketAdapter(FakeTicketAdapter):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def create_ticket(self, payload):
        if self.operation == "create":
            raise RuntimeError('{"token":"create-token"}')
        return super().create_ticket(payload)

    def get_ticket(self, external_id):
        if self.operation == "sync":
            raise RuntimeError('{"password":"sync-password"}')
        return super().get_ticket(external_id)

    def close_ticket(self, external_id, payload):
        if self.operation == "close":
            raise RuntimeError('{"authorization":"close-authorization"}')
        return super().close_ticket(external_id, payload)


class ItsmLifecycleTest(unittest.TestCase):
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

    def test_non_http_itsm_url_is_rejected(self):
        with self.assertRaises(ValidationError):
            ItsmConfigCreate(
                config_name="unsafe",
                provider_type="webhook",
                base_url="file:///tmp/tickets",
            )

    def _config(self, db):
        config = ItsmConfig(
            config_name="test-itsm",
            provider_type="webhook",
            base_url="https://itsm.example",
        )
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    def _ticket_data(self, config_id, source_id="alert:42", source_ref_id=None):
        return TicketCreate(
            config_id=config_id,
            source_type="ALERT",
            source_id=source_id,
            source_ref_id=source_ref_id,
            instance_id=1,
            title="Database unavailable",
            idempotency_key=f"itsm:{source_id}:1",
        )

    def test_create_is_idempotent_and_sync_maps_external_states(self):
        adapter = FakeTicketAdapter()
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            first = service.create_ticket(db, self._ticket_data(config.id))
            stored = db.query(ItsmTicket).filter(ItsmTicket.id == first["ticket_id"]).first()
            self.assertEqual(getattr(stored, "idempotency_key", None), "itsm:alert:42:1")
            try:
                second = service.create_ticket(db, self._ticket_data(config.id))
            except ValueError as exc:
                self.fail(f"idempotent create raised {exc}")

            self.assertEqual(first["ticket_id"], second["ticket_id"])
            self.assertEqual(adapter.create_calls, 1)

            sync_ticket = getattr(service, "sync_ticket", None)
            self.assertIsNotNone(sync_ticket)
            for external_status, expected_status in (
                ("In Progress", "in_progress"),
                ("Resolved", "resolved"),
                ("Closed", "closed"),
                ("Failed", "failed"),
            ):
                adapter.external_status = external_status
                ticket = sync_ticket(db, first["ticket_id"])
                self.assertEqual(ticket.mesguard_status, expected_status)

            logs = db.query(ItsmTicketSyncLog).filter(ItsmTicketSyncLog.ticket_id == first["ticket_id"]).all()
            self.assertGreaterEqual(len(logs), 6)
            self.assertEqual(logs[-1].new_status, "failed")

    def test_source_tuple_generates_a_stable_idempotency_key(self):
        adapter = FakeTicketAdapter()
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            data = self._ticket_data(config.id)
            data.idempotency_key = None

            first = service.create_ticket(db, data)
            second = service.create_ticket(db, data)
            ticket = db.get(ItsmTicket, first["ticket_id"])

            self.assertTrue(ticket.idempotency_key)
            self.assertEqual(first["ticket_id"], second["ticket_id"])
            self.assertEqual(adapter.create_calls, 1)

    def test_concurrent_create_persists_placeholder_before_external_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'itsm.db'}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                config = self._config(db)
                config_id = config.id

            adapter = BlockingTicketAdapter()
            results = []
            errors = []

            def create():
                try:
                    with Session() as db:
                        data = self._ticket_data(config_id)
                        data.idempotency_key = None
                        results.append(service.create_ticket(db, data))
                except Exception as exc:
                    errors.append(exc)

            with patch("app.itsm.service.get_adapter", return_value=adapter):
                first = threading.Thread(target=create)
                first.start()
                self.assertTrue(adapter.entered.wait(timeout=5))
                second = threading.Thread(target=create)
                second.start()
                second.join(timeout=5)
                adapter.release.set()
                first.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(adapter.create_calls, 1)
            self.assertEqual(len({result["ticket_id"] for result in results}), 1)
            with Session() as db:
                self.assertEqual(db.query(ItsmTicket).count(), 1)
            engine.dispose()

    def test_servicenow_numeric_states_map_to_internal_lifecycle(self):
        adapter = FakeTicketAdapter()
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            config.provider_type = "servicenow"
            db.commit()
            created = service.create_ticket(db, self._ticket_data(config.id))

            for external_status, expected_status in (
                (1, "created"),
                (2, "in_progress"),
                (6, "resolved"),
                (7, "closed"),
            ):
                adapter.external_status = external_status
                ticket = service.sync_ticket(db, created["ticket_id"])
                self.assertEqual(ticket.mesguard_status, expected_status)

    def test_extra_config_is_encrypted_at_rest_and_recursively_redacted_over_http(self):
        secret = "nested-extra-secret"
        extra = json.dumps({
            "project": "OPS",
            "auth": {"api_key": secret},
            "headers": [{"Authorization": "Bearer nested-token"}],
        })
        with self.Session() as db:
            config = service.create_config(db, ItsmConfigCreate(
                config_name="secret-config",
                provider_type="webhook",
                base_url="https://itsm.example",
                extra_config=extra,
            ))
            config_id = config.id
            self.assertNotIn(secret, config.extra_config)
            self.assertNotIn("nested-token", config.extra_config)

        app = FastAPI()
        app.include_router(router)

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        with TestClient(app) as client:
            response = client.get(f"/itsm/configs/{config_id}")

        self.assertEqual(response.status_code, 200)
        serialized = response.text
        self.assertNotIn(secret, serialized)
        self.assertNotIn("nested-token", serialized)
        self.assertIn("***", serialized)

    def test_create_response_error_and_audit_payloads_are_recursively_redacted(self):
        class SecretResultAdapter(FakeTicketAdapter):
            def create_ticket(self, payload):
                self.create_calls += 1
                return TicketResult(
                    False,
                    response_body='{"nested":{"secret":"response-secret"}}',
                    error_message='{"api_key":"error-key"}',
                )

        adapter = SecretResultAdapter()
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            data = self._ticket_data(config.id)
            data.description = '{"authorization":"request-authorization"}'
            result = service.create_ticket(db, data)
            ticket = db.get(ItsmTicket, result["ticket_id"])
            logs = db.query(ItsmTicketSyncLog).filter(ItsmTicketSyncLog.ticket_id == ticket.id).all()
            serialized = json.dumps(result) + ticket.description + ticket.create_payload + ticket.external_response + "".join(
                (log.request_payload or "") + (log.response_payload or "") + (log.error_message or "")
                for log in logs
            )

            self.assertNotIn("request-authorization", serialized)
            self.assertNotIn("response-secret", serialized)
            self.assertNotIn("error-key", serialized)

    def test_close_ticket_closes_the_linked_alert_only_when_requested(self):
        adapter = FakeTicketAdapter()
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            alert = DBAlertEvent(event_uid="alert-42", source="zabbix", title="Database unavailable")
            db.add(alert)
            db.commit()
            db.refresh(alert)

            first = service.create_ticket(db, self._ticket_data(config.id, "alert:42", alert.id))
            try:
                service.close_ticket(db, first["ticket_id"], close_source=False)
            except TypeError as exc:
                self.fail(f"close_ticket does not accept close_source: {exc}")
            db.refresh(alert)
            self.assertEqual(alert.status, "firing")

            second = service.create_ticket(db, self._ticket_data(config.id, "alert:43", alert.id))
            ticket = service.close_ticket(db, second["ticket_id"], close_source=True)
            db.refresh(alert)
            self.assertEqual(ticket.mesguard_status, "closed")
            self.assertEqual(alert.status, "closed")

    def test_failed_close_marks_ticket_failed_without_closing_the_alert(self):
        adapter = FakeTicketAdapter()
        adapter.close_result = TicketResult(False, error_message="remote unavailable")
        with self.Session() as db, patch("app.itsm.service.get_adapter", return_value=adapter):
            config = self._config(db)
            alert = DBAlertEvent(event_uid="alert-43", source="zabbix", title="Database unavailable")
            db.add(alert)
            db.commit()
            db.refresh(alert)

            created = service.create_ticket(db, self._ticket_data(config.id, "alert:43", alert.id))
            try:
                ticket = service.close_ticket(db, created["ticket_id"], close_source=True)
            except TypeError as exc:
                self.fail(f"close_ticket does not accept close_source: {exc}")
            db.refresh(alert)

            self.assertEqual(ticket.mesguard_status, "failed")
            self.assertEqual(alert.status, "firing")
            log = db.query(ItsmTicketSyncLog).filter(ItsmTicketSyncLog.ticket_id == ticket.id).order_by(ItsmTicketSyncLog.id.desc()).first()
            self.assertEqual(log.sync_status, "failed")
            self.assertEqual(log.new_status, "failed")

    def test_adapter_exceptions_write_failed_audits_and_never_close_alert(self):
        with self.Session() as db:
            config = self._config(db)
            create_data = self._ticket_data(config.id, "alert:create-exception")
            with patch("app.itsm.service.get_adapter", return_value=RaisingTicketAdapter("create")):
                with self.assertRaises(RuntimeError):
                    service.create_ticket(db, create_data)

            create_ticket = db.query(ItsmTicket).filter(
                ItsmTicket.idempotency_key == create_data.idempotency_key,
            ).one()
            self.assertEqual(create_ticket.mesguard_status, "failed")
            create_log = db.query(ItsmTicketSyncLog).filter(
                ItsmTicketSyncLog.ticket_id == create_ticket.id,
                ItsmTicketSyncLog.sync_type == "create",
            ).one()
            self.assertEqual(create_log.sync_status, "failed")
            self.assertNotIn("create-token", create_log.error_message)

            alert = DBAlertEvent(event_uid="alert-exception", source="zabbix", title="Database unavailable")
            db.add(alert)
            db.commit()
            db.refresh(alert)
            with patch("app.itsm.service.get_adapter", return_value=FakeTicketAdapter()):
                created = service.create_ticket(db, self._ticket_data(config.id, "alert:existing", alert.id))
            ticket_id = created["ticket_id"]

            with patch("app.itsm.service.get_adapter", return_value=RaisingTicketAdapter("sync")):
                with self.assertRaises(RuntimeError):
                    service.sync_ticket(db, ticket_id)
            synced_ticket = db.get(ItsmTicket, ticket_id)
            db.refresh(synced_ticket)
            self.assertEqual(synced_ticket.mesguard_status, "failed")
            sync_log = db.query(ItsmTicketSyncLog).filter(
                ItsmTicketSyncLog.ticket_id == ticket_id,
                ItsmTicketSyncLog.sync_type == "sync",
            ).order_by(ItsmTicketSyncLog.id.desc()).first()
            self.assertEqual(sync_log.sync_status, "failed")
            self.assertNotIn("sync-password", sync_log.error_message)

            with patch("app.itsm.service.get_adapter", return_value=RaisingTicketAdapter("close")):
                with self.assertRaises(RuntimeError):
                    service.close_ticket(db, ticket_id, close_source=True)
            ticket = db.get(ItsmTicket, ticket_id)
            db.refresh(ticket)
            db.refresh(alert)
            close_log = db.query(ItsmTicketSyncLog).filter(
                ItsmTicketSyncLog.ticket_id == ticket_id,
                ItsmTicketSyncLog.sync_type == "close",
            ).order_by(ItsmTicketSyncLog.id.desc()).first()
            self.assertEqual(ticket.mesguard_status, "failed")
            self.assertEqual(close_log.sync_status, "failed")
            self.assertNotIn("close-authorization", close_log.error_message)
            self.assertEqual(alert.status, "firing")


if __name__ == "__main__":
    unittest.main()
