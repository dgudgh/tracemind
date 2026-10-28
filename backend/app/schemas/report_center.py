from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReportSnapshotCreate(BaseModel):
    title: str = Field("统一运维报告快照", min_length=1, max_length=500)
    report_type: str = Field("all", max_length=50)
    instance_ids: list[int] = Field(default_factory=list)
    watermark: str | None = Field(None, max_length=255)
    retention_days: int = Field(365, ge=1, le=36500)
    limit: int = Field(500, ge=1, le=1000)


class ReportArchiveRequest(BaseModel):
    archive_uri: str | None = Field(None, max_length=2000)
    reason: str | None = Field(None, max_length=1000)


class ReportLegalHoldRequest(BaseModel):
    enabled: bool
    reason: str | None = Field(None, max_length=1000)


class ReportAttachmentCreate(BaseModel):
    attachment_key: str | None = Field(None, max_length=160)
    file_name: str = Field(..., min_length=1, max_length=500)
    media_type: str | None = Field(None, max_length=160)
    storage_uri: str | None = Field(None, max_length=4000)
    content_sha256: str | None = Field(None, pattern="^[0-9a-fA-F]{64}$")
    size_bytes: int | None = Field(None, ge=0)
    classification: str = Field("internal", pattern="^(public|internal|confidential|restricted)$")
    extra_json: dict[str, Any] = Field(default_factory=dict)


class ReportScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    report_type: str = Field("all", max_length=50)
    output_formats: list[str] = Field(default_factory=lambda: ["markdown", "html", "json"])
    filter_json: dict[str, Any] = Field(default_factory=dict)
    schedule_type: str = Field("daily", max_length=30)
    interval_minutes: int | None = Field(None, ge=1, le=525600)
    cron_expression: str | None = Field(None, max_length=120)
    run_time: str = Field("08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    day_of_month: int = Field(1, ge=1, le=31)
    timezone: str = Field("Asia/Shanghai", max_length=100)
    misfire_policy: str = Field("fire_once", max_length=30)
    misfire_grace_seconds: int = Field(3600, ge=0, le=604800)
    max_catchup_runs: int = Field(1, ge=1, le=10)
    enabled: int = Field(1, ge=0, le=1)
    watermark: str | None = Field(None, max_length=255)
    retention_days: int = Field(365, ge=1, le=36500)
    tenant_id: int | None = None
    organization_id: int | None = None


class ReportScheduleUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    report_type: str | None = Field(None, max_length=50)
    output_formats: list[str] | None = None
    filter_json: dict[str, Any] | None = None
    schedule_type: str | None = Field(None, max_length=30)
    interval_minutes: int | None = Field(None, ge=1, le=525600)
    cron_expression: str | None = Field(None, max_length=120)
    run_time: str | None = Field(None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[int] | None = None
    day_of_month: int | None = Field(None, ge=1, le=31)
    timezone: str | None = Field(None, max_length=100)
    misfire_policy: str | None = Field(None, max_length=30)
    misfire_grace_seconds: int | None = Field(None, ge=0, le=604800)
    max_catchup_runs: int | None = Field(None, ge=1, le=10)
    enabled: int | None = Field(None, ge=0, le=1)
    watermark: str | None = Field(None, max_length=255)
    retention_days: int | None = Field(None, ge=1, le=36500)
    tenant_id: int | None = None
    organization_id: int | None = None


class ReportSubscriptionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    schedule_id: int | None = None
    report_types: list[str] = Field(default_factory=list)
    channels: list[int | str] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    filter_json: dict[str, Any] = Field(default_factory=dict)
    enabled: int = Field(1, ge=0, le=1)
    tenant_id: int | None = None
    organization_id: int | None = None


class ReportSubscriptionUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    schedule_id: int | None = None
    report_types: list[str] | None = None
    channels: list[int | str] | None = None
    recipients: list[str] | None = None
    filter_json: dict[str, Any] | None = None
    enabled: int | None = Field(None, ge=0, le=1)
    tenant_id: int | None = None
    organization_id: int | None = None
