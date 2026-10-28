"""Capacity alerts must not flap, and a breach must persist before it is published (ISS-25).

`DBCapacityPolicy` had `consecutive_failures` and nothing else: one sample above the threshold
opened an alert, one sample back under it closed the same alert. On a metric that hovers around
its threshold that is a page on every collection run, and an operator learns to ignore it - the
alert that matters arrives with the same weight as the noise.

Two independent controls, both off by default so today's behaviour is unchanged until configured:
* `sustained_minutes` - a breach is recorded but not published until it has lasted that long.
* `recovery_margin_pct` - a metric must recover that far inside the threshold before closing.
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.capacity_policy import DBCapacityAlert, DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.services import capacity_monitor


def _now():
    return datetime(2026, 10, 19, 12, 0, 0)


class CapacityAntiFlapTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(DBInstance(
                instance_name="capacity-target",
                db_type="mysql",
                host="127.0.0.1",
                port=3306,
                username="monitor",
                password_cipher="cipher",
                enabled=1,
            ))
            db.commit()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _instance(self):
        with self.Session() as db:
            return db.get(DBInstance, db.query(DBInstance).first().id)

    def _policy(self, *, sustained_minutes: int = 0, margin: int = 10) -> DBCapacityPolicy:
        with self.Session() as db:
            instance = db.query(DBInstance).first()
            policy = capacity_monitor.get_or_create_policy(db, instance.id)
            policy.sustained_minutes = sustained_minutes
            policy.recovery_margin_pct = Decimal(str(margin))
            policy.forecast_days_threshold = 15
            db.commit()
            return policy.id

    def _breach(self, policy_id, *, level="critical", forecast_days=3):
        instance_id = self._instance().id
        with self.Session() as db:
            instance = db.get(DBInstance, instance_id)
            policy = db.get(DBCapacityPolicy, policy_id)
            capacity_monitor._create_alerts_from_analysis(db, instance, [{
                "object_name": "users", "object_type": "tablespace", "level": level,
                "forecast_days": forecast_days, "reason": "剩余天数不足",
            }], policy)
            db.commit()

    def _alert(self):
        with self.Session() as db:
            return db.query(DBCapacityAlert).first()

    def test_breach_is_published_immediately_by_default(self):
        policy = self._policy()
        self._breach(policy)
        alert = self._alert()
        self.assertEqual(alert.status, "open")
        self.assertIsNotNone(alert.notified_at)

    def test_young_breach_is_recorded_but_not_published(self):
        policy = self._policy(sustained_minutes=60)
        self._breach(policy)
        alert = self._alert()
        self.assertEqual(alert.status, "open")
        # The row exists so the streak is visible, but nothing was paged yet.
        self.assertIsNone(alert.notified_at)

    def test_old_enough_breach_is_published(self):
        policy = self._policy(sustained_minutes=60)
        self._breach(policy)
        with self.Session() as db:
            alert = db.query(DBCapacityAlert).first()
            alert.first_seen = _now() - timedelta(minutes=90)
            db.commit()
        self._breach(policy)
        self.assertIsNotNone(self._alert().notified_at)

    def test_alert_inside_the_recovery_margin_stays_open(self):
        """A metric just inside the threshold must not close and reopen on every run."""
        policy = self._policy(margin=50)
        self._breach(policy)
        self.assertEqual(self._alert().status, "open")

        # Recovery band with 50% margin on a 15 day threshold: anything up to 22.5 days holds.
        self._breach(policy, level="normal", forecast_days=20)
        self.assertEqual(self._alert().status, "open")

    def test_alert_outside_the_recovery_margin_closes(self):
        policy = self._policy(margin=50)
        self._breach(policy)
        self._breach(policy, level="normal", forecast_days=90)
        self.assertEqual(self._alert().status, "closed")

    def test_policy_exposes_the_controls(self):
        policy = self._policy(sustained_minutes=30, margin=25)
        with self.Session() as db:
            rendered = capacity_monitor.policy_to_dict(db.get(DBCapacityPolicy, policy))
        self.assertEqual(rendered["sustained_minutes"], 30)
        self.assertEqual(rendered["recovery_margin_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()
