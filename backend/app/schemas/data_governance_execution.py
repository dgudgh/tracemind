from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class ArchivePrecheckRequest(BaseModel):
    row_limit: int | None = Field(None, ge=1, le=100_000_000)
    sample_rows: int = Field(20, ge=1, le=1_000)


class ArchiveExecuteRequest(BaseModel):
    precheck_execution_id: int
    storage_id: int | None = None
    row_limit: int | None = Field(None, ge=1, le=100_000_000)
    encrypt: bool = True


class ArchiveRecoveryTestRequest(BaseModel):
    test_mode: str = Field("sample", pattern="^(sample|full)$")


class ArchiveLegalHoldCreate(BaseModel):
    hold_key: str = Field(..., min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    reason: str = Field(..., min_length=2, max_length=2_000)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_expiry(self):
        if self.expires_at is not None:
            now = datetime.now(self.expires_at.tzinfo) if self.expires_at.tzinfo else datetime.now()
            if self.expires_at <= now:
                raise ValueError("法律保留到期时间必须晚于当前时间")
        return self


class ArchiveLegalHoldRelease(BaseModel):
    reason: str = Field(..., min_length=2, max_length=2_000)
