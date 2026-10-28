import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.automation import service
from app.automation.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    AutomationAudit,
    AutomationPlan,
    AutomationTargetLock,
    ExecutionOutput,
    ExecutionRun,
    RollbackPlan,
)


class FakeExecutor:
    executor_type = "ssh"

    def __init__(self):
        self.calls = 0

    def execute(self, plan):
        self.calls += 1
        return {
            "exit_code": 0,
            "stdout": "password=super-secret token: api-token secret='super secret-value' Authorization: Bearer bearer-token",
            "stderr": "api_key=another-secret",
        }

    def probe_identity(self, plan):
        return "mes-db-01"


class AutomationApprovalTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (
            AutomationPlan,
            ApprovalRequest,
            ApprovalDecisionRecord,
            ExecutionRun,
            AutomationTargetLock,
            ExecutionOutput,
            RollbackPlan,
            AutomationAudit,
        ):
            model.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.executor = FakeExecutor()
        self.automation = service.AutomationService(
            self.db,
            executors={"ssh": self.executor},
            execution_enabled=True,
            allowed_hosts={"mes-db-01"},
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def create_plan(self, **overrides):
        script = overrides.pop("script", "echo healthy")
        payload = {
            "name": "Check MES database",
            "script": script,
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "target_host": "mes-db-01",
            "host_whitelist": ["mes-db-01"],
            "executor_type": "ssh",
        }
        payload.update(overrides)
        return self.automation.create_plan(**payload)

    def submit_and_approve(self, requester_id="requester", approver_id="approver"):
        plan = self.create_plan()
        request = self.automation.submit_plan(plan.id, requester_id)
        return plan, self.automation.approve(request.id, approver_id)

    def test_draft_plan_cannot_execute(self):
        plan = self.create_plan()

        with self.assertRaisesRegex(ValueError, "approved"):
            self.automation.execute(plan.id, "executor")

        self.assertEqual(plan.status, "draft")
        self.assertEqual(self.executor.calls, 0)

    def test_requester_cannot_approve_own_plan(self):
        plan = self.create_plan()
        request = self.automation.submit_plan(plan.id, "requester")

        with self.assertRaisesRegex(ValueError, "self-approve"):
            self.automation.approve(request.id, "requester")

        self.assertEqual(request.status, "pending")
        self.assertEqual(plan.status, "pending_approval")

    def test_rejected_or_expired_request_cannot_execute(self):
        plan = self.create_plan()
        request = self.automation.submit_plan(plan.id, "requester")
        self.automation.reject(request.id, "approver", "unsafe window")

        with self.assertRaisesRegex(ValueError, "rejected"):
            self.automation.execute(request.id, "executor")

        expired_plan = self.create_plan(name="Expired plan")
        expired_request = self.automation.submit_plan(
            expired_plan.id,
            "requester",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            self.automation.approve(expired_request.id, "approver")
        with self.assertRaisesRegex(ValueError, "expired"):
            self.automation.execute(expired_request.id, "executor")

        self.assertEqual(expired_request.status, "expired")
        self.assertEqual(expired_plan.status, "expired")
        self.assertEqual(self.executor.calls, 0)

    def test_approved_request_that_expires_cannot_execute(self):
        plan, request = self.submit_and_approve()
        request.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "expired"):
            self.automation.execute(request.id, "executor")

        self.assertEqual(request.status, "expired")
        self.assertEqual(plan.status, "expired")
        self.assertEqual(self.executor.calls, 0)

    def test_execution_rejects_hosts_outside_whitelist_and_script_hash_mismatch(self):
        host_plan, host_request = self.submit_and_approve()
        host_plan.target_host = "unlisted-host"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "whitelist"):
            self.automation.execute(host_request.id, "executor")

        hash_plan, hash_request = self.submit_and_approve()
        hash_plan.script = "echo altered"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "hash"):
            self.automation.execute(hash_request.id, "executor")

        self.assertEqual(self.executor.calls, 0)

    def test_approved_plan_executes_once_and_masks_stored_output(self):
        plan, request = self.submit_and_approve()

        run = self.automation.execute(request.id, "executor")

        self.assertEqual(run.status, "succeeded")
        self.assertEqual(plan.status, "executed")
        self.assertEqual(request.status, "executed")
        self.assertEqual(self.executor.calls, 1)
        output = self.db.query(ExecutionOutput).filter_by(run_id=run.id).one()
        self.assertNotIn("super-secret", output.stdout)
        self.assertNotIn("api-token", output.stdout)
        self.assertNotIn("secret-value", output.stdout)
        self.assertNotIn("bearer-token", output.stdout)
        self.assertNotIn("another-secret", output.stderr)
        self.assertIn("***", output.stdout)

        repeated = self.automation.execute(request.id, "executor")
        self.assertEqual(repeated.id, run.id)
        self.assertEqual(self.db.query(ExecutionRun).count(), 1)
        self.assertEqual(self.executor.calls, 1)

    def test_execution_is_plan_only_by_default_and_preserves_approved_state(self):
        plan = self.create_plan()
        request = self.automation.submit_plan(plan.id, "requester")
        self.automation.approve(request.id, "approver")
        disabled_service = service.AutomationService(
            self.db,
            executors={"ssh": self.executor},
            execution_enabled=False,
            allowed_hosts={"mes-db-01"},
        )

        executable_plan = disabled_service.execute(request.id, "executor")

        self.assertEqual(executable_plan.status, "plan_only")
        self.assertEqual(executable_plan.target_host, "mes-db-01")
        self.assertEqual(request.status, "approved")
        self.assertEqual(plan.status, "approved")
        self.assertEqual(self.db.query(ExecutionRun).count(), 0)
        self.assertEqual(self.executor.calls, 0)

    def test_high_risk_plan_requires_rollback_window_identity_and_two_approvers(self):
        validation = "echo validation-ok"
        plan = self.create_plan(
            name="High-risk maintenance",
            risk_level="high",
            expected_target_identity="mes-db-01",
            maintenance_window_start=datetime.now(timezone.utc) - timedelta(minutes=5),
            maintenance_window_end=datetime.now(timezone.utc) + timedelta(minutes=30),
            validation_script=validation,
            validation_sha256=hashlib.sha256(validation.encode("utf-8")).hexdigest(),
        )
        rollback_script = "echo rollback"
        self.automation.create_rollback_plan(
            plan.id,
            rollback_script,
            hashlib.sha256(rollback_script.encode("utf-8")).hexdigest(),
            "requester",
        )

        request = self.automation.submit_plan(plan.id, "requester")
        self.assertEqual(request.required_approvals, 2)
        first = self.automation.approve(request.id, "approver-a")
        self.assertEqual(first.status, "pending")
        self.assertEqual(first.approval_count, 1)
        with self.assertRaisesRegex(ValueError, "already approved"):
            self.automation.approve(request.id, "approver-a")
        second = self.automation.approve(request.id, "approver-b")
        self.assertEqual(second.status, "approved")
        self.assertEqual(second.approval_count, 2)

        run = self.automation.execute(request.id, "executor")

        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.target_identity, "mes-db-01")
        self.assertEqual(run.validation_status, "passed")
        self.assertEqual(self.executor.calls, 2)
        output = self.db.query(ExecutionOutput).filter_by(run_id=run.id).one()
        self.assertRegex(output.output_sha256, r"^[a-f0-9]{64}$")
        self.assertEqual(self.db.query(AutomationTargetLock).count(), 0)
        self.assertGreaterEqual(self.db.query(AutomationAudit).count(), 7)

    def test_high_risk_plan_without_compensation_is_not_submitted(self):
        plan = self.create_plan(
            risk_level="high",
            expected_target_identity="mes-db-01",
            maintenance_window_start=datetime.now(timezone.utc) - timedelta(minutes=5),
            maintenance_window_end=datetime.now(timezone.utc) + timedelta(minutes=30),
        )

        with self.assertRaisesRegex(ValueError, "rollback"):
            self.automation.submit_plan(plan.id, "requester")

        self.assertEqual(plan.status, "draft")

    def test_maintenance_window_and_server_whitelist_block_real_execution(self):
        plan = self.create_plan(
            maintenance_window_start=datetime.now(timezone.utc) + timedelta(hours=1),
            maintenance_window_end=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        request = self.automation.submit_plan(plan.id, "requester")
        self.automation.approve(request.id, "approver")

        with self.assertRaisesRegex(ValueError, "has not started"):
            self.automation.execute(request.id, "executor")

        plan.maintenance_window_start = None
        plan.maintenance_window_end = None
        self.db.commit()
        no_whitelist = service.AutomationService(
            self.db,
            executors={"ssh": self.executor},
            execution_enabled=True,
            allowed_hosts=set(),
        )
        with self.assertRaisesRegex(ValueError, "whitelist is not configured"):
            no_whitelist.execute(request.id, "executor")

    def test_approved_request_can_be_cancelled_before_execution(self):
        plan, request = self.submit_and_approve()

        cancelled = self.automation.cancel(request.id, "operator", "maintenance cancelled")

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(plan.status, "cancelled")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.automation.execute(request.id, "executor")


if __name__ == "__main__":
    unittest.main()
