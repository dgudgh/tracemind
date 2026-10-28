from pydantic import BaseModel, Field


class AIProviderConfigCreate(BaseModel):
    provider_name: str = Field(..., max_length=100)
    provider_type: str = Field(default="openai_compatible", max_length=50)
    base_url: str = Field(..., max_length=500)
    api_key: str | None = None
    model_name: str = Field(..., max_length=100)
    enabled: int = 1
    is_default: int = 0
    timeout_seconds: int = 60
    remark: str | None = None


class AIProviderConfigUpdate(BaseModel):
    provider_name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    enabled: int | None = None
    is_default: int | None = None
    timeout_seconds: int | None = None
    remark: str | None = None


class AIProviderTestRequest(BaseModel):
    provider_type: str = "openai_compatible"
    base_url: str
    api_key: str | None = None
    model_name: str
    timeout_seconds: int = 30
