from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReportArtifact(Base):
    """Immutable report payload plus lifecycle metadata used by the unified catalog."""

    __tablename__ = "report_artifact"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_report_artifact_source_key"),
        Index("ix_report_artifact_scope_time", "tenant_id", "organization_id", "create_time"),
        Index("ix_report_artifact_instance_time", "instance_id", "create_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    artifact_kind: Mapped[str] = mapped_column(String(30), default="source", nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    db_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="generated", nullable=False, index=True)
    risk_level: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    format_manifest: Mapped[list] = mapped_column(JSON, default=list)
    content_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    source_export_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_manifest: Mapped[list] = mapped_column(JSON, default=list)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_algorithm: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_state: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    archive_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    legal_hold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReportArtifactAttachment(Base):
    __tablename__ = "report_artifact_attachment"
    __table_args__ = (
        UniqueConstraint("artifact_id", "attachment_key", name="uq_report_attachment_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("report_artifact.id", ondelete="CASCADE"), index=True)
    attachment_key: Mapped[str] = mapped_column(String(160), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(30), default="internal", nullable=False)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReportSchedule(Base):
    __tablename__ = "report_schedule"
    __table_args__ = (
        Index("ix_report_schedule_due", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(50), default="all", nullable=False, index=True)
    output_formats: Mapped[list] = mapped_column(JSON, default=lambda: ["markdown", "html", "json"])
    filter_json: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule_type: Mapped[str] = mapped_column(String(30), default="daily", nullable=False)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)
    run_time: Mapped[str] = mapped_column(String(8), default="08:00", nullable=False)
    weekdays: Mapped[list] = mapped_column(JSON, default=lambda: [1, 2, 3, 4, 5])
    day_of_month: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Shanghai", nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(30), default="fire_once", nullable=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    max_catchup_runs: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False, index=True)
    watermark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_planned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReportScheduleRun(Base):
    __tablename__ = "report_schedule_run"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_report_schedule_run_idempotency"),
        Index("ix_report_schedule_run_schedule_time", "schedule_id", "planned_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("report_schedule.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(30), default="scheduled", nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("report_artifact.id", ondelete="SET NULL"), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ReportSubscription(Base):
    __tablename__ = "report_subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("report_schedule.id", ondelete="CASCADE"), nullable=True, index=True)
    report_types: Mapped[list] = mapped_column(JSON, default=list)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    filter_json: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SchedulerLease(Base):
    __tablename__ = "scheduler_lease"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SchedulerDispatchRun(Base):
    __tablename__ = "scheduler_dispatch_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispatch_uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    lease_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False, index=True)
    job_results: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
