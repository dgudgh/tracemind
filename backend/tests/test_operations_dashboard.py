import unittest
import hashlib
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import system
from app.automation.models import ApprovalRequest, AutomationPlan
from app.core.database import Base, get_db
from app.models.alert_event import DBAlertEvent
from app.models.backup_operation import BackupOperation
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_diagnosis import DBInspectionTask
from app.models.report_center import ReportArtifact
from app.services import operations_dashboard


class OperationsDashboardTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.now = datetime(2026, 8, 23, 12, 0, 0)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _instance(self, db, name, db_type, host):
        row = DBInstance(
            instance_name=name,
            db_type=db_type,
            host=host,
            port={"oracle": 1521, "mysql": 3306}[db_type],
            username="monitor",
            password_cipher="env://TEST_PASSWORD",
            enabled=1,
        )
        db.add(row)
        db.flush()
        return row

    def _native(self, db, instance, task_id, state="success", at=None):
        # Store real findings; do not stub the production task-summary function.
        states = ["success", "failed"] if state == "partial" else [state]
        for execution in states:
            db.add(DBCheckResult(
                task_id=task_id, instance_id=instance.id, db_type=instance.db_type,
                check_category="availability", check_item="database query",
                check_status="normal", metric_name="session_count", metric_value="12",
                raw_data={"execution_status": execution},
                create_time=at if at is not None else self.now - timedelta(minutes=10),
            ))

    def _legacy(self, db, instance, task_id, at=None):
        db.add(DBInspectionTask(
            task_id=task_id, instance_id=instance.id, db_type=instance.db_type,
            overall_score=100, risk_level="healthy",
            create_time=at if at is not None else self.now - timedelta(minutes=5),
        ))

    def _positive_other_slos(self, db, instance):
        db.add_all([
            DBAlertEvent(
                event_uid=f"resolved-{instance.id}", source="manual", instance_id=instance.id,
                severity="warning", status="resolved", title="resolved test alert",
                first_seen=self.now - timedelta(minutes=30), last_seen=self.now,
                acknowledged_at=self.now - timedelta(minutes=25),
                resolved_at=self.now - timedelta(minutes=10), create_time=self.now,
            ),
            BackupOperation(
                operation_no=f"backup-{instance.id}", instance_id=instance.id,
                engine=instance.db_type, category="backup", operation_type="full",
                status="success", finished_at=self.now - timedelta(minutes=5),
                create_time=self.now - timedelta(minutes=10),
            ),
        ])

    def _inspection_dashboard(self, state=None, at=None, *, legacy=False, previous_success=False):
        with self.Session() as db:
            instance = self._instance(db, "tested", "oracle", "127.0.0.1")
            self._positive_other_slos(db, instance)
            if previous_success:
                self._native(db, instance, "previous-success", at=self.now - timedelta(hours=1))
            if state is not None:
                self._native(db, instance, "native-task", state, at)
            if legacy:
                self._legacy(db, instance, "legacy-task")
            db.commit()
            return operations_dashboard.build_operations_dashboard(db, now=self.now)

    def _assert_unverified(self, result, status):
        risk = result["risk_ranking"][0]
        self.assertEqual(risk["inspection_status"], status)
        self.assertIsNone(risk["latest_inspection_score"])
        self.assertEqual(risk["risk_level"], "unknown")
        self.assertNotEqual(result["platform_status"], "healthy")
        inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
        self.assertNotEqual(inspections["status"], "healthy")

    def test_fresh_native_success_supplies_real_score_and_health(self):
        result = self._inspection_dashboard("success")
        risk = result["risk_ranking"][0]
        self.assertEqual(risk["inspection_status"], "success")
        self.assertEqual(risk["latest_inspection_score"], 100)
        self.assertEqual(risk["latest_inspection_at"], self.now - timedelta(minutes=10))
        self.assertEqual(risk["risk_level"], "healthy")
        self.assertEqual(result["platform_status"], "healthy")
        inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
        self.assertEqual(inspections["value"], 1)
        self.assertEqual(inspections["trace"]["model"], "db_check_result")

    def test_no_collected_records_do_not_supply_health(self):
        self._assert_unverified(self._inspection_dashboard(), "not_collected")

    def test_legacy_only_records_are_history_not_collected_health(self):
        result = self._inspection_dashboard(legacy=True)
        self._assert_unverified(result, "not_collected")
        inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
        self.assertEqual(inspections["value"], 1)
        coverage = next(item for item in result["slo_cards"] if item["key"] == "inspection_coverage")
        self.assertEqual(coverage["actual"], 100.0)  # Attempt coverage is not a health claim.

    def test_failed_native_task_never_gets_a_health_score(self):
        self._assert_unverified(self._inspection_dashboard("failed"), "failed")

    def test_mixed_native_findings_are_partial_without_a_health_score(self):
        self._assert_unverified(self._inspection_dashboard("partial"), "partial")

    def test_stale_native_success_cannot_keep_platform_healthy(self):
        self._assert_unverified(
            self._inspection_dashboard("success", self.now - timedelta(hours=25)), "stale",
        )

    def test_native_history_outside_seven_days_is_not_a_recent_attempt(self):
        result = self._inspection_dashboard("success", self.now - timedelta(days=8))
        self._assert_unverified(result, "stale")
        inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
        self.assertEqual(inspections["value"], 0)
        self.assertEqual(inspections["secondary"], "覆盖 0/1")

    def test_future_native_success_is_not_replaced_by_previous_success(self):
        self._assert_unverified(
            self._inspection_dashboard("success", self.now + timedelta(microseconds=1), previous_success=True),
            "future",
        )

    def test_newer_legacy_record_does_not_replace_native_execution_evidence(self):
        result = self._inspection_dashboard("failed", legacy=True)
        self._assert_unverified(result, "failed")
        self.assertEqual(result["risk_ranking"][0]["latest_inspection_at"], self.now - timedelta(minutes=10))
        self.assertEqual(next(item for item in result["kpis"] if item["key"] == "inspections")["value"], 2)

    def test_latest_native_failure_does_not_fall_back_to_old_success(self):
        result = self._inspection_dashboard("failed", previous_success=True)
        self._assert_unverified(result, "failed")

    def test_native_runtime_summary_alone_is_not_collected_evidence(self):
        with self.Session() as db:
            instance = self._instance(db, "summary-only", "oracle", "127.0.0.1")
            self._positive_other_slos(db, instance)
            db.add(DBCheckResult(
                task_id="summary-only", instance_id=instance.id, db_type="oracle",
                metric_name="executed_sections", check_status="normal",
                raw_data={"execution_status": "success", "evidence_kind": "execution_summary"},
                create_time=self.now,
            ))
            db.commit()
            result = operations_dashboard.build_operations_dashboard(db, now=self.now)
        self._assert_unverified(result, "failed")

    def test_native_freshness_boundaries_use_the_injected_clock(self):
        with self.Session() as db:
            instance = self._instance(db, "boundary", "oracle", "127.0.0.1")
            for at, status in (
                (self.now - timedelta(hours=24), "success"),
                (self.now, "success"),
                (self.now - timedelta(hours=24, microseconds=1), "stale"),
                (self.now + timedelta(microseconds=1), "future"),
            ):
                with self.subTest(at=at):
                    db.query(DBCheckResult).delete()
                    self._native(db, instance, "boundary-task", at=at)
                    db.commit()
                    result = operations_dashboard.build_operations_dashboard(db, now=self.now)
                    risk = result["risk_ranking"][0]
                    self.assertEqual(risk["inspection_status"], status)
                    self.assertEqual(risk["risk_level"], "healthy" if status == "success" else "unknown")
                    self.assertEqual(risk["latest_inspection_score"], 100 if status == "success" else None)

    def test_native_legacy_same_task_is_counted_once_and_native_wins(self):
        with self.Session() as db:
            instance = self._instance(db, "dedup", "oracle", "127.0.0.1")
            for state in ("success", "partial", "failed"):
                with self.subTest(state=state):
                    db.query(DBCheckResult).delete()
                    db.query(DBInspectionTask).delete()
                    self._native(db, instance, "shared-task", state)
                    self._legacy(db, instance, "shared-task", self.now + timedelta(days=1))
                    db.commit()
                    result = operations_dashboard.build_operations_dashboard(db, now=self.now)
                    risk = result["risk_ranking"][0]
                    self.assertEqual(risk["inspection_status"], state)
                    self.assertEqual(risk["latest_inspection_score"], 100 if state == "success" else None)
                    inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
                    self.assertEqual(inspections["value"], 1)

    def test_native_scope_does_not_mix_same_task_ids_across_instances(self):
        with self.Session() as db:
            visible = self._instance(db, "visible", "oracle", "127.0.0.1")
            hidden = self._instance(db, "hidden", "oracle", "127.0.0.2")
            self._native(db, visible, "same-task", "success")
            self._native(db, hidden, "same-task", "failed")
            self._legacy(db, hidden, "hidden-legacy")
            db.commit()
            unscoped = operations_dashboard.build_operations_dashboard(db, now=self.now)
            self.assertEqual(next(item for item in unscoped["kpis"] if item["key"] == "inspections")["value"], 3)
            self.assertEqual(
                {item["instance_name"]: item["inspection_status"] for item in unscoped["risk_ranking"]},
                {"visible": "success", "hidden": "failed"},
            )
            for scope, count in (({visible.id}, 1), (set(), 0), ({999999}, 0)):
                with self.subTest(scope=scope):
                    result = operations_dashboard.build_operations_dashboard(db, now=self.now, allowed_instance_ids=scope)
                    self.assertEqual(result["instance_count"], count)
                    self.assertEqual(len(result["risk_ranking"]), count)
                    inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
                    self.assertEqual(inspections["value"], count)
                    self.assertEqual(inspections["trace"]["scope_instance_ids"], [visible.id] if count else [])
                    if count:
                        risk = result["risk_ranking"][0]
                        self.assertEqual(risk["instance_name"], "visible")
                        self.assertEqual(risk["inspection_status"], "success")
                        self.assertEqual(risk["latest_inspection_score"], 100)

    def test_disabled_instances_cannot_push_attempt_coverage_over_100_percent(self):
        with self.Session() as db:
            enabled = self._instance(db, "enabled", "oracle", "127.0.0.1")
            disabled = self._instance(db, "disabled", "oracle", "127.0.0.2")
            disabled.enabled = 0
            self._native(db, enabled, "enabled-task")
            self._native(db, disabled, "disabled-task")
            db.commit()
            result = operations_dashboard.build_operations_dashboard(db, now=self.now)
        coverage = next(item for item in result["slo_cards"] if item["key"] == "inspection_coverage")
        self.assertEqual(coverage["actual"], 100.0)
        inspections = next(item for item in result["kpis"] if item["key"] == "inspections")
        self.assertEqual(inspections["value"], 2)
        self.assertEqual(inspections["secondary"], "覆盖 1/1")

    def test_platform_health_includes_instances_beyond_the_top_ten(self):
        with self.Session() as db:
            for index in range(11):
                instance = self._instance(db, f"instance-{index:02d}", "oracle", "127.0.0.1")
                self._native(db, instance, f"native-{index}", "failed" if index == 10 else "success")
                if index == 0:
                    self._positive_other_slos(db, instance)
            db.commit()
            result = operations_dashboard.build_operations_dashboard(db, now=self.now)
        self.assertEqual(result["instance_count"], 11)
        self.assertEqual(len(result["risk_ranking"]), 10)
        self.assertTrue(all(item["risk_level"] == "healthy" for item in result["risk_ranking"]))
        self.assertTrue(all(item["status"] == "met" for item in result["slo_cards"]))
        self.assertEqual(result["platform_status"], "warning")

    def test_empty_samples_stay_unknown_instead_of_claiming_full_slo(self):
        with self.Session() as db:
            result = operations_dashboard.build_operations_dashboard(db, now=self.now)

        self.assertEqual(result["platform_status"], "warning")
        self.assertTrue(all(item["status"] == "unknown" for item in result["slo_cards"]))
        self.assertTrue(all(item["actual"] is None for item in result["slo_cards"]))
        self.assertEqual(result["kpis"][0]["trace"]["model"], "db_instance")

    def test_dashboard_aggregates_traceable_slo_queue_and_risk(self):
        with self.Session() as db:
            instance = self._instance(db, "mesdb", "oracle", "10.0.0.1")
            db.add_all([
                DBAlertEvent(
                    event_uid="active-1",
                    source="manual",
                    instance_id=instance.id,
                    db_type="oracle",
                    severity="critical",
                    status="assigned",
                    title="归档空间不足",
                    first_seen=self.now - timedelta(minutes=20),
                    last_seen=self.now,
                    assigned_to="dba",
                    sla_due_at=self.now - timedelta(minutes=5),
                    sla_breached_at=self.now - timedelta(minutes=4),
                ),
                DBAlertEvent(
                    event_uid="resolved-1",
                    source="manual",
                    instance_id=instance.id,
                    db_type="oracle",
                    severity="warning",
                    status="resolved",
                    title="会话升高",
                    first_seen=self.now - timedelta(minutes=40),
                    last_seen=self.now - timedelta(minutes=10),
                    acknowledged_at=self.now - timedelta(minutes=35),
                    resolved_at=self.now - timedelta(minutes=10),
                ),
                DBInspectionTask(
                    task_id="inspection-1",
                    instance_id=instance.id,
                    db_type="oracle",
                    overall_score=72,
                    risk_level="warning",
                    create_time=self.now - timedelta(days=1),
                ),
                BackupOperation(
                    operation_no="backup-1",
                    instance_id=instance.id,
                    engine="oracle",
                    category="backup",
                    operation_type="full",
                    execution_scope="live",
                    risk_level="low",
                    status="failed",
                    storage_type="local",
                    error_message="RMAN failed",
                    finished_at=self.now - timedelta(hours=1),
                    create_time=self.now - timedelta(hours=2),
                ),
                ApprovalRequest(
                    plan_id=7,
                    requester_id="operator",
                    approver_id="dba",
                    required_approvals=1,
                    status="pending",
                    expires_at=self.now + timedelta(hours=2),
                ),
                ReportArtifact(
                    artifact_uid="artifact-1",
                    source_key="inspection:1",
                    report_type="inspection",
                    source_type="inspection",
                    title="巡检报告",
                    instance_id=instance.id,
                ),
            ])
            db.commit()

            result = operations_dashboard.build_operations_dashboard(db, username="dba", now=self.now)

        self.assertEqual(result["instance_count"], 1)
        self.assertEqual(result["critical_count"], 1)
        self.assertEqual(result["failed_backup_count"], 1)
        self.assertEqual(result["report_artifact_count"], 1)
        self.assertEqual(result["risk_ranking"][0]["risk_level"], "critical")
        self.assertEqual({item["category"] for item in result["work_queue"]}, {"alert", "approval", "backup"})
        coverage = next(item for item in result["slo_cards"] if item["key"] == "inspection_coverage")
        self.assertEqual(coverage["actual"], 100.0)
        self.assertEqual(coverage["status"], "met")
        self.assertEqual(next(item for item in result["kpis"] if item["key"] == "alerts")["detail_page_key"], "unifiedAlerts")

    def test_instance_scope_filters_every_instance_owned_metric(self):
        with self.Session() as db:
            visible = self._instance(db, "visible", "oracle", "10.0.0.1")
            hidden = self._instance(db, "hidden", "mysql", "10.0.0.2")
            script = "echo scoped"
            script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
            visible_plan = AutomationPlan(
                name="visible plan",
                script=script,
                script_sha256=script_hash,
                target_host=visible.host,
                host_whitelist=[visible.host],
                status="pending_approval",
            )
            hidden_plan = AutomationPlan(
                name="hidden plan",
                script=script,
                script_sha256=script_hash,
                target_host=hidden.host,
                host_whitelist=[hidden.host],
                status="pending_approval",
            )
            db.add_all([visible_plan, hidden_plan])
            db.flush()
            db.add_all([
                DBAlertEvent(event_uid="v", source="manual", instance_id=visible.id, severity="warning", status="firing", title="visible"),
                DBAlertEvent(event_uid="h", source="manual", instance_id=hidden.id, severity="critical", status="firing", title="hidden"),
                ApprovalRequest(plan_id=visible_plan.id, requester_id="operator", approver_id="dba", status="pending", expires_at=self.now + timedelta(hours=1)),
                ApprovalRequest(plan_id=hidden_plan.id, requester_id="operator", approver_id="dba", status="pending", expires_at=self.now + timedelta(hours=1)),
            ])
            db.commit()
            result = operations_dashboard.build_operations_dashboard(
                db,
                allowed_instance_ids={visible.id},
                username="dba",
                now=self.now,
            )

        self.assertEqual(result["instance_count"], 1)
        self.assertEqual(result["alert_count"], 1)
        self.assertEqual(result["critical_count"], 0)
        self.assertEqual([item["instance_name"] for item in result["risk_ranking"]], ["visible"])
        approvals = [item for item in result["work_queue"] if item["category"] == "approval"]
        self.assertEqual([item["entity_id"] for item in approvals], [1])
        self.assertTrue(result["scope"]["restricted"])

    def test_dashboard_api_preserves_legacy_counts_and_exposes_new_sections(self):
        app = FastAPI()
        app.include_router(system.router, prefix="/api/v1")

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        response = TestClient(app).get("/api/v1/system/dashboard-stats")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("instance_count", payload)
        self.assertIn("kpis", payload)
        self.assertIn("slo_cards", payload)
        self.assertIn("work_queue", payload)
        self.assertIn("risk_ranking", payload)


if __name__ == "__main__":
    unittest.main()
