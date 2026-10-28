"""A close that was never confirmed must not retire a live alert.

The old Jira adapter returned ``TicketResult(True, ..., "Closed")`` whenever it could
read the transition list - including when the workflow had no close transition and when
the POST was rejected. ``service.close_ticket`` mapped that to ``closed`` and then closed
the source alert, so a Jira misconfiguration silently retired live alerts.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.itsm import service
from app.itsm.adapters.base import (
    OUTCOME_BLOCKED,
    OUTCOME_CONFIRMED,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    OUTCOME_UNVERIFIED,
)
from app.itsm.adapters.jira import JiraAdapter
from app.itsm.models import ItsmConfig, ItsmTicket, ItsmTicketSyncLog
from app.itsm.schemas import TicketClose
from app.models.alert_event import DBAlertEvent

CONFIG = {"base_url": "https://jira.example", "token_cipher": "t", "timeout_seconds": 5}


def response(status_code, payload=None, text=""):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload if payload is not None else {},
        text=text or str(payload or ""),
    )


class JiraCloseTruthTest(unittest.TestCase):
    def setUp(self):
        self.adapter = JiraAdapter(CONFIG)

    def _run(self, listing, posted=None, status_read=None):
        """Drive close_ticket with scripted HTTP answers.

        ``status_read`` is the post-transition confirmation read; ``None`` means the read
        never happens because no transition was submitted.
        """
        calls = []

        def fake_get(url, **kwargs):
            if url.endswith("/transitions"):
                calls.append("list")
                return listing
            calls.append("read")
            if status_read is None:
                raise AssertionError("unexpected status read")
            return status_read

        def fake_post(url, **kwargs):
            calls.append("post")
            return posted

        with patch("app.itsm.adapters.jira.requests.get", fake_get), \
             patch("app.itsm.adapters.jira.requests.post", fake_post):
            return self.adapter.close_ticket("EXT-1", {}), calls

    def test_no_close_transition_is_blocked_not_closed(self):
        result, calls = self._run(
            response(200, {"transitions": [{"id": "11", "name": "To Do"}]}),
            status_read=response(200, {"fields": {"status": {"name": "In Progress"}}}),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, OUTCOME_BLOCKED)
        self.assertEqual(result.status, "In Progress")
        self.assertNotIn("post", calls)

    def test_already_closed_without_transition_is_confirmed(self):
        result, _ = self._run(
            response(200, {"transitions": []}),
            status_read=response(200, {"fields": {"status": {"name": "Closed"}}}),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertEqual(result.status, "Closed")

    def test_posted_transition_is_confirmed_by_reading_back(self):
        result, calls = self._run(
            response(200, {"transitions": [{"id": "31", "name": "Done"}]}),
            posted=response(204),
            status_read=response(200, {"fields": {"status": {"name": "Done"}}}),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertIn("post", calls)

    def test_posted_but_status_unchanged_is_a_failure(self):
        result, _ = self._run(
            response(200, {"transitions": [{"id": "31", "name": "Done"}]}),
            posted=response(204),
            status_read=response(200, {"fields": {"status": {"name": "In Progress"}}}),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertIn("仍为 In Progress", result.error_message)

    def test_rejected_transition_is_reported(self):
        result, calls = self._run(
            response(200, {"transitions": [{"id": "31", "name": "Done"}]}),
            posted=response(409, text="conflict"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertIn("409", result.error_message)

    def test_unreadable_final_state_is_unknown_not_success(self):
        result, _ = self._run(
            response(200, {"transitions": [{"id": "31", "name": "Done"}]}),
            posted=response(204),
            status_read=response(500, text="boom"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, OUTCOME_UNKNOWN)

    def test_failed_transition_listing_is_reported(self):
        result, _ = self._run(response(403, text="forbidden"), status_read=None)
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, OUTCOME_FAILED)


class CloseDoesNotRetireLiveAlertTest(unittest.TestCase):
    """The service side: an unconfirmed close must not close the source alert."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _fixture(self, db):
        config = ItsmConfig(config_name="jira", provider_type="jira", base_url="https://jira.example")
        db.add(config)
        alert = DBAlertEvent(event_uid="alert-99", source="zabbix", title="Database unavailable")
        db.add(alert)
        db.commit()
        db.refresh(config)
        db.refresh(alert)
        self.initial_alert_status = alert.status
        ticket = ItsmTicket(
            config_id=config.id, source_type="ALERT", source_id="alert:99",
            source_ref_id=alert.id,
            title="t", mesguard_status="created", external_status="Open",
            external_ticket_id="EXT-1",
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return config, ticket, alert

    def _close_with(self, result):
        with self.Session() as db:
            config, ticket, alert = self._fixture(db)
            with patch.object(service, "get_adapter", return_value=SimpleNamespace(
                close_ticket=lambda external_id, payload: result,
            )):
                updated = service.close_ticket(
                    db, ticket.id, TicketClose(resolution="done", close_source=True),
                )
            ticket_id, alert_id = updated.id, alert.id
            status = updated.mesguard_status
            external = updated.external_status
        with self.Session() as db:
            alert_after = db.get(DBAlertEvent, alert_id).status
            logs = db.query(ItsmTicketSyncLog).filter_by(ticket_id=ticket_id).all()
        return status, external, alert_after, logs

    def test_blocked_close_leaves_the_source_alert_open(self):
        from app.itsm.adapters.base import TicketResult

        status, external, alert_status, logs = self._close_with(
            TicketResult(False, "EXT-1", status="In Progress",
                         error_message="Jira 工作流未提供关闭/完成转换，工单未关闭",
                         outcome=OUTCOME_BLOCKED),
        )
        self.assertEqual(status, "failed")
        self.assertEqual(external, "Open")  # not silently rewritten to "Closed"
        self.assertNotEqual(alert_status, "closed")

    def test_unknown_outcome_leaves_the_source_alert_open(self):
        from app.itsm.adapters.base import TicketResult

        status, external, alert_status, logs = self._close_with(
            TicketResult(False, "EXT-1", error_message="无法回读最终状态", outcome=OUTCOME_UNKNOWN),
        )
        self.assertEqual(status, "failed")
        # An adapter that reports no status must not have one invented for it.
        self.assertEqual(external, "Open")
        self.assertNotEqual(alert_status, "closed")
        self.assertTrue(any("outcome=unknown" in (log.error_message or "") for log in logs))

    def test_success_without_a_reported_status_does_not_invent_one(self):
        from app.itsm.adapters.base import TicketResult

        # Adapters that write state directly (ServiceNow/ZenTao PUT) answer 2xx without
        # naming the resulting status. That is not a licence to record one.
        status, external, _alert_status, _logs = self._close_with(
            TicketResult(True, "EXT-1", status="", response_body="{}", outcome=OUTCOME_UNVERIFIED),
        )
        self.assertEqual(status, "closed")
        self.assertEqual(external, "Open")

    def test_confirmed_close_still_closes_the_source_alert(self):
        from app.itsm.adapters.base import TicketResult

        status, external, alert_status, _logs = self._close_with(
            TicketResult(True, "EXT-1", status="Done", outcome=OUTCOME_CONFIRMED),
        )
        self.assertEqual(status, "resolved")
        self.assertEqual(external, "Done")
        self.assertEqual(alert_status, "closed")


if __name__ == "__main__":
    unittest.main()
