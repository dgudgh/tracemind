import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.automation.models import (
    ApprovalRequest, AutomationAudit, AutomationPlan, AutomationTargetLock, ExecutionOutput, ExecutionRun, RollbackPlan,
)
from app.automation.service import AutomationService
from app.models.config_governance import DBConfigBaselineItem, DBConfigDrift, DBConfigGovernanceAudit
from app.services import config_governance as governance
from backend.tests.test_config_governance import ConfigGovernanceTest


class FakeCursor:
    description = None

    def __init__(self, driver):
        self.driver = driver
        self.rows = []

    def execute(self, sql, args=None):
        self.driver.sql.append(sql)
        if sql.startswith("SET PERSIST"):
            if self.driver.failure:
                raise self.driver.failure
            match = re.fullmatch(r"SET PERSIST (\w+) = (\d+);", sql)
            if not match:
                raise AssertionError(f"unexpected SQL: {sql}")
            name, value = match.groups()
            if not self.driver.ignore_writes:
                self.driver.runtime[name] = value
                if not self.driver.ignore_persisted:
                    self.driver.persisted[name] = value
        elif sql.startswith("SHOW GLOBAL VARIABLES"):
            self.rows = [{"Variable_name": key, "Value": value} for key, value in self.driver.runtime.items()]
        elif "FROM performance_schema.persisted_variables" in sql:
            self.rows = [{"VARIABLE_NAME": key, "value": value} for key, value in self.driver.persisted.items()]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self.rows

    def close(self):
        self.driver.cursor_closes += 1


class FakeConnection:
    _read_timeout = None
    _write_timeout = None

    def __init__(self, driver):
        self.driver = driver

    def cursor(self):
        return FakeCursor(self.driver)

    def commit(self):
        pass

    def close(self):
        self.driver.closes += 1


class FakeDriver:
    def __init__(self):
        self.sql = []
        self.runtime = {"max_connections": "100", "slow_query_log": "1"}
        self.persisted = dict(self.runtime)
        self.failure = None
        self.ignore_writes = False
        self.ignore_persisted = False
        self.closes = 0
        self.cursor_closes = 0
        self.connections = []

    def connect(self, **kwargs):
        connection = FakeConnection(self)
        self.connections.append((kwargs, connection))
        return connection

    @property
    def writes(self):
        return [sql for sql in self.sql if sql.startswith("SET PERSIST")]


class ConfigRollbackTest(unittest.TestCase):
    setUp = ConfigGovernanceTest.setUp
    tearDown = ConfigGovernanceTest.tearDown
    _instance = ConfigGovernanceTest._instance
    _baseline = ConfigGovernanceTest._baseline

    def fixture(self, db, *, apply=True):
        instance = self._instance(db)
        baseline = self._baseline(db, instance)
        governance.evaluate_baseline(db, baseline, instance, actor="collector", observed_parameters={
            "max_connections": "100", "slow_query_log": "ON", "log_retention": {"retention_days": 30},
        })
        drift = db.query(DBConfigDrift).filter_by(parameter_name="max_connections").one()
        remediation = governance.create_remediation(db, baseline, instance, actor="requester", drift_ids=[drift.id])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        governance.submit_remediation(db, remediation, instance, actor="requester",
                                      maintenance_window_start=now - timedelta(minutes=5),
                                      maintenance_window_end=now + timedelta(hours=1))
        service = AutomationService(db, execution_enabled=True, allowed_hosts={instance.host})
        service.approve(remediation.approval_request_id, "approver")
        driver = FakeDriver()
        self.addCleanup(patch.stopall)
        patch("pymysql.connect", side_effect=driver.connect).start()
        patch("app.collectors.mysql.mysql_connection.decrypt_password", return_value="test-only").start()
        if apply:
            governance.apply_remediation(db, remediation.id, instance, actor="operator", confirm=True)
            self.assertEqual(remediation.status, "pending_verification")
        rollback = db.query(RollbackPlan).filter_by(plan_id=remediation.automation_plan_id).one()
        plan = service.prepare_rollback(rollback.id, "rollback-requester")
        request = service.submit_plan(plan.id, "rollback-requester")
        service.approve(request.id, "reviewer-one")
        service.approve(request.id, "reviewer-two")
        driver.sql.clear()
        driver.connections.clear()
        return service, remediation, instance, baseline, rollback, plan, request, driver

    def test_real_service_driver_execution_verifies_runtime_and_persisted_and_reopens_drift(self):
        with self.Session() as db:
            service, remediation, instance, _, rollback, plan, request, driver = self.fixture(db)
            run = service.execute(request.id, "operator")
            self.assertEqual((run.status, run.validation_status), ("succeeded", "passed"))
            self.assertEqual(remediation.status, "rolled_back")
            self.assertEqual(rollback.status, "verified")
            self.assertEqual(driver.writes, ["SET PERSIST max_connections = 100;"])
            self.assertEqual(driver.runtime["max_connections"], "100")
            self.assertEqual(driver.persisted["max_connections"], "100")
            kwargs, connection = driver.connections[0]
            self.assertEqual((kwargs["host"], kwargs["port"], kwargs["user"]),
                             (instance.host, instance.port, instance.username))
            self.assertGreater(connection._read_timeout, 0)
            self.assertLessEqual(connection._read_timeout, plan.timeout_seconds)
            self.assertEqual(db.query(AutomationTargetLock).count(), 0)
            self.assertIn("verified", db.query(ExecutionOutput).filter_by(run_id=run.id).one().stdout)
            evidence = db.query(DBConfigGovernanceAudit).filter_by(action="rollback_verified").one()
            self.assertEqual(evidence.after_json["observed"]["runtime"]["max_connections"], "100")
            self.assertEqual(db.query(DBConfigDrift).filter_by(parameter_name="max_connections").one().status, "open")
            self.assertEqual(service.execute(request.id, "operator").id, run.id)
            with self.assertRaisesRegex(ValueError, "only once"):
                service.execute(request.id, "operator", "different-key")
            self.assertEqual(len(driver.writes), 1)
            with self.assertRaisesRegex(ValueError, "new approved plan"):
                governance.apply_remediation(db, remediation.id, instance, actor="operator", confirm=True)

    def test_plan_only_has_no_connection_or_run(self):
        with self.Session() as db:
            service, _, _, _, _, _, request, driver = self.fixture(db)
            service.execution_enabled = False
            result = service.execute(request.id, "operator")
            self.assertEqual(result.status, "plan_only")
            self.assertEqual(driver.connections, [])
            self.assertEqual(db.query(ExecutionRun).filter_by(request_id=request.id).count(), 0)
            self.assertEqual(request.status, "approved")

    def test_driver_failure_is_redacted_audited_and_not_retried(self):
        with self.Session() as db:
            service, remediation, _, _, rollback, _, request, driver = self.fixture(db)
            driver.failure = RuntimeError("permission denied password=do-not-log")
            run = service.execute(request.id, "operator")
            self.assertEqual((run.status, run.validation_status, run.attempt_count), ("failed", "failed", 1))
            self.assertEqual(remediation.status, "rollback_failed")
            self.assertEqual(rollback.status, "failed")
            self.assertNotIn("do-not-log", run.error_message)
            self.assertEqual(service.execute(request.id, "operator").id, run.id)
            self.assertEqual(len(driver.writes), 1)
            self.assertEqual(db.query(AutomationTargetLock).count(), 0)
            self.assertEqual(db.query(AutomationAudit).filter_by(entity_type="run", entity_id=run.id, action="finished").count(), 1)
            self.assertEqual(db.query(DBConfigGovernanceAudit).filter_by(action="rollback_failed").count(), 1)

    def test_exit_zero_without_actual_or_persisted_change_is_not_success(self):
        for mode in ("ignore_writes", "ignore_persisted"):
            with self.subTest(mode=mode), self.Session() as db:
                service, remediation, _, _, _, _, request, driver = self.fixture(db)
                setattr(driver, mode, True)
                run = service.execute(request.id, "operator")
                self.assertEqual((run.status, run.validation_status), ("failed", "failed"))
                self.assertIn("verification failed", run.error_message)
                self.assertEqual(remediation.status, "rollback_failed")
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_stale_live_configuration_is_rejected_before_any_write(self):
        for source in ("runtime", "persisted"):
            with self.subTest(source=source), self.Session() as db:
                service, _, _, _, _, _, request, driver = self.fixture(db)
                getattr(driver, source)["max_connections"] = "250"
                run = service.execute(request.id, "operator")
                self.assertEqual(run.status, "failed")
                self.assertIn("stale live configuration", run.error_message)
                self.assertEqual(driver.writes, [])
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_stale_baseline_items_or_version_block_before_connect(self):
        for change in ("version", "item"):
            with self.subTest(change=change), self.Session() as db:
                service, _, _, baseline, _, _, request, driver = self.fixture(db)
                if change == "version":
                    baseline.version_no += 1
                else:
                    db.query(DBConfigBaselineItem).filter_by(baseline_id=baseline.id, parameter_name="max_connections").one().desired_value = "500"
                db.commit()
                run = service.execute(request.id, "operator")
                self.assertEqual(run.status, "failed")
                self.assertEqual(driver.connections, [])
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_cross_target_and_rehashed_sql_and_mutated_operations_are_rejected(self):
        for change in ("instance", "plan_target", "sql", "operations"):
            with self.subTest(change=change), self.Session() as db:
                service, remediation, instance, _, _, plan, request, driver = self.fixture(db)
                if change == "instance":
                    instance.port = 3307
                elif change == "plan_target":
                    plan.expected_target_identity = "mysql:10.0.0.10:3307"
                elif change == "sql":
                    plan.script = "DROP DATABASE business;"
                    plan.script_sha256 = governance.sha256_text(plan.script)
                else:
                    remediation.plan_json = [{**remediation.plan_json[0], "actual_value": "123"}]
                db.commit()
                run = service.execute(request.id, "operator")
                self.assertEqual(run.status, "failed")
                self.assertEqual(driver.connections, [])
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_missing_forward_execution_and_arbitrary_database_plan_are_rejected(self):
        with self.Session() as db:
            service, _, instance, _, _, _, request, driver = self.fixture(db, apply=False)
            run = service.execute(request.id, "operator")
            self.assertEqual(run.status, "failed")
            self.assertIn("has not finished", run.error_message)
            plan = service.create_plan(actor="requester", name="arbitrary", script="SELECT 1;",
                                       script_sha256=governance.sha256_text("SELECT 1;"),
                                       executor_type="database", target_host=instance.host, host_whitelist=[instance.host])
            request = service.submit_plan(plan.id, "requester")
            service.approve(request.id, "reviewer")
            run = service.execute(request.id, "operator")
            self.assertEqual(run.status, "failed")
            self.assertIn("governed rollback plans only", run.error_message)
            self.assertEqual(driver.connections, [])

    def test_cancellation_and_timeout_release_locks_without_success(self):
        for mode in ("cancel", "timeout"):
            with self.subTest(mode=mode), self.Session() as db:
                service, remediation, _, _, _, _, request, driver = self.fixture(db)
                if mode == "cancel":
                    context = patch.object(service, "_cancel_requested", return_value=True)
                else:
                    context = patch("app.automation.executors.database.time.monotonic", side_effect=[0, 301])
                with context:
                    run = service.execute(request.id, "operator")
                self.assertEqual(run.status, "cancelled" if mode == "cancel" else "failed")
                self.assertEqual(remediation.status, "rollback_failed")
                self.assertEqual(driver.writes, [])
                self.assertEqual(db.query(AutomationTargetLock).count(), 0)
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_existing_approval_whitelist_window_and_target_lock_gates(self):
        for gate in ("approval", "expiry", "window", "whitelist", "lock"):
            with self.subTest(gate=gate), self.Session() as db:
                service, _, instance, _, _, plan, request, driver = self.fixture(db)
                if gate == "approval":
                    request.status = "pending"
                elif gate == "expiry":
                    request.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
                elif gate == "window":
                    plan.maintenance_window_end = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
                elif gate == "whitelist":
                    service.allowed_hosts = {"another-host"}
                else:
                    db.add(AutomationTargetLock(target_host=instance.host, owner="another-run",
                                                acquired_at=datetime.now(timezone.utc).replace(tzinfo=None),
                                                expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)))
                db.commit()
                with self.assertRaises(ValueError):
                    service.execute(request.id, "operator")
                self.assertEqual(driver.connections, [])
            self.tearDown()
            self.setUp()
            patch.stopall()

    def test_idempotency_key_from_other_approval_cannot_return_cross_target_result(self):
        with self.Session() as db:
            service, remediation, _, _, _, _, request, driver = self.fixture(db)
            with self.assertRaisesRegex(ValueError, "different approval target"):
                service.execute(request.id, "operator", f"config-remediation:{remediation.id}:apply")
            self.assertEqual(driver.connections, [])

    def test_cross_instance_apply_is_rejected_even_with_same_host_and_port(self):
        with self.Session() as db:
            _, remediation, _, _, _, _, _, driver = self.fixture(db, apply=False)
            other = self._instance(db, name="other")
            db.commit()
            with self.assertRaisesRegex(ValueError, "instance mismatch"):
                governance.apply_remediation(db, remediation.id, other, actor="operator", confirm=True)
            self.assertEqual(driver.connections, [])


if __name__ == "__main__":
    unittest.main()
