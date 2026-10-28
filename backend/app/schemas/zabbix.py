from pydantic import BaseModel, Field


class ZabbixConfigCreate(BaseModel):
    config_name: str = Field(..., min_length=1, max_length=100)
    base_url: str = Field(..., min_length=1, max_length=500)
    username: str | None = Field(None, max_length=100)
    password: str | None = None
    api_token: str | None = None
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)


class ZabbixConfigUpdate(BaseModel):
    config_name: str | None = Field(None, min_length=1, max_length=100)
    base_url: str | None = Field(None, min_length=1, max_length=500)
    username: str | None = Field(None, max_length=100)
    password: str | None = None
    api_token: str | None = None
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)


class BindDbInstanceRequest(BaseModel):
    db_instance_id: int | None = None
    enabled: int = Field(1, ge=0, le=1)


class ZabbixHistoryQuery(BaseModel):
    itemids: list[str]
    history: int = Field(0, ge=0, le=5)
    limit: int = Field(100, ge=1, le=1000)
