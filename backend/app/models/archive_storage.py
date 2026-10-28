from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ArchiveStorageConfig(Base):
    __tablename__ = "archive_storage_config"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(160), nullable=False)
    storage_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    endpoint_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bucket_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    root_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    access_key_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_key_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    status: Mapped[str] = mapped_column(String(30), default="configured", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ArchiveArtifact(Base):
    __tablename__ = "archive_artifact"
    __table_args__ = (
        Index("ix_archive_artifact_object_key", "object_key", mysql_length=255),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    storage_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    archive_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="planned", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ArchiveTransfer(Base):
    __tablename__ = "archive_transfer"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    storage_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    archive_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="plan_only", index=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finish_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ArchiveLifecycleRequest(Base):
    __tablename__ = "archive_lifecycle_request"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    approval_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, default="delete")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval", index=True)
    requester: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approver: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
