from typing import Any

from pydantic import BaseModel, Field


class EcosystemConfigCreate(BaseModel):
    task_code: str = Field(..., min_length=5, max_length=20)
    integration_type: str = Field(..., min_length=1, max_length=50)
    config_name: str = Field(..., min_length=1, max_length=100)
    endpoint_url: str | None = Field(None, max_length=500)
    auth_type: str | None = Field(None, max_length=50)
    username: str | None = Field(None, max_length=100)
    secret: str | None = None
    extra_config: dict[str, Any] | None = None
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)


class EcosystemConfigUpdate(BaseModel):
    task_code: str | None = Field(None, min_length=5, max_length=20)
    integration_type: str | None = Field(None, min_length=1, max_length=50)
    config_name: str | None = Field(None, min_length=1, max_length=100)
    endpoint_url: str | None = Field(None, max_length=500)
    auth_type: str | None = Field(None, max_length=50)
    username: str | None = Field(None, max_length=100)
    secret: str | None = None
    extra_config: dict[str, Any] | None = None
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)


class EcosystemEventCreate(BaseModel):
    task_code: str = Field(..., min_length=5, max_length=20)
    integration_type: str = Field(..., min_length=1, max_length=50)
    event_type: str = Field(..., min_length=1, max_length=50)
    target_name: str | None = Field(None, max_length=200)
    status: str = Field("planned", max_length=30)
    level: str | None = Field(None, max_length=30)
    title: str | None = Field(None, max_length=300)
    content: str | None = None
    payload_json: dict[str, Any] | None = None
    response_text: str | None = None
    error_message: str | None = None


class EcosystemPlanRequest(BaseModel):
    plan_type: str = Field("implementation", max_length=50)
    plan_name: str | None = Field(None, max_length=200)
    payload: dict[str, Any] | None = None


class EcosystemDispatchRequest(BaseModel):
    config_id: int | None = None
    task_code: str = Field(..., min_length=5, max_length=20)
    integration_type: str | None = Field(None, min_length=1, max_length=50)
    title: str = Field(..., min_length=1, max_length=300)
    content: str | None = None
    level: str = Field("info", max_length=30)
    target_name: str | None = Field(None, max_length=200)
    payload: dict[str, Any] | None = None
    execute: bool = False


class GrafanaDashboardRequest(BaseModel):
    dashboard_type: str = Field("overview", max_length=50)
    title: str | None = Field(None, max_length=200)
    datasource: str = Field("Prometheus", max_length=100)
    refresh: str = Field("30s", max_length=30)
