"""Additive engine identity and result ledgers; legacy columns stay writable."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBEngineInstanceBinding(Base):
    __tablename__ = "db_engine_instance_binding"
    instance_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engine_id: Mapped[str | None] = mapped_column(String(64))
    source_db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_identity_json: Mapped[dict | None] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DBEnginePlanBinding(Base):
    __tablename__ = "db_engine_plan_binding"
    plan_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    engine_id: Mapped[str | None] = mapped_column(String(64))
    execution_identity_json: Mapped[dict | None] = mapped_column(JSON)
    provider_version: Mapped[str | None] = mapped_column(String(64))
    provider_generation: Mapped[int | None] = mapped_column(Integer)
    operation: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DBEngineResultReceipt(Base):
    __tablename__ = "db_engine_result_receipt"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    step_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    result_schema: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("plan_id", "step_seq", name="uq_engine_result_plan_step"),
        UniqueConstraint("operation_id", "attempt", name="uq_engine_result_operation_attempt"),
    )
