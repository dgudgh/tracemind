from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBArchivePolicy(Base):
    __tablename__ = "db_archive_policy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    archive_column: Mapped[str] = mapped_column(String(128), nullable=False)
    retention_months: Mapped[int] = mapped_column(Integer, default=6)
    archive_method: Mapped[str] = mapped_column(String(40), default="same_db")
    target_schema: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_table: Mapped[str | None] = mapped_column(String(128), nullable=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=10000)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBArchiveCandidate(Base):
    __tablename__ = "db_archive_candidate"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_column: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oldest_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    newest_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), default="info")
    status: Mapped[str] = mapped_column(String(30), default="candidate", index=True)
    scan_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DBArchiveJob(Base):
    __tablename__ = "db_archive_job"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    job_name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_type: Mapped[str] = mapped_column(String(40), default="archive_plan")
    status: Mapped[str] = mapped_column(String(30), default="plan_only", index=True)
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_schema: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_table: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archive_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DBArchiveExecution(Base):
    __tablename__ = "db_archive_execution"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    storage_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    execution_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="export", index=True)
    driver: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", index=True)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    source_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exported_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    encrypted: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    encryption_algorithm: Mapped[str | None] = mapped_column(String(40), nullable=True)
    encryption_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plaintext_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    recoverability_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    estimated_cost_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actual_cost_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DBArchiveRecoveryTest(Base):
    __tablename__ = "db_archive_recovery_test"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    test_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="sample")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running", index=True)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    restored_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DBArchiveLegalHold(Base):
    __tablename__ = "db_archive_legal_hold"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    hold_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    placed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    released_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    release_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
