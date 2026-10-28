"""Durable delivery ledger; plan_service remains the owner of execution evidence."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityBatch(Base):
    __tablename__ = "db_capability_batch"

    batch_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deadline_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_requested_by: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("requested_by", "idempotency_key", name="uq_capability_batch_request"),
        CheckConstraint("deadline_seconds > 0 AND deadline_seconds <= 86400",
                        name="ck_capability_batch_deadline"),
    )


class DBCapabilityExecution(Base):
    __tablename__ = "db_capability_execution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    owner_token: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    reconciliation_checked_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String(96))
    control_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    control_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    desired_state: Mapped[str] = mapped_column(String(8), nullable=False, default="run", server_default="run")
    observed_state: Mapped[str | None] = mapped_column(String(16))
    control_command_id: Mapped[str | None] = mapped_column(String(30))
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    checkpoint_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("batch_id", "seq", name="uq_capability_execution_sequence"),
        CheckConstraint("seq >= 1 AND seq <= 100", name="ck_capability_execution_sequence"),
        CheckConstraint(
            "state IN ('queued','running','archived','reconciliation_required',"
            "'cancelled','expired','blocked','paused','stopped')", name="ck_capability_execution_state",
        ),
        CheckConstraint("control_version >= 0 AND fencing_epoch >= 0 "
                        "AND desired_state IN ('run','pause','stop') "
                        "AND (observed_state IS NULL OR observed_state IN "
                        "('running','paused','stopped','completed','unknown'))",
                        name="ck_capability_execution_control"),
        CheckConstraint("state NOT IN ('paused','stopped') OR (control_enabled "
                        "AND checkpoint_json IS NOT NULL AND checkpoint_hash IS NOT NULL "
                        "AND checkpoint_at IS NOT NULL AND owner_token IS NULL AND lease_expires_at IS NULL)",
                        name="ck_capability_execution_checkpoint"),
        CheckConstraint(
            "state != 'running' OR (owner_token IS NOT NULL AND worker_id IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_capability_execution_owner",
        ),
        Index("ix_capability_execution_claim", "state", "id"),
    )
