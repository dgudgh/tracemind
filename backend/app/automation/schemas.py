from datetime import datetime

from pydantic import BaseModel, Field


class AutomationPlanCreate(BaseModel):
    idempotency_key: str | None = Field(None, min_length=1, max_length=160)
    name: str = Field(..., min_length=1, max_length=200)
    script: str = Field(..., min_length=1)
    script_sha256: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$")
    target_host: str = Field(..., min_length=1, max_length=255)
    host_whitelist: list[str] = Field(..., min_length=1)
    executor_type: str = Field("ssh", pattern=r"^(ssh|ansible)$")
    risk_level: str = Field("medium", pattern=r"^(low|medium|high)$")
    maintenance_window_start: datetime | None = None
    maintenance_window_end: datetime | None = None
    timeout_seconds: int = Field(300, ge=10, le=86400)
    max_retries: int = Field(0, ge=0, le=5)
    retry_interval_seconds: int = Field(10, ge=1, le=3600)
    expected_target_identity: str | None = Field(None, min_length=1, max_length=255)
    validation_script: str | None = Field(None, min_length=1)
    validation_sha256: str | None = Field(None, pattern=r"^[a-fA-F0-9]{64}$")


class ApprovalSubmit(BaseModel):
    requester_id: str | None = Field(None, min_length=1, max_length=100, description="兼容字段，服务端始终使用当前登录用户")
    expires_at: datetime | None = None


class ApprovalDecision(BaseModel):
    approver_id: str | None = Field(None, min_length=1, max_length=100, description="兼容字段，服务端始终使用当前登录用户")
    reason: str | None = Field(None, max_length=2000)


class ExecutionRequest(BaseModel):
    executor_id: str | None = Field(None, min_length=1, max_length=100, description="兼容字段，服务端始终使用当前登录用户")
    idempotency_key: str | None = Field(None, min_length=1, max_length=160)


class RollbackPlanCreate(BaseModel):
    script: str = Field(..., min_length=1)
    script_sha256: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$")
