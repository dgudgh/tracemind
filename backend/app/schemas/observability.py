from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class LogQueryRequest(BaseModel):
    source_id: int
    start_time: datetime
    end_time: datetime
    instance_id: int | None = None
    application: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=200)
    severity: str | None = Field(default=None, max_length=30)
    trace_id: str | None = Field(default=None, max_length=256)
    keyword: str | None = Field(default=None, max_length=500)
    query: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=2000, ge=1, le=2000)


class FaultTimelineRequest(BaseModel):
    instance_id: int | None = None
    start_time: datetime
    end_time: datetime
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)


class GrafanaExportRequest(BaseModel):
    template_key: str = Field(max_length=80)
    datasource_uid: str = Field(max_length=200)
    refresh: str = Field(default="30s", max_length=10)


class TraceQueryRequest(BaseModel):
    source_id: int
    trace_id: str = Field(min_length=1, max_length=256)


class ApmEvidenceRequest(BaseModel):
    source_id: int
    instance_id: int
    start_time: datetime
    end_time: datetime
    trace_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("trace_ids")
    @classmethod
    def normalize_trace_ids(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
        if not normalized:
            raise ValueError("at least one trace id is required")
        if any(len(item) > 256 for item in normalized):
            raise ValueError("trace id must not exceed 256 characters")
        return normalized
