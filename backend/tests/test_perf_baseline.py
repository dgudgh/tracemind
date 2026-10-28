import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.perf_metric import DBPerfMetric
from app.services import perf_baseline


class PerformanceBaselineTest(unittest.TestCase):
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

    def _instance(self, db, db_type):
        row = DBInstance(
            instance_name=f"{db_type}-baseline",
            db_type=db_type,
            host="127.0.0.1",
            port={"oracle": 1521, "mysql": 3306, "postgresql": 5432, "sqlserver": 1433}[db_type],
            username="monitor",
            password_cipher="encrypted-placeholder",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def test_four_engines_dispatch_to_live_collectors_and_persist_metrics(self):
        collector_by_engine = {
            "oracle": "_collect_oracle",
            "mysql": "_collect_mysql",
            "postgresql": "_collect_postgresql",
            "sqlserver": "_collect_sqlserver",
        }
        with self.Session() as db:
            for engine, collector_name in collector_by_engine.items():
                instance = self._instance(db, engine)
                with patch.object(perf_baseline, collector_name, return_value={"qps": 12.5, "aas": 3.0}) as collector:
                    result = perf_baseline.collect_perf_metrics(db, instance)
                collector.assert_called_once_with(instance)
                self.assertEqual(result["count"], 2)
                self.assertEqual(result["warnings"], [])
                self.assertEqual(
                    db.query(DBPerfMetric).filter(DBPerfMetric.instance_id == instance.id).count(),
                    2,
                )

    def test_connection_failure_returns_error_and_does_not_create_zero_metrics(self):
        with self.Session() as db:
            instance = self._instance(db, "postgresql")
            with patch.object(perf_baseline, "_collect_postgresql", side_effect=RuntimeError("password authentication failed")):
                result = perf_baseline.collect_perf_metrics(db, instance)
            self.assertEqual(result["count"], 0)
            self.assertIn("password authentication failed", result["errors"][0])
            self.assertEqual(db.query(DBPerfMetric).count(), 0)

    def test_baseline_calculates_current_average_and_deviation_for_new_metrics(self):
        with self.Session() as db:
            instance = self._instance(db, "sqlserver")
            with patch.object(perf_baseline, "_collect_sqlserver", return_value={"cache_hit_pct": 98.0, "active_sessions": 4}):
                perf_baseline.collect_perf_metrics(db, instance)
            with patch.object(perf_baseline, "_collect_sqlserver", return_value={"cache_hit_pct": 94.0, "active_sessions": 8}):
                perf_baseline.collect_perf_metrics(db, instance)
            result = perf_baseline.compute_baseline(db, instance.id)
            items = {row["metric"]: row for row in result["items"]}
            self.assertEqual(items["cache_hit_pct"]["label"], "缓存命中率%")
            self.assertEqual(items["active_sessions"]["current"], 8.0)
            self.assertEqual(items["active_sessions"]["day_avg"], 6.0)
            self.assertEqual(items["active_sessions"]["deviation_pct"], 33.3)


if __name__ == "__main__":
    unittest.main()
