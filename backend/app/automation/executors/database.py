"""Bound configuration rollback only; this is not a general SQL executor."""

import re
import time

from app.automation.models import ApprovalRequest, AutomationPlan, ExecutionRun, RollbackPlan
from app.automation.policy import bounded_redacted_output, validate_plan_integrity
from app.core.database_options import redact_database_error
from app.models.config_governance import DBConfigDrift, DBConfigGovernanceAudit, DBConfigRemediation
from app.models.db_instance import DBInstance


class DatabaseRollbackExecutor:
    def __init__(self, db):
        self.db = db
        self.outcomes = {}

    @staticmethod
    def validate_replay(request, plan, run):
        if run.request_id != request.id or run.plan_id != plan.id:
            raise ValueError("idempotency key belongs to a different approval target")

    def _source(self, original, rollback):
        from app.services import config_governance as governance

        remediation = self.db.query(DBConfigRemediation).filter_by(automation_plan_id=original.id).one_or_none()
        if remediation is None or original.executor_type != "database" or original.plan_kind != "configuration_change":
            raise ValueError("database executor requires a bound configuration remediation")
        instance = self.db.get(DBInstance, remediation.instance_id)
        if instance is None:
            raise ValueError("bound database instance no longer exists")
        binding = governance.validate_config_binding(self.db, remediation, instance)
        submitted = self.db.query(DBConfigGovernanceAudit).filter_by(
            entity_type="remediation", entity_id=remediation.id, action="submitted",
        ).one_or_none()
        if submitted is None or submitted.after_json != {
            "automation_plan_id": original.id,
            "approval_request_id": remediation.approval_request_id,
            "rollback_plan_id": rollback.id,
        }:
            raise ValueError("rollback is not the submitted configuration rollback")
        validate_plan_integrity(original)
        identity = f"{instance.db_type}:{instance.host}:{instance.port}"
        if (original.target_host != instance.host or original.expected_target_identity != identity
                or original.script_sha256 != binding["script_sha256"]
                or original.validation_sha256 != binding["validation_sha256"]
                or rollback.plan_id != original.id
                or rollback.script_sha256 != binding["rollback_sha256"]
                or governance.sha256_text(rollback.script) != binding["rollback_sha256"]):
            raise ValueError("approved configuration SQL or target changed")
        operations = binding["operations"]
        if instance.db_type != "mysql":
            raise ValueError("governed rollback currently supports MySQL only")
        if not operations or len(operations) > 64:
            raise ValueError("governed rollback requires 1..64 parameter operations")
        for item in operations:
            if (item.get("category") != "parameter" or not item.get("executable")
                    or item.get("restart_required") or item.get("actual_value") is None
                    or item.get("value_type") not in {"integer", "number", "boolean"}
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", item.get("parameter_name", ""))):
                raise ValueError("rollback supports only generated numeric/boolean parameters without restart")
            for key in ("actual_value", "desired_value"):
                value = governance._typed(item[key], item["value_type"])
                if item["value_type"] == "number" and not value.is_finite():
                    raise ValueError("rollback parameter values must be finite")
        names = [item["parameter_name"].lower() for item in operations]
        if len(names) != len(set(names)):
            raise ValueError("duplicate rollback parameters")
        generated = [governance._parameter_statement(
            "mysql", item["parameter_name"], item["actual_value"], item["value_type"], False,
        ) for item in operations]
        statements = [line for line in rollback.script.splitlines() if line and not line.startswith("--")]
        if statements != generated:
            raise ValueError("custom rollback SQL is not executable by configuration governance")
        return remediation, instance, operations

    def prepare_fields(self, original, rollback):
        self._source(original, rollback)
        # Verification is inseparable from execute(), not an arbitrary validation script.
        return {}

    def _bound(self, plan):
        if plan.executor_type != "database" or plan.plan_kind != "rollback" or not plan.source_plan_id:
            raise ValueError("database executor accepts governed rollback plans only")
        original = self.db.get(AutomationPlan, plan.source_plan_id)
        rollback = self.db.query(RollbackPlan).filter_by(prepared_plan_id=plan.id).one_or_none()
        if original is None or rollback is None:
            raise ValueError("rollback is not bound to its prepared configuration plan")
        remediation, instance, operations = self._source(original, rollback)
        validate_plan_integrity(plan)
        if (plan.script != rollback.script or plan.script_sha256 != rollback.script_sha256
                or plan.target_host != original.target_host
                or plan.expected_target_identity != original.expected_target_identity
                or plan.risk_level != "high" or plan.max_retries != 0
                or plan.validation_script or plan.validation_sha256
                or not 10 <= plan.timeout_seconds <= 300):
            raise ValueError("governed rollback SQL, target or execution policy changed")
        request = self.db.query(ApprovalRequest).filter_by(plan_id=plan.id, status="executing").one_or_none()
        if request is None or request.required_approvals < 2 or request.approval_count < 2:
            raise ValueError("governed rollback requires independent dual approval")
        forward = self.db.query(ExecutionRun).filter_by(
            plan_id=original.id, request_id=remediation.approval_request_id, executor_type="database",
        ).first()
        if forward is None or forward.status not in {"success", "succeeded", "failed"}:
            raise ValueError("original configuration execution has not finished")
        if remediation.status not in {
            "pending_verification", "pending_restart", "verified", "failed", "verification_failed", "drift_remaining",
        }:
            raise ValueError("configuration remediation is not eligible for rollback")
        other = self.db.query(ExecutionRun).join(AutomationPlan, ExecutionRun.plan_id == AutomationPlan.id).filter(
            AutomationPlan.source_plan_id == original.id,
            AutomationPlan.plan_kind == "rollback", ExecutionRun.plan_id != plan.id,
        ).first()
        if other is not None:
            raise ValueError("configuration rollback already attempted; re-evaluate before another change")
        return remediation, instance, operations, rollback

    def probe_identity(self, plan):
        _, instance, _, _ = self._bound(plan)
        return f"{instance.db_type}:{instance.host}:{instance.port}"

    def execute(self, plan, cancel_check=None):
        from app.services import config_governance as governance

        remediation, instance, operations, rollback = self._bound(plan)
        deadline = time.monotonic() + plan.timeout_seconds
        outcome = {"remediation": remediation, "rollback": rollback, "verified": False, "observed": {}}
        self.outcomes[plan.id] = outcome
        messages = []

        def check(connection=None):
            if cancel_check and cancel_check():
                raise InterruptedError("configuration rollback cancelled; inspect partial changes")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("configuration rollback timed out; inspect partial changes")
            if connection is not None:
                # PyMySQL applies these socket bounds for each command read/write.
                if not hasattr(connection, "_read_timeout") or not hasattr(connection, "_write_timeout"):
                    raise ValueError("database driver does not support bounded rollback IO")
                connection._read_timeout = remaining
                connection._write_timeout = remaining

        def read_values(cursor, connection, persisted=False):
            check(connection)
            names = ", ".join(governance._sql_string(item["parameter_name"]) for item in operations)
            sql = ("SELECT VARIABLE_NAME, VARIABLE_VALUE AS value FROM performance_schema.persisted_variables "
                   f"WHERE VARIABLE_NAME IN ({names})") if persisted else governance._validation_script(
                       "mysql", [item["parameter_name"] for item in operations],
                   )
            cursor.execute(sql)
            rows = governance._rows_as_dicts(cursor)
            check(connection)
            return {str(row["variable_name"]).lower(): row.get("value") for row in rows}

        def matches(values, item, key):
            actual = values.get(item["parameter_name"].lower())
            return actual is not None and governance.compare_value(
                actual, item[key], "eq", item["value_type"],
            )

        try:
            check()
            with governance.mysql_connection(instance) as connection:
                check(connection)
                cursor = connection.cursor()
                try:
                    current = read_values(cursor, connection)
                    persisted = read_values(cursor, connection, persisted=True)
                    # SET PERSIST is nontransactional. Check the entire set before the first write.
                    for item in operations:
                        if not matches(current, item, "desired_value") or not matches(persisted, item, "desired_value"):
                            raise ValueError(f"stale live configuration for {item['parameter_name']}; no rollback writes issued")
                    for item in operations:
                        check(connection)
                        cursor.execute(governance._parameter_statement(
                            "mysql", item["parameter_name"], item["actual_value"], item["value_type"], False,
                        ))
                        messages.append(f"submitted rollback parameter {item['parameter_name']}")
                        check(connection)
                    current = read_values(cursor, connection)
                    persisted = read_values(cursor, connection, persisted=True)
                    outcome["observed"] = {"runtime": current, "persisted": persisted}
                    for item in operations:
                        if not matches(current, item, "actual_value") or not matches(persisted, item, "actual_value"):
                            raise ValueError(f"rollback verification failed for {item['parameter_name']}")
                    outcome["verified"] = True
                finally:
                    cursor.close()
            check()
            return {"exit_code": 0, "stdout": "\n".join(messages + ["runtime and persisted rollback values verified"]), "stderr": ""}
        except Exception as exc:
            outcome["verified"] = False
            return {
                "exit_code": 1, "stdout": "\n".join(messages),
                "stderr": bounded_redacted_output(redact_database_error(exc)),
                "timed_out": isinstance(exc, TimeoutError), "cancelled": isinstance(exc, InterruptedError),
            }

    def finalize(self, plan, run):
        from app.services import config_governance as governance

        outcome = self.outcomes.pop(plan.id, None)
        if outcome is None:
            run.validation_status = "not_run"
            return
        verified = outcome["verified"] and run.status == "succeeded"
        run.validation_status = "passed" if verified else "failed"
        run.validation_message = "runtime and persisted rollback values verified" if verified else (
            run.error_message or "rollback not verified; inspect partial nontransactional changes"
        )
        remediation, rollback = outcome["remediation"], outcome["rollback"]
        remediation.status = "rolled_back" if verified else "rollback_failed"
        remediation.apply_message = run.validation_message
        rollback.status = "verified" if verified else run.status
        if verified:
            remediation.verified_by = run.executor_id
            remediation.verified_at = governance.utcnow()
            for drift in self.db.query(DBConfigDrift).filter(
                DBConfigDrift.id.in_(remediation.drift_ids_json),
                DBConfigDrift.instance_id == remediation.instance_id,
                DBConfigDrift.baseline_id == remediation.baseline_id,
            ).all():
                drift.actual_value = str(outcome["observed"]["runtime"][drift.parameter_name.lower()])
                drift.status = "open"
                drift.verified_at = None
        governance.audit(
            self.db, "remediation", remediation.id, "rollback_verified" if verified else "rollback_failed",
            run.executor_id, after={"run_id": run.id, "rollback_plan_id": rollback.id,
                                    "status": remediation.status, "observed": outcome["observed"]},
            message=run.validation_message,
        )
