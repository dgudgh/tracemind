from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BackupOperation(Base):
    __tablename__ = "backup_operation"
    __table_args__ = (
        Index("ix_backup_operation_artifact_path", "artifact_path", mysql_length=255),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    drill_policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    engine: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    execution_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="low", index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ready", index=True)

    storage_type: Mapped[str] = mapped_column(String(30), nullable=False, default="local")
    backup_dir: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    table_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rpo_target_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=1440)
    rto_target_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
    actual_rpo_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_rto_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    parameters_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    artifact_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    artifact_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_checksum: Mapped[str | None] = mapped_column(String(160), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_run", index=True)
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BackupOperationAudit(Base):
    __tablename__ = "backup_operation_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
