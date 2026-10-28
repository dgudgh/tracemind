import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.external_ecosystem import (
    EcosystemIntegrationConfig,
    EcosystemIntegrationEvent,
    EcosystemIntegrationPlan,
)
from app.services.ecosystem_registry import (
    CAPABILITY_REGISTRY,
    _health_status,
    _newest,
    get_capability,
    summarize_capabilities,
)
from app.services.external_ecosystem import overview as legacy_overview


class EcosystemRegistryTest(unittest.TestCase):
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

    def test_every_task_has_domain_owner_and_route(self):
        self.assertEqual(set(CAPABILITY_REGISTRY), {f"T-{number:03d}" for number in range(51, 61)})
        for task_code, item in CAPABILITY_REGISTRY.items():
            self.assertEqual(item["task_code"], task_code)
            self.assertTrue(item["domain"])
            self.assertTrue(item["route_key"])
            self.assertIn(
                item["status"],
                {"planned", "configured", "verified", "active", "degraded", "disabled"},
            )

    def test_get_capability_returns_none_for_unknown_task(self):
        self.assertEqual(get_capability("T-051")["route_key"], "integration.notify")
        self.assertIsNone(get_capability("T-999"))

    def test_builtin_grafana_templates_are_available_without_external_source(self):
        with self.Session() as db:
            grafana = next(
                item for item in summarize_capabilities(db)
                if item["task_code"] == "T-055"
            )

        self.assertEqual(grafana["configured_count"], 1)
        self.assertEqual(grafana["active_count"], 1)
        self.assertEqual(grafana["health_status"], "configured")

    def test_health_time_comparison_handles_mixed_timezone_values(self):
        naive_success = datetime(2026, 7, 20, 8, 0, 0)
        aware_failure = datetime(2026, 7, 20, 8, 1, 0, tzinfo=timezone.utc)
        definition = CAPABILITY_REGISTRY["T-051"]

        self.assertEqual(_newest(naive_success, aware_failure), aware_failure)
        self.assertEqual(
            _health_status(
                definition,
                {
                    "configured_count": 1,
                    "active_count": 1,
                    "last_success_time": naive_success,
                    "last_failure_time": aware_failure,
                },
                0,
            ),
            "degraded",
        )

    def test_summary_combines_legacy_counts_without_losing_owner(self):
        with self.Session() as db:
            db.add(
                EcosystemIntegrationConfig(
                    task_code="T-051",
                    integration_type="webhook",
                    config_name="primary",
                )
            )
            db.add(
                EcosystemIntegrationEvent(
                    task_code="T-051",
                    integration_type="webhook",
                    event_type="notification",
                    status="failed",
                )
            )
            db.add(
                EcosystemIntegrationEvent(
                    task_code="T-051",
                    integration_type="webhook",
                    event_type="notification",
                    status="success",
                )
            )
            db.add(
                EcosystemIntegrationPlan(
                    task_code="T-051",
                    integration_type="notification",
                    plan_name="legacy",
                    plan_type="notify",
                )
            )
            db.commit()

            items = summarize_capabilities(db)
            legacy_items = legacy_overview(db)["items"]

        notify = next(item for item in items if item["task_code"] == "T-051")
        self.assertEqual(notify["route_key"], "integration.notify")
        self.assertEqual(notify["config_count"], 1)
        self.assertEqual(notify["event_count"], 2)
        self.assertEqual(notify["failure_count"], 1)
        self.assertEqual(notify["plan_count"], 1)
        self.assertIsNotNone(notify["latest_success"])
        self.assertIsNotNone(notify["latest_failure"])
        self.assertEqual(legacy_items, items)


if __name__ == "__main__":
    unittest.main()
