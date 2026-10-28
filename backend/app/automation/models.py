from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AutomationPlan(Base):
    __tablename__ = "automation_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True, index=True)
    plan_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="change", index=True)
    source_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_host: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    host_whitelist: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    executor_type: Mapped[str] = mapped_column(String(30), nullable=False, default="ssh")
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", index=True)
    maintenance_window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    maintenance_window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    expected_target_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    validation_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApprovalRequest(Base):
    __tablename__ = "automation_approval_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    requester_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    approver_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approval_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApprovalDecisionRecord(Base):
    __tablename__ = "automation_approval_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ExecutionRun(Base):
    __tablename__ = "automation_execution_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True, index=True)
    request_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    executor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    executor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AutomationTargetLock(Base):
    __tablename__ = "automation_target_lock"

    target_host: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class ExecutionOutput(Base):
    __tablename__ = "automation_execution_output"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RollbackPlan(Base):
    __tablename__ = "automation_rollback_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prepared_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AutomationAudit(Base):
    __tablename__ = "automation_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status_before: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status_after: Mapped[str | None] = mapped_column(String(30), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
