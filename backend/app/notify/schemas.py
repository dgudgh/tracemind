from typing import Optional

from pydantic import BaseModel, Field


class ChannelCreate(BaseModel):
    channel_name: str = Field(..., max_length=200)
    channel_type: str = Field(..., max_length=50)
    config_json: str
    description: Optional[str] = None
    enabled: int = 1


class ChannelUpdate(BaseModel):
    channel_name: Optional[str] = Field(None, max_length=200)
    channel_type: Optional[str] = Field(None, max_length=50)
    config_json: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[int] = None


class PolicyCreate(BaseModel):
    policy_name: str = Field(..., max_length=200)
    level_filter: str = "all"
    metric_filter: Optional[str] = None
    time_filter: Optional[str] = None
    throttle_minutes: int = 10
    channel_ids: list[int] = []
    enabled: int = 1


class PolicyUpdate(BaseModel):
    policy_name: Optional[str] = Field(None, max_length=200)
    level_filter: Optional[str] = None
    metric_filter: Optional[str] = None
    time_filter: Optional[str] = None
    throttle_minutes: Optional[int] = None
    channel_ids: Optional[list[int]] = None
    enabled: Optional[int] = None


class TemplateCreate(BaseModel):
    template_name: str = Field(..., max_length=200)
    channel_type: str = Field(..., max_length=50)
    title_template: Optional[str] = Field(None, max_length=500)
    body_template: str
    description: Optional[str] = None


class TemplateUpdate(BaseModel):
    template_name: Optional[str] = Field(None, max_length=200)
    channel_type: Optional[str] = Field(None, max_length=50)
    title_template: Optional[str] = Field(None, max_length=500)
    body_template: Optional[str] = None
    description: Optional[str] = None


class RecipientCreate(BaseModel):
    name: str = Field(..., max_length=200)
    channel_type: str = Field(..., max_length=50)
    address: str = Field(..., max_length=500)
    description: Optional[str] = None


class EventCreate(BaseModel):
    event_key: Optional[str] = Field(None, max_length=160)
    event_type: str = Field(..., max_length=100)
    level: str = Field(..., max_length=20)
    metric_name: Optional[str] = None
    instance_id: Optional[int] = None
    title: str = Field(..., max_length=500)
    body: Optional[str] = None
    extra_json: Optional[str] = None


class ChannelTestMessage(BaseModel):
    channel_id: int
    title: str = "Test Notification"
    content: str = "This is a test notification from TraceMind AI 智能运维平台."
