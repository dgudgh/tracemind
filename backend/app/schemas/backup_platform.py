from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BackupPlatformConfigCreate(BaseModel):
    config_name: str = Field(..., min_length=1, max_length=160)
    platform: str = Field(..., min_length=1, max_length=40)
    mapping_profile: str = Field("generic", max_length=40)
    instance_id: int | None = None
    endpoint_url: str | None = Field(None, max_length=500)
    auth_type: str | None = Field(None, max_length=30)
    username: str | None = Field(None, max_length=160)
    secret: str | None = None
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = None


class BackupRunSyncRequest(BaseModel):
    since: datetime | None = None
    run_records: list[dict[str, Any]] | None = Field(None, max_length=5000)


class RestoreDrillCreate(BaseModel):
    instance_id: int | None = None
    external_job_id: str | None = Field(None, max_length=200)
    status: str = Field("pending", max_length=30)
    error_message: str | None = None
