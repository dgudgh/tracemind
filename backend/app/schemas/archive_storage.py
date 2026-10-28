from pydantic import BaseModel, Field


class ArchiveStorageConfigCreate(BaseModel):
    config_name: str = Field(..., min_length=1, max_length=160)
    storage_type: str = Field(..., pattern="^(s3|minio|oss|cos|obs|nas)$")
    endpoint_url: str | None = Field(None, max_length=500)
    bucket_name: str | None = Field(None, max_length=200)
    root_path: str | None = Field(None, max_length=1000)
    access_key: str | None = None
    secret_key: str | None = None
    enabled: int = Field(1, ge=0, le=1)


class ArchiveStorageConfigUpdate(BaseModel):
    config_name: str | None = Field(None, min_length=1, max_length=160)
    endpoint_url: str | None = Field(None, max_length=500)
    bucket_name: str | None = Field(None, max_length=200)
    root_path: str | None = Field(None, max_length=1000)
    access_key: str | None = None
    secret_key: str | None = None
    enabled: int | None = Field(None, ge=0, le=1)


class ArchiveUploadPlanRequest(BaseModel):
    storage_id: int


class ArchiveArtifactRegister(BaseModel):
    storage_id: int
    source_path: str = Field(..., min_length=1, max_length=1000)
    object_key: str = Field(..., min_length=1, max_length=1000)
    archive_job_id: int | None = None


class ArchiveLifecycleDeleteRequest(BaseModel):
    reason: str | None = Field(None, max_length=1000)
    requester: str | None = Field(None, max_length=100)


class ArchiveRestoreRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=128)
