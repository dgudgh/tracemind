"""The data quality consumer has to run on a clock, not on page views (ISS-22).

`evaluate_data_quality` and `run_retention` existed with a policy model, a snapshot table and an
API - and no caller that ran periodically. The platform had no quality trend of its own; the page
computed whatever the request moment produced.

The retention half is deliberately conservative: the job plans by default and deletes only for
scopes that opted in, because a scheduler that removes metric history by default is data loss
with a cron expression.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.observability import (
    ObservabilityDataQualityPolicy,
    ObservabilityDataQualitySnapshot,
    ObservabilityMetricHistory,
    ObservabilitySource,
)
from app.tasks import data_quality_scheduler, master_scheduler
from app.services.observability import data_quality_service


class DataQualitySchedulerTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(ObservabilitySource(
                source_name="quality-source",
                source_type="prometheus",
                enabled=1,
                collect_interval_seconds=60,
            ))
            db.commit()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _run_jobs(self):
        """Run the consumer against the test database instead of the configured server."""
        with patch.object(data_quality_scheduler, "SessionLocal", self.Session):
            return data_quality_scheduler.run_due_data_quality_jobs()

    @staticmethod
    def _utcnow():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _expired_samples(self, count: int = 3):
        with self.Session() as db:
            source = db.query(ObservabilitySource).first()
            now = self._utcnow()
            for index in range(count):
                db.add(ObservabilityMetricHistory(
                    source_id=source.id,
                    source_type="prometheus",
                    standard_metric=f"metric_{index}",
                    metric_value=1.0,
                    metric_time=now - timedelta(days=400),
                ))
            db.commit()

    def test_periodic_run_persists_a_snapshot(self):
        result = self._run_jobs()
        self.assertTrue(any(item["evaluated"] for item in result["items"]))
        with self.Session() as db:
            self.assertGreater(db.query(ObservabilityDataQualitySnapshot).count(), 0)

    def test_second_run_inside_the_interval_does_not_re_evaluate(self):
        first = self._run_jobs()
        self.assertTrue(first["items"][0]["evaluated"])
        with self.Session() as db:
            snapshots = db.query(ObservabilityDataQualitySnapshot).count()
        second = self._run_jobs()
        self.assertFalse(second["items"][0]["evaluated"])
        with self.Session() as db:
            self.assertEqual(db.query(ObservabilityDataQualitySnapshot).count(), snapshots)

    def test_retention_is_planned_not_executed_by_default(self):
        self._expired_samples()
        result = self._run_jobs()
        item = result["items"][0]
        self.assertTrue(item["retention_planned"])
        self.assertFalse(item["retention_executed"])
        with self.Session() as db:
            self.assertEqual(db.query(ObservabilityMetricHistory).count(), 3)

    def test_retention_executes_only_when_the_scope_opted_in(self):
        self._expired_samples()
        with self.Session() as db:
            data_quality_service.ensure_default_policy(db)
            policy = db.query(ObservabilityDataQualityPolicy).first()
            policy.retention_auto_execute = 1
            db.commit()
        result = self._run_jobs()
        self.assertTrue(result["items"][0]["retention_executed"])
        with self.Session() as db:
            self.assertEqual(db.query(ObservabilityMetricHistory).count(), 0)
            db.refresh(db.query(ObservabilityDataQualityPolicy).first())
            self.assertIsNotNone(db.query(ObservabilityDataQualityPolicy).first().last_retention_at)

    def test_retention_outside_the_interval_is_skipped(self):
        with self.Session() as db:
            data_quality_service.ensure_default_policy(db)
            policy = db.query(ObservabilityDataQualityPolicy).first()
            policy.last_retention_at = self._utcnow()
            db.commit()
        result = self._run_jobs()
        self.assertFalse(result["items"][0]["retention_planned"])

    def test_consumer_is_registered_in_the_master_scheduler(self):
        jobs = master_scheduler.due_job_functions()
        self.assertIn(data_quality_scheduler.run_due_data_quality_jobs, jobs)

    def test_policy_exposes_a_cadence(self):
        with self.Session() as db:
            policy = data_quality_service.ensure_default_policy(db)
            rendered = data_quality_service.policy_to_dict(policy)
        self.assertGreaterEqual(rendered["evaluate_interval_seconds"], 60)
        self.assertEqual(rendered["retention_auto_execute"], 0)


if __name__ == "__main__":
    unittest.main()
