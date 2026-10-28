"""端到端智能诊断中心 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class DiagnoseRequest(BaseModel):
    instance_id: int
    title: str | None = None
    symptom: str | None = None
    intent_summary: str | None = None
    intent_signals: list[str] | None = None
    recommended_cards: list[str] | None = None
    trigger_source: str = "manual"
    create_ticket: bool = False
    notify: bool = False


class TicketFromIncidentRequest(BaseModel):
    config_id: int | None = None
    priority: str | None = None
    assignee: str | None = None


class ReviewCreate(BaseModel):
    root_cause: str | None = None
    process: str | None = None
    impact_scope: str | None = None
    recovery_minutes: int | None = Field(None, ge=0)
    optimizations: list[str] | None = None
    reviewer: str | None = None
    sediment: bool = False  # 是否沉淀为方法论


class MethodologyCreate(BaseModel):
    code: str | None = None
    title: str = Field(..., max_length=200)
    symptom: str | None = None
    layer: str | None = None
    applicable_arch: list[str] | None = None
    entry_point: str | None = None
    troubleshoot_path: list[str] | None = None
    check_sql: str | None = None
    check_command: str | None = None
    judge_logic: str | None = None
    solution: str | None = None
    risk: str | None = None
    review_note: str | None = None
    keywords: list[str] | None = None
    prior_prob: float = Field(0.5, ge=0, le=1)
    tags: list[str] | None = None
    enabled: int = Field(1, ge=0, le=1)


class MethodologyUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    symptom: str | None = None
    layer: str | None = None
    applicable_arch: list[str] | None = None
    entry_point: str | None = None
    troubleshoot_path: list[str] | None = None
    check_sql: str | None = None
    check_command: str | None = None
    judge_logic: str | None = None
    solution: str | None = None
    risk: str | None = None
    review_note: str | None = None
    keywords: list[str] | None = None
    prior_prob: float | None = Field(None, ge=0, le=1)
    tags: list[str] | None = None
    enabled: int | None = Field(None, ge=0, le=1)


class AutoPolicyUpdate(BaseModel):
    enabled: int = Field(0, ge=0, le=1)
    mode: str = Field("alert")  # alert/scheduled/both
    interval_minutes: int = Field(10, ge=1, le=1440)
    cooldown_minutes: int = Field(120, ge=0, le=10080)
    min_severity: str = Field("critical")  # warning/critical
    auto_ticket: int = Field(0, ge=0, le=1)
    auto_notify: int = Field(0, ge=0, le=1)


class DataSourceCreate(BaseModel):
    name: str = Field(..., max_length=120)
    source_type: str = Field(..., max_length=30)
    endpoint: str | None = None
    auth: dict | None = None  # 明文认证信息，落库前加密
    config_json: dict | None = None
    instance_id: int | None = None
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = None


class DataSourceUpdate(BaseModel):
    name: str | None = Field(None, max_length=120)
    source_type: str | None = Field(None, max_length=30)
    endpoint: str | None = None
    auth: dict | None = None
    config_json: dict | None = None
    instance_id: int | None = None
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = None
