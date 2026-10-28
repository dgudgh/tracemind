from pydantic import BaseModel, Field


class RestoreDrillPolicyCreate(BaseModel):
    policy_name: str = Field(..., min_length=2, max_length=200)
    source_backup_policy_id: int = Field(..., ge=1)
    schedule: str = Field("manual", min_length=1, max_length=100)
    rto_target_minutes: int = Field(240, ge=1, le=43200)
    max_retries: int = Field(1, ge=0, le=5)
    retry_interval_minutes: int = Field(30, ge=1, le=1440)
    enabled: int = Field(0, ge=0, le=1)


class RestoreDrillPolicyUpdate(BaseModel):
    policy_name: str | None = Field(None, min_length=2, max_length=200)
    source_backup_policy_id: int | None = Field(None, ge=1)
    schedule: str | None = Field(None, min_length=1, max_length=100)
    rto_target_minutes: int | None = Field(None, ge=1, le=43200)
    max_retries: int | None = Field(None, ge=0, le=5)
    retry_interval_minutes: int | None = Field(None, ge=1, le=1440)
    enabled: int | None = Field(None, ge=0, le=1)
