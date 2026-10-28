import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.external_ecosystem import _moved_to_domain, router
from app.core.database import Base, get_db
from app.models.external_ecosystem import EcosystemIntegrationConfig, EcosystemIntegrationEvent, EcosystemIntegrationPlan
from app.services import external_ecosystem


class EcosystemLegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        for number in range(51, 61):
            task_code = f"T-{number:03d}"
            self.db.add(EcosystemIntegrationConfig(
                task_code=task_code,
                integration_type="legacy",
                config_name=f"legacy-{task_code}",
            ))
            self.db.add(EcosystemIntegrationEvent(
                task_code=task_code,
                integration_type="legacy",
                event_type="audit",
                status="success",
            ))
            self.db.add(EcosystemIntegrationPlan(
                task_code=task_code,
                integration_type="legacy",
                plan_name=f"legacy-{task_code}",
                plan_type="implementation",
            ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_migration_is_idempotent_and_never_deletes_legacy_rows(self):
        first = external_ecosystem.migrate_legacy_records(self.db)
        counts_after_first = (
            self.db.query(EcosystemIntegrationConfig).count(),
            self.db.query(EcosystemIntegrationEvent).count(),
            self.db.query(EcosystemIntegrationPlan).count(),
        )
        second = external_ecosystem.migrate_legacy_records(self.db)
        counts_after_second = (
            self.db.query(EcosystemIntegrationConfig).count(),
            self.db.query(EcosystemIntegrationEvent).count(),
            self.db.query(EcosystemIntegrationPlan).count(),
        )

        self.assertTrue(first["applied"])
        self.assertFalse(first["already_applied"])
        self.assertTrue(second["already_applied"])
        self.assertEqual(counts_after_first, counts_after_second)
        self.assertEqual(counts_after_first, (10, 11, 10))
        self.assertEqual(first["summary"]["record_count"], 30)
        self.assertEqual(first["summary"]["deleted_count"], 0)

    def test_legacy_rows_are_read_only_and_point_to_the_domain_owner(self):
        config = external_ecosystem.list_configs(self.db, task_code="T-053")[0]
        event = external_ecosystem.list_events(self.db, task_code="T-054")[0]
        plan = external_ecosystem.list_plans(self.db, task_code="T-058")[0]

        for item in (config, event, plan):
            self.assertTrue(item["legacy"])
            self.assertTrue(item["read_only"])
            self.assertTrue(item["owner"]["route_key"])
            self.assertTrue(item["deprecation"]["deprecated"])

        row = self.db.query(EcosystemIntegrationConfig).filter_by(task_code="T-053").one()
        with self.assertRaisesRegex(ValueError, "只读历史"):
            external_ecosystem.delete_config(self.db, row)
        self.assertEqual(self.db.query(EcosystemIntegrationConfig).count(), 10)

    def test_legacy_action_endpoint_returns_owning_route(self):
        with self.assertRaises(HTTPException) as raised:
            _moved_to_domain("T-060")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["route_key"], "monitor.observability")
        self.assertEqual(raised.exception.detail["section"], "apmTrace")

    def _client(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        return TestClient(app)

    def test_retired_actions_keep_conflicts_without_writes_or_delivery(self):
        cases = [
            ("/events", {"task_code": "T-051", "integration_type": "webhook", "event_type": "audit"}, "T-051"),
            ("/notifications/dispatch", {"task_code": "T-051", "title": "legacy"}, "T-051"),
            ("/tickets/create", {"task_code": "T-052", "title": "legacy"}, "T-052"),
            ("/grafana/dashboard", {}, "T-055"),
            ("/cmdb/sync-plan", {}, "T-053"),
            ("/logs/query-plan", {}, "T-054"),
            ("/backup/platform-plan", {}, "T-056"),
            ("/object-storage/archive-plan", {}, "T-057"),
            ("/automation/approval-plan", {}, "T-058"),
            ("/identity/mapping-plan", {}, "T-059"),
            ("/apm/trace-plan", {}, "T-060"),
        ]
        cases += [(f"/plans/T-{number:03d}", {}, f"T-{number:03d}") for number in range(51, 61)]
        with self._client() as client, patch("requests.sessions.Session.request") as http, patch("smtplib.SMTP") as smtp:
            for path, payload, task in cases:
                for execute in (False, True):
                    with self.subTest(path=path, execute=execute):
                        response = client.post(f"/ecosystem{path}", json={**payload, "execute": execute})
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(response.json()["detail"], external_ecosystem.deprecation_for_task(task))
            http.assert_not_called()
            smtp.assert_not_called()
        self.assertEqual(self.db.query(EcosystemIntegrationConfig).count(), 10)
        self.assertEqual(self.db.query(EcosystemIntegrationEvent).count(), 10)
        self.assertEqual(self.db.query(EcosystemIntegrationPlan).count(), 10)

    def test_legacy_http_validation_and_history_remain_available(self):
        with self._client() as client:
            unknown = client.post("/ecosystem/events", json={
                "task_code": "T-999", "integration_type": "webhook", "event_type": "audit",
            })
            self.assertEqual(unknown.status_code, 400)
            self.assertEqual(client.post("/ecosystem/notifications/dispatch", json={}).status_code, 422)
            unknown_plan = client.post("/ecosystem/plans/T-999", json={})
            self.assertEqual(unknown_plan.status_code, 409)
            self.assertTrue(unknown_plan.json()["detail"]["deprecated"])
            for resource in ("configs", "events", "plans"):
                with self.subTest(resource=resource):
                    response = client.get(f"/ecosystem/{resource}", params={"task_code": "T-053"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(len(response.json()["items"]), 1)
                    self.assertTrue(response.json()["items"][0]["legacy"])


if __name__ == "__main__":
    unittest.main()
