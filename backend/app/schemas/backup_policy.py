from typing import Literal

from pydantic import BaseModel, Field


class BackupPolicyCreate(BaseModel):
    policy_name: str = Field(..., min_length=2, max_length=200)
    instance_id: int = Field(..., ge=1)
    engine: Literal["oracle", "mysql", "postgresql", "sqlserver"]
    operation_type: str = Field(..., min_length=1, max_length=60)
    schedule: str = Field("manual", min_length=1, max_length=100)
    storage_type: str = Field("local", min_length=1, max_length=30)
    backup_dir: str = Field(..., min_length=1, max_length=1000)
    database_name: str | None = Field(None, max_length=200)
    schema_name: str | None = Field(None, max_length=200)
    table_name: str | None = Field(None, max_length=200)
    retention_days: int = Field(30, ge=1, le=3650)
    rpo_target_minutes: int = Field(1440, ge=1, le=525600)
    rto_target_minutes: int = Field(240, ge=1, le=43200)
    validate_after_backup: int = Field(1, ge=0, le=1)
    max_retries: int = Field(2, ge=0, le=10)
    retry_interval_minutes: int = Field(15, ge=1, le=1440)
    enabled: int = Field(0, ge=0, le=1)


class BackupPolicyUpdate(BaseModel):
    policy_name: str | None = Field(None, min_length=2, max_length=200)
    operation_type: str | None = Field(None, min_length=1, max_length=60)
    schedule: str | None = Field(None, min_length=1, max_length=100)
    storage_type: str | None = Field(None, min_length=1, max_length=30)
    backup_dir: str | None = Field(None, min_length=1, max_length=1000)
    database_name: str | None = Field(None, max_length=200)
    schema_name: str | None = Field(None, max_length=200)
    table_name: str | None = Field(None, max_length=200)
    retention_days: int | None = Field(None, ge=1, le=3650)
    rpo_target_minutes: int | None = Field(None, ge=1, le=525600)
    rto_target_minutes: int | None = Field(None, ge=1, le=43200)
    validate_after_backup: int | None = Field(None, ge=0, le=1)
    max_retries: int | None = Field(None, ge=0, le=10)
    retry_interval_minutes: int | None = Field(None, ge=1, le=1440)
    enabled: int | None = Field(None, ge=0, le=1)


class BackupPolicyRunRequest(BaseModel):
    reason: str | None = Field(None, max_length=1000)
