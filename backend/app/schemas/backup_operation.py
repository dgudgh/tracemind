from typing import Literal

from pydantic import BaseModel, Field


class BackupPlanRequest(BaseModel):
    engine: Literal["oracle", "mysql", "postgresql", "sqlserver"] = "oracle"
    instance_id: int | None = Field(None, ge=1)
    plan_type: str | None = Field(None, min_length=1, max_length=60)
    storage_type: str = Field("local", min_length=1, max_length=30)
    backup_dir: str | None = Field(None, max_length=1000)
    database: str | None = Field(None, max_length=200)
    schema_name: str | None = Field(None, alias="schema", max_length=200)
    table: str | None = Field(None, max_length=200)
    target_time: str | None = Field(None, max_length=100)
    execution_scope: Literal["live", "probe"] = "live"
    execute: bool = False
    idempotency_key: str | None = Field(None, min_length=8, max_length=160)
    source_operation_id: int | None = Field(None, ge=1)
    rpo_target_minutes: int = Field(1440, ge=1, le=525600)
    rto_target_minutes: int = Field(240, ge=1, le=43200)

    model_config = {"populate_by_name": True}


class BackupOperationDecision(BaseModel):
    reason: str | None = Field(None, max_length=2000)


class BackupOperationExecute(BaseModel):
    confirmation: str = Field(..., min_length=1, max_length=64)
