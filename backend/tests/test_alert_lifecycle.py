import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.alert_event import (
    DBAlertEvent,
    DBAlertLifecycleLog,
    DBAlertMaintenanceWindow,
    DBAlertSourceLink,
)
from app.services import alert_lifecycle


class AlertLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.now = datetime(2026, 8, 23, 10, 0, 0)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _payload(self, **values):
        payload = {
            "event_uid": "zabbix:1001",
            "source": "zabbix",
            "instance_id": 7,
            "db_type": "oracle",
            "severity": "critical",
            "status": "firing",
            "title": "Database tablespace usage high",
            "object_type": "tablespace",
            "object_name": "USERS",
            "metric_name": "database.tablespace.used_pct",
            "metric_value": 92.5,
            "threshold_value": 90,
        }
        payload.update(values)
        return payload

    def test_cross_source_events_are_deduplicated_and_recovery_closes_the_same_event(self):
        first = alert_lifecycle.upsert_event(self.db, self._payload(), now=self.now)
        self.db.commit()

        second = alert_lifecycle.upsert_event(
            self.db,
            self._payload(
                event_uid="prometheus:abcdef",
                source="prometheus",
                metric_value=93.1,
            ),
            now=self.now + timedelta(minutes=2),
        )
        self.db.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(DBAlertEvent).count(), 1)
        self.assertEqual(second.source_count, 2)
        self.assertEqual(second.occurrence_count, 2)
        self.assertEqual(self.db.query(DBAlertSourceLink).count(), 2)

        recovered = alert_lifecycle.upsert_event(
            self.db,
            self._payload(
                event_uid="prometheus:abcdef",
                source="prometheus",
                status="resolved",
            ),
            now=self.now + timedelta(minutes=5),
        )
        self.db.commit()
        self.assertEqual(recovered.id, first.id)
        self.assertEqual(recovered.status, "resolved")
        self.assertEqual(recovered.resolved_at, self.now + timedelta(minutes=5))
        self.assertEqual(recovered.occurrence_count, 2)

    def test_manual_transitions_keep_a_complete_audit_timeline(self):
        event = alert_lifecycle.upsert_event(self.db, self._payload(), now=self.now)
        self.db.commit()

        alert_lifecycle.transition_event(
            self.db,
            event,
            action="acknowledge",
            operator="dba1",
            reason="investigating",
            now=self.now + timedelta(minutes=1),
        )
        alert_lifecycle.transition_event(
            self.db,
            event,
            action="assign",
            operator="leader",
            assignee="dba2",
            now=self.now + timedelta(minutes=2),
        )
        alert_lifecycle.transition_event(
            self.db,
            event,
            action="suppress",
            operator="dba2",
            reason="approved change",
            suppressed_until=self.now + timedelta(minutes=30),
            now=self.now + timedelta(minutes=3),
        )
        self.db.commit()

        self.assertEqual(event.status, "suppressed")
        self.assertEqual(event.acknowledged_by, "dba1")
        self.assertEqual(event.assigned_to, "dba2")
        alert_lifecycle.refresh_runtime_states(self.db, self.now + timedelta(minutes=31))
        self.db.commit()
        self.assertEqual(event.status, "firing")

        alert_lifecycle.transition_event(
            self.db,
            event,
            action="close",
            operator="dba2",
            reason="fixed",
            now=self.now + timedelta(minutes=35),
        )
        self.db.commit()
        self.assertEqual(event.status, "closed")
        with self.assertRaisesRegex(ValueError, "reopened"):
            alert_lifecycle.transition_event(self.db, event, action="acknowledge", now=self.now + timedelta(minutes=36))
        alert_lifecycle.transition_event(self.db, event, action="reopen", now=self.now + timedelta(minutes=40))
        self.db.commit()
        self.assertEqual(event.status, "firing")
        actions = [row.action for row in self.db.query(DBAlertLifecycleLog).order_by(DBAlertLifecycleLog.id).all()]
        self.assertEqual(
            actions,
            [
                "created",
                "acknowledge",
                "assign",
                "suppress",
                "suppression_expired",
                "sla_breached",
                "close",
                "reopen",
            ],
        )

    def test_maintenance_window_suppresses_matching_alert_only(self):
        self.db.add(
            DBAlertMaintenanceWindow(
                name="Oracle storage change",
                starts_at=self.now - timedelta(minutes=5),
                ends_at=self.now + timedelta(hours=1),
                instance_id=7,
                metric_name_pattern="database.tablespace.*",
                reason="approved change CR-100",
                enabled=1,
            )
        )
        self.db.commit()
        event = alert_lifecycle.upsert_event(self.db, self._payload(), now=self.now)
        other = alert_lifecycle.upsert_event(
            self.db,
            self._payload(
                event_uid="zabbix:1002",
                instance_id=8,
                object_name="SYSTEM",
            ),
            now=self.now,
        )
        self.db.commit()

        self.assertEqual(event.status, "suppressed")
        self.assertEqual(event.suppress_reason, "approved change CR-100")
        self.assertIsNotNone(event.maintenance_window_id)
        self.assertEqual(other.status, "firing")

    def test_sla_breach_is_recorded_once_and_terminal_events_stop_the_clock(self):
        first_seen = self.now - timedelta(minutes=20)
        event = alert_lifecycle.upsert_event(
            self.db,
            self._payload(first_seen=first_seen, last_seen=first_seen),
            now=first_seen,
        )
        self.db.commit()
        self.assertEqual(event.sla_due_at, first_seen + timedelta(minutes=15))

        alert_lifecycle.refresh_runtime_states(self.db, self.now)
        alert_lifecycle.refresh_runtime_states(self.db, self.now + timedelta(minutes=1))
        self.db.commit()
        self.assertEqual(event.sla_breached_at, self.now)
        self.assertEqual(
            self.db.query(DBAlertLifecycleLog).filter(DBAlertLifecycleLog.action == "sla_breached").count(),
            1,
        )
        alert_lifecycle.transition_event(self.db, event, action="resolve", now=self.now + timedelta(minutes=2))
        self.db.commit()
        alert_lifecycle.refresh_runtime_states(self.db, self.now + timedelta(hours=1))
        self.db.commit()
        self.assertEqual(event.status, "resolved")


if __name__ == "__main__":
    unittest.main()
