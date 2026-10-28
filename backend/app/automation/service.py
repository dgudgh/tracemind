import hashlib
import hmac
import inspect
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.automation.executors import AnsibleExecutor, SSHExecutor
from app.automation.executors.database import DatabaseRollbackExecutor
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
from app.automation.policy import (
    AUTOMATION_ALLOWED_HOSTS,
    AUTOMATION_EXECUTION_ENABLED,
    ExecutablePlan,
    bounded_redacted_output,
    is_expired,
    utcnow,
    validate_maintenance_window,
    validate_plan_integrity,
    validate_server_whitelist,
    validate_target_host,
)


class AutomationService:
    def __init__(
        self,
        db: Session,
        executors: dict | None = None,
        execution_enabled: bool | None = None,
        allowed_hosts: set[str] | None = None,
    ):
        self.db = db
        self.executors = executors or {
            "ssh": SSHExecutor(), "ansible": AnsibleExecutor(), "database": DatabaseRollbackExecutor(db),
        }
        self.execution_enabled = AUTOMATION_EXECUTION_ENABLED if execution_enabled is None else execution_enabled
        self.allowed_hosts = set(AUTOMATION_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts)

    def create_plan(self, actor: str | None = None, **payload) -> AutomationPlan:
        values = dict(payload)
        target_host = validate_target_host(values.get("target_host"))
        host_whitelist = {validate_target_host(item) for item in (values.get("host_whitelist") or [])}
        if target_host not in host_whitelist:
            raise ValueError("target host is not in the plan whitelist")
        if self.allowed_hosts and target_host not in self.allowed_hosts:
            raise ValueError("target host is not in the server automation whitelist")
        script = str(values.get("script") or "")
        actual_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_hash, str(values.get("script_sha256") or "").lower()):
            raise ValueError("script hash mismatch")
        validation_script = values.get("validation_script")
        if validation_script:
            validation_hash = hashlib.sha256(validation_script.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(validation_hash, str(values.get("validation_sha256") or "").lower()):
                raise ValueError("validation script hash mismatch")
        elif values.get("validation_sha256"):
            raise ValueError("validation script is required when validation hash is supplied")
        risk_level = str(values.get("risk_level") or "medium").lower()
        if risk_level not in {"low", "medium", "high"}:
            raise ValueError("risk level must be low, medium or high")
        idempotency_key = str(values.get("idempotency_key") or "").strip() or None
        if idempotency_key:
            existing = self.db.query(AutomationPlan).filter(
                AutomationPlan.idempotency_key == idempotency_key
            ).first()
            if existing:
                return existing
        values.update(
            target_host=target_host,
            host_whitelist=sorted(host_whitelist),
            risk_level=risk_level,
            idempotency_key=idempotency_key,
            timeout_seconds=int(values.get("timeout_seconds") or 300),
            max_retries=int(values.get("max_retries") or 0),
            retry_interval_seconds=int(values.get("retry_interval_seconds") or 10),
            created_by=actor or values.get("created_by"),
        )
        if not 10 <= values["timeout_seconds"] <= 86400:
            raise ValueError("timeout seconds must be between 10 and 86400")
        if not 0 <= values["max_retries"] <= 5:
            raise ValueError("max retries must be between 0 and 5")
        if not 1 <= values["retry_interval_seconds"] <= 3600:
            raise ValueError("retry interval seconds must be between 1 and 3600")
        plan = AutomationPlan(**values, status="draft")
        self.db.add(plan)
        self.db.flush()
        self._audit("plan", plan.id, "created", actor, None, "draft", "自动化方案已创建", {
            "target_host": target_host,
            "executor_type": plan.executor_type,
            "risk_level": risk_level,
            "script_sha256": plan.script_sha256,
        })
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def list_plans(self) -> list[AutomationPlan]:
        return self.db.query(AutomationPlan).order_by(AutomationPlan.id.desc()).all()

    def list_requests(self) -> list[ApprovalRequest]:
        return self.db.query(ApprovalRequest).order_by(ApprovalRequest.id.desc()).all()

    def list_runs(self) -> list[ExecutionRun]:
        return self.db.query(ExecutionRun).order_by(ExecutionRun.id.desc()).all()

    def list_rollbacks(self) -> list[RollbackPlan]:
        return self.db.query(RollbackPlan).order_by(RollbackPlan.id.desc()).all()

    def get_run_output(self, run_id: int) -> tuple[ExecutionRun, ExecutionOutput | None]:
        run = self.db.get(ExecutionRun, run_id)
        if not run:
            raise ValueError("execution run not found")
        output = self.db.query(ExecutionOutput).filter(ExecutionOutput.run_id == run.id).first()
        return run, output

    def list_audits(self, limit: int = 200) -> list[AutomationAudit]:
        return self.db.query(AutomationAudit).order_by(AutomationAudit.id.desc()).limit(limit).all()

    def submit_plan(self, plan_id: int, requester_id: str, expires_at=None) -> ApprovalRequest:
        plan = self._plan_or_error(plan_id)
        if plan.status != "draft":
            raise ValueError("only draft plans can be submitted")
        validate_plan_integrity(plan)
        self._validate_high_risk_readiness(plan)
        existing = self.db.query(ApprovalRequest).filter(
            ApprovalRequest.plan_id == plan.id,
            ApprovalRequest.status.in_(["pending", "approved", "executing"]),
        ).first()
        if existing:
            return existing
        request = ApprovalRequest(
            plan_id=plan.id,
            requester_id=requester_id,
            expires_at=expires_at or utcnow() + timedelta(hours=24),
            required_approvals=2 if plan.risk_level == "high" else 1,
            approval_count=0,
            status="pending",
        )
        before = plan.status
        plan.status = "pending_approval"
        self.db.add(request)
        self.db.flush()
        self._audit("approval", request.id, "submitted", requester_id, before, "pending", "方案已提交审批", {
            "plan_id": plan.id,
            "required_approvals": request.required_approvals,
        })
        self.db.commit()
        self.db.refresh(request)
        return request

    def approve(self, request_id: int, approver_id: str, reason: str | None = None) -> ApprovalRequest:
        request, plan = self._request_and_plan(request_id)
        self._expire_if_needed(request, plan)
        if request.status != "pending":
            raise ValueError(f"approval request is {request.status}")
        if request.requester_id == approver_id:
            raise ValueError("requester cannot self-approve")
        duplicate = self.db.query(ApprovalDecisionRecord).filter(
            ApprovalDecisionRecord.request_id == request.id,
            ApprovalDecisionRecord.actor_id == approver_id,
            ApprovalDecisionRecord.decision == "approved",
        ).first()
        if duplicate:
            raise ValueError("approver has already approved this request")
        self.db.add(ApprovalDecisionRecord(
            request_id=request.id,
            actor_id=approver_id,
            decision="approved",
            reason=reason,
        ))
        request.approval_count = int(request.approval_count or 0) + 1
        request.approver_id = approver_id
        if request.approval_count >= request.required_approvals:
            request.status = "approved"
            request.approved_at = utcnow()
            plan.status = "approved"
            action = "approved"
            message = "审批已满足执行门禁"
        else:
            action = "approval_recorded"
            message = f"已完成 {request.approval_count}/{request.required_approvals} 人审批"
        self._audit("approval", request.id, action, approver_id, "pending", request.status, message, {
            "approval_count": request.approval_count,
            "required_approvals": request.required_approvals,
        })
        self.db.commit()
        self.db.refresh(request)
        return request

    def reject(self, request_id: int, approver_id: str, reason: str) -> ApprovalRequest:
        request, plan = self._request_and_plan(request_id)
        self._expire_if_needed(request, plan)
        if request.status != "pending":
            raise ValueError(f"approval request is {request.status}")
        if request.requester_id == approver_id:
            raise ValueError("requester cannot self-reject")
        self.db.add(ApprovalDecisionRecord(
            request_id=request.id,
            actor_id=approver_id,
            decision="rejected",
            reason=reason,
        ))
        request.status = "rejected"
        request.approver_id = approver_id
        request.rejection_reason = reason
        plan.status = "rejected"
        self._audit("approval", request.id, "rejected", approver_id, "pending", "rejected", reason, {
            "plan_id": plan.id,
        })
        self.db.commit()
        self.db.refresh(request)
        return request

    def execute(self, request_id: int, executor_id: str, idempotency_key: str | None = None):
        request = self.db.get(ApprovalRequest, request_id)
        if request is None:
            plan = self.db.get(AutomationPlan, request_id)
            if plan is not None:
                raise ValueError("plan is not approved")
            raise ValueError("approval request not found")
        plan = self._plan_or_error(request.plan_id)
        run_key = str(idempotency_key or f"automation-request:{request.id}").strip()
        existing_run = self.db.query(ExecutionRun).filter(ExecutionRun.idempotency_key == run_key).first()
        if existing_run:
            if plan.executor_type == "database":
                DatabaseRollbackExecutor.validate_replay(request, plan, existing_run)
            return existing_run
        self._expire_if_needed(request, plan)
        if request.status in {"executing", "executed"}:
            raise ValueError("approval request can execute only once")
        if request.status in {"rejected", "expired", "cancelled"}:
            raise ValueError(f"approval request is {request.status}")
        if request.status != "approved" or plan.status != "approved":
            raise ValueError("approval request must be approved before execution")
        validate_plan_integrity(plan)
        preview = ExecutablePlan(
            status="plan_only",
            request_id=request.id,
            plan_id=plan.id,
            target_host=plan.target_host,
            executor_type=plan.executor_type,
            script=plan.script,
        )
        if not self.execution_enabled:
            return preview

        validate_server_whitelist(plan.target_host, self.allowed_hosts)
        validate_maintenance_window(plan)
        self._validate_high_risk_readiness(plan)
        executor = self.executors.get(plan.executor_type)
        if executor is None:
            raise ValueError(f"unsupported executor type: {plan.executor_type}")

        lock = self._acquire_target_lock(plan, executor_id)
        request.status = "executing"
        plan.status = "executing"
        request.executed_at = utcnow()
        run = ExecutionRun(
            idempotency_key=run_key,
            request_id=request.id,
            plan_id=plan.id,
            executor_id=executor_id,
            executor_type=plan.executor_type,
            status="running",
            attempt_count=0,
        )
        self.db.add(run)
        self.db.flush()
        lock.run_id = run.id
        self._audit("run", run.id, "started", executor_id, "approved", "running", "自动化执行已开始", {
            "request_id": request.id,
            "target_host": plan.target_host,
            "idempotency_key": run_key,
        })
        self.db.commit()
        self.db.refresh(run)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            target_identity = self._probe_identity(executor, plan)
            run.target_identity = target_identity or None
            if plan.expected_target_identity and target_identity != plan.expected_target_identity:
                raise ValueError(
                    f"target identity mismatch: expected {plan.expected_target_identity}, got {target_identity or 'empty'}"
                )

            result = None
            for attempt in range(1, int(plan.max_retries or 0) + 2):
                run.attempt_count = attempt
                self.db.commit()
                result = self._execute_executor(executor, plan, run)
                stdout_parts.append(f"[attempt {attempt}]\n{result.get('stdout', '')}")
                stderr_parts.append(f"[attempt {attempt}]\n{result.get('stderr', '')}")
                if result.get("cancelled"):
                    run.status = "cancelled"
                    run.error_message = "execution cancelled by kill switch"
                    break
                if int(result.get("exit_code", 1)) == 0 and not result.get("timed_out"):
                    run.status = "succeeded"
                    break
                run.status = "failed"
                run.error_message = "execution timed out" if result.get("timed_out") else (
                    result.get("stderr") or f"executor exited with code {result.get('exit_code')}"
                )
                if attempt <= int(plan.max_retries or 0):
                    time.sleep(int(plan.retry_interval_seconds or 1))

            run.exit_code = int(result.get("exit_code", 1)) if result and result.get("exit_code") is not None else None
            if run.status == "succeeded" and plan.validation_script:
                validation_plan = self._validation_plan(plan)
                validation_result = self._execute_executor(executor, validation_plan, run)
                stdout_parts.append(f"[validation]\n{validation_result.get('stdout', '')}")
                stderr_parts.append(f"[validation]\n{validation_result.get('stderr', '')}")
                if validation_result.get("cancelled"):
                    run.status = "cancelled"
                    run.validation_status = "cancelled"
                    run.validation_message = "validation cancelled by kill switch"
                elif int(validation_result.get("exit_code", 1)) == 0 and not validation_result.get("timed_out"):
                    run.validation_status = "passed"
                    run.validation_message = "post-execution validation passed"
                else:
                    run.status = "failed"
                    run.validation_status = "failed"
                    run.validation_message = validation_result.get("stderr") or "post-execution validation failed"
                    run.error_message = run.validation_message
            elif run.status == "succeeded":
                run.validation_status = "not_configured"
                run.validation_message = "no post-execution validation script configured"
        except Exception as exc:
            run.status = "failed"
            run.exit_code = None
            run.error_message = str(exc)
            stderr_parts.append(str(exc))

        if plan.executor_type == "database" and isinstance(executor, DatabaseRollbackExecutor):
            executor.finalize(plan, run)
        stdout = bounded_redacted_output("\n".join(stdout_parts))
        stderr = bounded_redacted_output("\n".join(stderr_parts))
        output_digest = hashlib.sha256(f"{stdout}\n{stderr}".encode("utf-8")).hexdigest()
        self.db.add(ExecutionOutput(
            run_id=run.id,
            stdout=stdout,
            stderr=stderr,
            output_sha256=output_digest,
        ))
        run.finished_at = utcnow()
        request.status = "cancelled" if run.status == "cancelled" else "executed"
        plan.status = "executed" if run.status == "succeeded" else run.status
        self._release_target_lock(plan.target_host, run.id)
        self._audit("run", run.id, "finished", executor_id, "running", run.status, run.error_message or "自动化执行完成", {
            "attempt_count": run.attempt_count,
            "exit_code": run.exit_code,
            "target_identity": run.target_identity,
            "validation_status": run.validation_status,
            "output_sha256": output_digest,
        })
        self.db.commit()
        self.db.refresh(run)
        return run

    def cancel(self, request_id: int, actor: str, reason: str) -> ApprovalRequest:
        request, plan = self._request_and_plan(request_id)
        if request.status in {"pending", "approved"}:
            before = request.status
            request.status = "cancelled"
            request.cancelled_at = utcnow()
            request.cancelled_by = actor
            request.cancellation_reason = reason
            plan.status = "cancelled"
            self._audit("approval", request.id, "cancelled", actor, before, "cancelled", reason, None)
            self.db.commit()
            self.db.refresh(request)
            return request
        if request.status == "executing":
            run = self.db.query(ExecutionRun).filter(
                ExecutionRun.request_id == request.id,
                ExecutionRun.status == "running",
            ).order_by(ExecutionRun.id.desc()).first()
            if not run:
                raise ValueError("running execution record not found")
            run.cancel_requested = 1
            request.cancelled_by = actor
            request.cancellation_reason = reason
            self._audit("run", run.id, "cancel_requested", actor, "running", "running", reason, None)
            self.db.commit()
            return request
        raise ValueError(f"approval request is {request.status}")

    def create_rollback_plan(
        self,
        plan_id: int,
        script: str,
        script_sha256: str,
        actor: str | None = None,
    ) -> RollbackPlan:
        self._plan_or_error(plan_id)
        actual_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_hash, script_sha256.lower()):
            raise ValueError("rollback script hash mismatch")
        rollback = RollbackPlan(
            plan_id=plan_id,
            script=script,
            script_sha256=script_sha256.lower(),
            created_by=actor,
        )
        self.db.add(rollback)
        self.db.flush()
        self._audit("rollback", rollback.id, "created", actor, None, "draft", "回滚方案已创建", {
            "plan_id": plan_id,
            "script_sha256": rollback.script_sha256,
        })
        self.db.commit()
        self.db.refresh(rollback)
        return rollback

    def prepare_rollback(self, rollback_id: int, actor: str) -> AutomationPlan:
        rollback = self.db.get(RollbackPlan, rollback_id)
        if not rollback:
            raise ValueError("rollback plan not found")
        if rollback.prepared_plan_id:
            return self._plan_or_error(rollback.prepared_plan_id)
        original = self._plan_or_error(rollback.plan_id)
        if original.executor_type == "database":
            DatabaseRollbackExecutor(self.db).prepare_fields(original, rollback)
        plan = self.create_plan(
            actor=actor,
            idempotency_key=f"rollback:{rollback.id}",
            plan_kind="rollback",
            source_plan_id=original.id,
            name=f"回滚：{original.name}",
            script=rollback.script,
            script_sha256=rollback.script_sha256,
            target_host=original.target_host,
            host_whitelist=original.host_whitelist,
            executor_type=original.executor_type,
            risk_level="high",
            maintenance_window_start=original.maintenance_window_start,
            maintenance_window_end=original.maintenance_window_end,
            timeout_seconds=original.timeout_seconds,
            max_retries=0,
            retry_interval_seconds=original.retry_interval_seconds,
            expected_target_identity=original.expected_target_identity,
        )
        rollback = self.db.get(RollbackPlan, rollback.id)
        rollback.prepared_plan_id = plan.id
        rollback.status = "prepared"
        self._audit("rollback", rollback.id, "prepared", actor, "draft", "prepared", "回滚已转为独立高风险审批方案", {
            "prepared_plan_id": plan.id,
        })
        self.db.commit()
        return plan

    def _request_and_plan(self, request_id: int) -> tuple[ApprovalRequest, AutomationPlan]:
        request = self.db.get(ApprovalRequest, request_id)
        if request is None:
            raise ValueError("approval request not found")
        return request, self._plan_or_error(request.plan_id)

    def _plan_or_error(self, plan_id: int) -> AutomationPlan:
        plan = self.db.get(AutomationPlan, plan_id)
        if plan is None:
            raise ValueError("automation plan not found")
        return plan

    def _expire_if_needed(self, request: ApprovalRequest, plan: AutomationPlan) -> None:
        if request.status in {"pending", "approved"} and is_expired(request.expires_at):
            before = request.status
            request.status = "expired"
            plan.status = "expired"
            self._audit("approval", request.id, "expired", "system", before, "expired", "审批已过期", None)
            self.db.commit()

    def _validate_high_risk_readiness(self, plan: AutomationPlan) -> None:
        if plan.risk_level != "high":
            return
        if not plan.expected_target_identity:
            raise ValueError("high-risk plans require expected target identity")
        if not plan.maintenance_window_start or not plan.maintenance_window_end:
            raise ValueError("high-risk plans require a maintenance window")
        if plan.plan_kind != "rollback":
            rollback = self.db.query(RollbackPlan).filter(RollbackPlan.plan_id == plan.id).first()
            if not rollback:
                raise ValueError("high-risk plans require a rollback plan before submission")

    def _acquire_target_lock(self, plan: AutomationPlan, owner: str) -> AutomationTargetLock:
        existing = self.db.get(AutomationTargetLock, plan.target_host)
        if existing and is_expired(existing.expires_at):
            self.db.delete(existing)
            self.db.flush()
            existing = None
        if existing:
            raise ValueError(f"target host already has a running automation: {plan.target_host}")
        duration = int(plan.timeout_seconds or 300) * (int(plan.max_retries or 0) + 1)
        duration += int(plan.retry_interval_seconds or 1) * int(plan.max_retries or 0) + 300
        lock = AutomationTargetLock(
            target_host=plan.target_host,
            owner=owner,
            acquired_at=utcnow(),
            expires_at=utcnow() + timedelta(seconds=duration),
        )
        self.db.add(lock)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ValueError(f"target host already has a running automation: {plan.target_host}") from exc
        return lock

    def _release_target_lock(self, target_host: str, run_id: int) -> None:
        lock = self.db.get(AutomationTargetLock, target_host)
        if lock and (lock.run_id is None or lock.run_id == run_id):
            self.db.delete(lock)

    def _probe_identity(self, executor, plan: AutomationPlan) -> str:
        probe = getattr(executor, "probe_identity", None)
        if not probe:
            if plan.expected_target_identity:
                raise ValueError("executor does not support target identity probing")
            return ""
        return str(probe(plan) or "").strip()

    def _execute_executor(self, executor, plan, run: ExecutionRun) -> dict:
        parameters = inspect.signature(executor.execute).parameters
        if "cancel_check" in parameters:
            return executor.execute(plan, cancel_check=lambda: self._cancel_requested(run.id))
        return executor.execute(plan)

    def _cancel_requested(self, run_id: int) -> bool:
        with self.db.get_bind().connect() as connection:
            value = connection.execute(
                select(ExecutionRun.cancel_requested).where(ExecutionRun.id == run_id)
            ).scalar_one_or_none()
        return bool(value)

    @staticmethod
    def _validation_plan(plan: AutomationPlan):
        return SimpleNamespace(
            target_host=plan.target_host,
            executor_type=plan.executor_type,
            script=plan.validation_script,
            timeout_seconds=plan.timeout_seconds,
        )

    def _audit(
        self,
        entity_type: str,
        entity_id: int,
        action: str,
        actor: str | None,
        status_before: str | None,
        status_after: str | None,
        message: str | None,
        evidence: dict | None,
    ) -> None:
        self.db.add(AutomationAudit(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            status_before=status_before,
            status_after=status_after,
            message=message,
            evidence_json=evidence,
        ))
