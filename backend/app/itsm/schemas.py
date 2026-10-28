from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.core.http_security import require_http_url


class ItsmConfigCreate(BaseModel):
    config_name: str = Field(..., max_length=100)
    provider_type: str = Field(..., max_length=50)
    base_url: str = Field(..., max_length=500)
    auth_type: str = "TOKEN"
    username: Optional[str] = None
    password_cipher: Optional[str] = None
    token_cipher: Optional[str] = None
    extra_config: Optional[str] = None
    enabled: int = 1
    is_default: int = 0
    timeout_seconds: int = 15
    remark: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return require_http_url(value).rstrip("/")


class ItsmConfigUpdate(BaseModel):
    config_name: Optional[str] = Field(None, max_length=100)
    provider_type: Optional[str] = Field(None, max_length=50)
    base_url: Optional[str] = Field(None, max_length=500)
    auth_type: Optional[str] = None
    username: Optional[str] = None
    password_cipher: Optional[str] = None
    token_cipher: Optional[str] = None
    extra_config: Optional[str] = None
    enabled: Optional[int] = None
    is_default: Optional[int] = None
    timeout_seconds: Optional[int] = None
    remark: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return require_http_url(value).rstrip("/") if value else value


class FieldMappingCreate(BaseModel):
    mesguard_field: str = Field(..., max_length=100)
    external_field: str = Field(..., max_length=200)
    default_value: Optional[str] = None
    required: int = 0
    enabled: int = 1


class FieldMappingUpdate(BaseModel):
    mesguard_field: Optional[str] = None
    external_field: Optional[str] = None
    default_value: Optional[str] = None
    required: Optional[int] = None
    enabled: Optional[int] = None


class TicketCreate(BaseModel):
    config_id: int
    idempotency_key: Optional[str] = Field(None, max_length=160)
    source_type: str = Field("MANUAL", max_length=50)
    source_id: Optional[str] = Field(None, max_length=200)
    source_ref_id: Optional[int] = None
    instance_id: Optional[int] = None
    instance_name: Optional[str] = None
    db_type: Optional[str] = None
    title: str = Field(..., max_length=500)
    description: Optional[str] = None
    priority: Optional[str] = Field(None, max_length=20)
    severity: Optional[str] = Field(None, max_length=50)
    category: Optional[str] = None
    sub_category: Optional[str] = None
    assignee: Optional[str] = None
    reporter: Optional[str] = None


class TicketComment(BaseModel):
    comment: str


class TicketClose(BaseModel):
    resolution: Optional[str] = None
    close_source: bool = True


class TicketRuleCreate(BaseModel):
    rule_name: str = Field(..., max_length=100)
    source_type: Optional[str] = None
    alert_type: Optional[str] = None
    severity: Optional[str] = None
    db_type: Optional[str] = None
    env_type: Optional[str] = None
    instance_id: Optional[int] = None
    auto_create: int = 0
    priority: Optional[str] = None
    assignee: Optional[str] = None
    suppress_minutes: int = 60
    enabled: int = 1


class TicketRuleUpdate(BaseModel):
    rule_name: Optional[str] = None
    source_type: Optional[str] = None
    alert_type: Optional[str] = None
    severity: Optional[str] = None
    db_type: Optional[str] = None
    env_type: Optional[str] = None
    instance_id: Optional[int] = None
    auto_create: Optional[int] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    suppress_minutes: Optional[int] = None
    enabled: Optional[int] = None


class WebhookCallback(BaseModel):
    external_ticket_id: str
    status: str
    comment: Optional[str] = None
    operator: Optional[str] = None
    update_time: Optional[str] = None
