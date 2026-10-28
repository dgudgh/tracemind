import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import config_governance as config_governance_api
from app.automation.service import AutomationService
from app.core.database import Base, get_db
from app.models.config_governance import (
    DBConfigBaseline,
    DBConfigBaselineItem,
    DBConfigDrift,
    DBConfigRemediation,
    DBConfigSnapshot,
)
from app.models.db_instance import DBInstance
from app.services import config_governance


class ConfigGovernanceTest(unittest.TestCase):
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

    def _instance(self, db, db_type="mysql", name="mesdb"):
        row = DBInstance(
            instance_name=name,
            db_type=db_type,
            db_version={"mysql": "8.0.44", "oracle": "19.3"}.get(db_type, "16.7"),
            host="10.0.0.10",
            port={"mysql": 3306, "oracle": 1521, "postgresql": 5432, "sqlserver": 1433}[db_type],
            username="monitor",
            password_cipher="env://TEST_PASSWORD",
            enabled=1,
        )
        db.add(row)
        db.flush()
        return row

    def _baseline(self, db, instance, restart_required=0):
        baseline = DBConfigBaseline(
            name=f"{instance.db_type}-production",
            db_type=instance.db_type,
            version_pattern="*",
            status="active",
            is_default=1,
            created_by="tester",
        )
        db.add(baseline)
        db.flush()
        db.add_all([
            DBConfigBaselineItem(
                baseline_id=baseline.id,
                category="parameter",
                parameter_name="max_connections",
                desired_value="200",
                comparator="ge",
                value_type="integer",
                severity="critical" if restart_required else "warning",
                restart_required=restart_required,
            ),
            DBConfigBaselineItem(
                baseline_id=baseline.id,
                category="parameter",
                parameter_name="slow_query_log",
                desired_value="true",
                comparator="eq",
                value_type="boolean",
                severity="warning",
            ),
            DBConfigBaselineItem(
                baseline_id=baseline.id,
                category="log_retention",
                parameter_name="retention_days",
                desired_value="14",
                comparator="ge",
                value_type="integer",
                severity="warning",
                remediation_hint="调整日志保留策略",
            ),
        ])
        db.commit()
        return baseline

    def _client(self):
        app = FastAPI()
        app.include_router(config_governance_api.router, prefix="/api/v1")

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        return TestClient(app)

    def test_manual_evaluation_tracks_drift_and_resolves_it_on_recheck(self):
        with self.Session() as db:
            instance = self._instance(db)
            baseline = self._baseline(db, instance)
            snapshot = config_governance.evaluate_baseline(
                db,
                baseline,
                instance,
                actor="tester",
                observed_parameters={
                    "max_connections": "100",
                    "slow_query_log": "ON",
                    "log_retention": {"retention_days": 7},
                },
            )
            self.assertEqual(snapshot.status, "success")
            self.assertEqual(snapshot.item_count, 3)
            self.assertEqual(snapshot.compliant_count, 1)
            self.assertEqual(snapshot.drift_count, 2)
            self.assertEqual(db.query(DBConfigDrift).filter(DBConfigDrift.status == "open").count(), 2)

            second = config_governance.evaluate_baseline(
                db,
                baseline,
                instance,
                actor="tester",
                observed_parameters={
                    "max_connections": 300,
                    "slow_query_log": True,
                    "log_retention": {"retention_days": 30},
                },
            )
            self.assertEqual(second.compliant_count, 3)
            self.assertEqual(second.drift_count, 0)
            self.assertEqual(db.query(DBConfigDrift).filter(DBConfigDrift.status == "resolved").count(), 2)

    def test_live_collection_failure_is_not_reported_as_configuration_drift(self):
        with self.Session() as db:
            instance = self._instance(db, db_type="oracle")
            baseline = DBConfigBaseline(name="oracle-live", db_type="oracle", status="active")
            db.add(baseline)
            db.flush()
            db.add(DBConfigBaselineItem(
                baseline_id=baseline.id,
                parameter_name="open_cursors",
                desired_value="500",
                value_type="integer",
            ))
            db.commit()
            with patch.object(config_governance, "collect_live_parameters", side_effect=RuntimeError("connection refused")):
                snapshot = config_governance.evaluate_baseline(
                    db, baseline, instance, actor="tester", source="live"
                )
            self.assertEqual(snapshot.status, "failed")
            self.assertIn("connection refused", snapshot.collector_error)
            self.assertEqual(snapshot.drift_count, 0)
            self.assertEqual(db.query(DBConfigDrift).count(), 0)

    def test_remediation_uses_safe_plan_approval_apply_and_live_verification(self):
        with self.Session() as db:
            instance = self._instance(db)
            baseline = self._baseline(db, instance)
            config_governance.evaluate_baseline(
                db,
                baseline,
                instance,
                actor="tester",
                observed_parameters={
                    "max_connections": "100",
                    "slow_query_log": "ON",
                    "log_retention": {"retention_days": 30},
                },
            )
            drift = db.query(DBConfigDrift).filter(DBConfigDrift.parameter_name == "max_connections").one()
            remediation = config_governance.create_remediation(
                db,
                baseline,
                instance,
                actor="requester",
                drift_ids=[drift.id],
            )
            self.assertIn("SET PERSIST max_connections = 200;", remediation.script)
            self.assertIn("SET PERSIST max_connections = 100;", remediation.rollback_script)
            self.assertEqual(remediation.script_sha256, hashlib.sha256(remediation.script.encode()).hexdigest())

            remediation = config_governance.submit_remediation(
                db, remediation, instance, actor="requester"
            )
            request = AutomationService(db).approve(remediation.approval_request_id, "approver", "reviewed")
            self.assertEqual(request.status, "approved")

            with patch.object(config_governance, "_execute_parameter_operations", return_value=["applied"]):
                remediation = config_governance.apply_remediation(
                    db, remediation.id, instance, actor="operator", confirm=True
                )
            self.assertEqual(remediation.status, "pending_verification")

            remediation, snapshot = config_governance.verify_remediation(
                db,
                remediation,
                baseline,
                instance,
                actor="operator",
                observed_parameters={
                    "max_connections": 200,
                    "slow_query_log": True,
                    "log_retention": {"retention_days": 30},
                },
            )
            self.assertEqual(snapshot.status, "success")
            self.assertEqual(remediation.status, "verified")
            self.assertEqual(db.get(DBConfigDrift, drift.id).status, "resolved")

    def test_high_risk_remediation_requires_maintenance_window(self):
        with self.Session() as db:
            instance = self._instance(db)
            baseline = self._baseline(db, instance, restart_required=1)
            config_governance.evaluate_baseline(
                db,
                baseline,
                instance,
                actor="tester",
                observed_parameters={
                    "max_connections": 100,
                    "slow_query_log": True,
                    "log_retention": {"retention_days": 30},
                },
            )
            drift = db.query(DBConfigDrift).filter(DBConfigDrift.parameter_name == "max_connections").one()
            remediation = config_governance.create_remediation(
                db, baseline, instance, actor="requester", drift_ids=[drift.id]
            )
            self.assertEqual(remediation.risk_level, "high")
            with self.assertRaisesRegex(ValueError, "维护窗口"):
                config_governance.submit_remediation(db, remediation, instance, actor="requester")

            start = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
            remediation = config_governance.submit_remediation(
                db,
                remediation,
                instance,
                actor="requester",
                maintenance_window_start=start,
                maintenance_window_end=start + timedelta(hours=1),
            )
            self.assertEqual(remediation.status, "pending_approval")

    def test_api_exposes_all_baselines_and_explicit_failed_snapshot_status(self):
        with self.Session() as db:
            instance = self._instance(db, db_type="oracle")
            instance_id = instance.id
            db.commit()
        client = self._client()
        created = client.post("/api/v1/operations/config-governance/baselines", json={
            "name": "Oracle 19c production",
            "db_type": "oracle",
            "version_pattern": "19*",
            "status": "active",
            "items": [{
                "parameter_name": "open_cursors",
                "desired_value": "500",
                "value_type": "integer",
                "comparator": "ge",
            }],
        })
        self.assertEqual(created.status_code, 201, created.text)
        baseline_id = created.json()["id"]
        listed = client.get("/api/v1/operations/config-governance/baselines")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 1)
        with patch.object(config_governance, "collect_live_parameters", side_effect=RuntimeError("network down")):
            checked = client.post(
                f"/api/v1/operations/config-governance/baselines/{baseline_id}/evaluate",
                json={"instance_id": instance_id},
            )
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "failed")
        self.assertEqual(checked.json()["drift_count"], 0)


if __name__ == "__main__":
    unittest.main()
