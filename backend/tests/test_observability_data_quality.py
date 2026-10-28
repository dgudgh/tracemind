import unittest
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import observability
from app.core.database import Base, get_db
from app.models.observability import (
    ObservabilityCollectTask,
    ObservabilityCurrentMetric,
    ObservabilityDataQualitySnapshot,
    ObservabilityMetricHistory,
    ObservabilitySource,
)
from app.services.observability import data_quality_service


class ObservabilityDataQualityTest(unittest.TestCase):
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

    @staticmethod
    def _source(name="prom-main", source_type="prometheus", enabled=1):
        return ObservabilitySource(
            source_name=name,
            source_type=source_type,
            api_url="http://monitoring.example.invalid",
            enabled=enabled,
            collect_interval_seconds=60,
        )

    @staticmethod
    def _success_task(source_id, now):
        return ObservabilityCollectTask(
            source_id=source_id,
            task_name="标准指标采集",
            collect_type="prometheus",
            task_status="success",
            start_time=now - timedelta(seconds=2),
            end_time=now,
            success_count=10,
            fail_count=0,
            create_time=now,
        )

    def _add_complete_metric_window(self, db, source_id, now):
        db.add(ObservabilityCurrentMetric(
            source_type="prometheus",
            source_id=source_id,
            instance_id=1,
            host_id="host-1",
            standard_metric="host.cpu.used_pct",
            metric_value=20,
            metric_unit="percent",
            collect_time=now,
            status="ok",
        ))
        for minute in range(10):
            db.add(ObservabilityMetricHistory(
                source_type="prometheus",
                source_id=source_id,
                instance_id=1,
                host_id="host-1",
                standard_metric="host.cpu.used_pct",
                metric_value=20 + minute,
                metric_unit="percent",
                metric_time=now - timedelta(minutes=minute),
            ))

    def test_missing_metrics_are_never_reported_healthy(self):
        now = datetime(2026, 8, 23, 12, 0, 0)
        with self.Session() as db:
            source = self._source()
            db.add(source)
            db.commit()
            result = data_quality_service.evaluate_data_quality(db, now=now)

        self.assertEqual(result["overall_status"], "missing")
        self.assertEqual(result["items"][0]["status"], "missing")
        self.assertEqual(result["items"][0]["freshness_status"], "missing")
        self.assertEqual(result["items"][0]["score"], 0)

    def test_fresh_complete_samples_are_healthy_and_snapshot_is_persisted(self):
        now = datetime(2026, 8, 23, 12, 0, 0)
        with self.Session() as db:
            source = self._source()
            db.add(source)
            db.flush()
            data_quality_service.upsert_policy(db, {
                "source_id": source.id,
                "policy_name": "Prometheus 严格策略",
                "freshness_warn_seconds": 120,
                "freshness_critical_seconds": 300,
                "completeness_window_minutes": 10,
                "min_completeness_pct": 90,
                "max_clock_skew_seconds": 30,
                "cardinality_budget": 100,
                "hot_retention_days": 1,
                "warm_retention_days": 7,
                "archive_retention_days": 30,
                "anomaly_sensitivity": 3,
                "enabled": 1,
            })
            self._add_complete_metric_window(db, source.id, now)
            db.add(self._success_task(source.id, now))
            db.commit()

            result = data_quality_service.evaluate_data_quality(db, now=now, persist=True)
            snapshot = db.query(ObservabilityDataQualitySnapshot).one()

        item = result["items"][0]
        self.assertEqual(item["status"], "healthy")
        self.assertEqual(item["completeness_pct"], 100.0)
        self.assertTrue(item["baseline_ready"] is False)
        self.assertEqual(snapshot.status, "healthy")
        self.assertEqual(int(snapshot.score), 100)

    def test_stale_failure_clock_skew_and_cardinality_are_distinguished(self):
        now = datetime(2026, 8, 23, 12, 0, 0)
        with self.Session() as db:
            stale = self._source("stale")
            skewed = self._source("skewed")
            db.add_all([stale, skewed])
            db.flush()
            db.add(ObservabilityCurrentMetric(
                source_type="prometheus",
                source_id=stale.id,
                standard_metric="host.cpu.used_pct",
                metric_value=20,
                collect_time=now - timedelta(hours=1),
                status="ok",
            ))
            db.add(ObservabilityCurrentMetric(
                source_type="prometheus",
                source_id=skewed.id,
                standard_metric="host.cpu.used_pct",
                metric_value=20,
                collect_time=now + timedelta(minutes=5),
                status="ok",
            ))
            db.add(ObservabilityCollectTask(
                source_id=skewed.id,
                task_status="failed",
                error_message="connection refused",
                create_time=now,
            ))
            db.commit()
            result = data_quality_service.evaluate_data_quality(db, now=now)

        by_name = {item["source_name"]: item for item in result["items"]}
        self.assertEqual(by_name["stale"]["status"], "stale")
        self.assertEqual(by_name["skewed"]["status"], "degraded")
        self.assertEqual(by_name["skewed"]["clock_skew_seconds"], 300)
        self.assertEqual(by_name["skewed"]["task_status"], "failed")

    def test_retention_requires_explicit_confirmation_before_delete(self):
        now = datetime(2026, 8, 23, 12, 0, 0)
        with self.Session() as db:
            source = self._source()
            db.add(source)
            db.flush()
            data_quality_service.upsert_policy(db, {
                "source_id": source.id,
                "policy_name": "短保留测试",
                "freshness_warn_seconds": 60,
                "freshness_critical_seconds": 120,
                "completeness_window_minutes": 10,
                "min_completeness_pct": 80,
                "max_clock_skew_seconds": 60,
                "cardinality_budget": 100,
                "hot_retention_days": 1,
                "warm_retention_days": 2,
                "archive_retention_days": 3,
                "anomaly_sensitivity": 3,
                "enabled": 1,
            })
            db.add_all([
                ObservabilityMetricHistory(
                    source_type="prometheus",
                    source_id=source.id,
                    standard_metric="old.metric",
                    metric_value=1,
                    metric_time=now - timedelta(days=5),
                ),
                ObservabilityMetricHistory(
                    source_type="prometheus",
                    source_id=source.id,
                    standard_metric="new.metric",
                    metric_value=1,
                    metric_time=now - timedelta(days=1),
                ),
            ])
            db.commit()

            preview = data_quality_service.run_retention(db, source_id=source.id, now=now)
            self.assertEqual(preview["expired_sample_count"], 1)
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                data_quality_service.run_retention(db, source_id=source.id, dry_run=False, now=now)
            executed = data_quality_service.run_retention(
                db,
                source_id=source.id,
                dry_run=False,
                confirmation="DELETE_EXPIRED_OBSERVABILITY",
                now=now,
            )
            remaining = db.query(ObservabilityMetricHistory).all()

        self.assertEqual(executed["deleted_count"], 1)
        self.assertEqual([row.standard_metric for row in remaining], ["new.metric"])

    def test_api_exposes_overview_policy_evaluation_and_history(self):
        with self.Session() as db:
            source = self._source()
            db.add(source)
            db.commit()
            source_id = source.id

        app = FastAPI()
        app.include_router(observability.router, prefix="/api/v1")

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)
        overview = client.get("/api/v1/observability/data-quality")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["items"][0]["status"], "missing")
        policy = client.put("/api/v1/observability/data-quality/policies", json={
            "policy_name": "源策略",
            "source_id": source_id,
            "freshness_warn_seconds": 120,
            "freshness_critical_seconds": 600,
            "completeness_window_minutes": 60,
            "min_completeness_pct": 90,
            "max_clock_skew_seconds": 60,
            "cardinality_budget": 5000,
            "hot_retention_days": 7,
            "warm_retention_days": 30,
            "archive_retention_days": 180,
            "anomaly_sensitivity": 3,
            "enabled": 1,
        })
        self.assertEqual(policy.status_code, 200)
        evaluated = client.post("/api/v1/observability/data-quality/evaluate", json={"persist": True})
        self.assertEqual(evaluated.status_code, 200)
        history = client.get(f"/api/v1/observability/data-quality/history?source_id={source_id}")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
