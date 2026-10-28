from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DiagnosisRuleCreate(BaseModel):
    rule_code: str = Field(..., max_length=100)
    rule_name: str = Field(..., max_length=200)
    category: str | None = None
    severity: str | None = "warning"
    enabled: int = 1
    condition_json: dict[str, Any] | list[Any] | None = None
    action_json: dict[str, Any] | list[Any] | None = None
    description: str | None = None


class DiagnosisRuleUpdate(BaseModel):
    rule_name: str | None = None
    category: str | None = None
    severity: str | None = None
    enabled: int | None = None
    condition_json: dict[str, Any] | list[Any] | None = None
    action_json: dict[str, Any] | list[Any] | None = None
    description: str | None = None


class DiagnosisRunRequest(BaseModel):
    instance_id: int
    source: str = "inspection"
    persist: bool = True


class DiagnosisResultOut(BaseModel):
    id: int | None = None
    run_id: str | None = None
    instance_id: int | None = None
    db_type: str | None = None
    instance_name: str | None = None
    rule_code: str
    rule_name: str
    category: str | None = None
    severity: str
    confidence: int
    object_type: str | None = None
    object_name: str | None = None
    evidence: list[str] = Field(default_factory=list)
    root_cause: str | None = None
    impact: str | None = None
    suggestion: str | None = None
    actions: list[str] = Field(default_factory=list)
    status: str | None = None
    create_time: datetime | None = None
