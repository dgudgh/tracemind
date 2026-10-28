from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class BackupPlatformConfig(Base):
    __tablename__ = "backup_platform_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    mapping_profile: Mapped[str] = mapped_column(String(40), default="generic")
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    endpoint_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    username: Mapped[str | None] = mapped_column(String(160), nullable=True)
    secret_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    status: Mapped[str] = mapped_column(String(30), default="configured", index=True)
    last_success_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BackupExternalJob(Base):
    __tablename__ = "backup_external_job"
    __table_args__ = (UniqueConstraint("config_id", "external_job_id", name="uq_backup_external_job"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_job_id: Mapped[str] = mapped_column(String(200), nullable=False)
    job_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="unknown", index=True)
    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class BackupExternalRun(Base):
    __tablename__ = "backup_external_run"
    __table_args__ = (UniqueConstraint("config_id", "external_run_id", name="uq_backup_external_run"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_job_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    external_run_id: Mapped[str] = mapped_column(String(200), nullable=False)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    capacity_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RestoreDrill(Base):
    __tablename__ = "backup_restore_drill"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
