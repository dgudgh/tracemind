from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DBType = Literal["oracle", "mysql", "postgresql", "sqlserver"]
ConfigCategory = Literal["parameter", "capacity", "log_retention", "object_lifecycle"]
Comparator = Literal["eq", "ne", "ge", "le", "gt", "lt", "in", "contains"]
ValueType = Literal["string", "integer", "number", "boolean"]


class ConfigBaselineItemPayload(BaseModel):
    category: ConfigCategory = "parameter"
    parameter_name: str = Field(..., min_length=1, max_length=160)
    desired_value: str = Field(..., max_length=4000)
    comparator: Comparator = "eq"
    value_type: ValueType = "string"
    severity: Literal["info", "warning", "critical"] = "warning"
    restart_required: int = Field(0, ge=0, le=1)
    description: str | None = Field(None, max_length=2000)
    remediation_hint: str | None = Field(None, max_length=4000)
    sort_order: int = Field(0, ge=0, le=100000)

    @field_validator("parameter_name")
    @classmethod
    def normalize_parameter_name(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("字段名称不能为空")
        return normalized


class ConfigBaselineCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    db_type: DBType
    version_pattern: str | None = Field(None, max_length=100)
    scope_type: Literal["global", "environment", "instance"] = "global"
    scope_id: int | None = Field(None, ge=1)
    status: Literal["draft", "active"] = "draft"
    is_default: int = Field(0, ge=0, le=1)
    description: str | None = Field(None, max_length=4000)
    items: list[ConfigBaselineItemPayload] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope_type == "global" and self.scope_id is not None:
            raise ValueError("全局基线不能指定 scope_id")
        if self.scope_type != "global" and self.scope_id is None:
            raise ValueError("环境或实例基线必须指定 scope_id")
        return self


class ConfigBaselineUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=200)
    version_pattern: str | None = Field(None, max_length=100)
    scope_type: Literal["global", "environment", "instance"] | None = None
    scope_id: int | None = Field(None, ge=1)
    status: Literal["draft", "active", "archived"] | None = None
    is_default: int | None = Field(None, ge=0, le=1)
    description: str | None = Field(None, max_length=4000)
    items: list[ConfigBaselineItemPayload] | None = Field(None, max_length=500)


class ConfigEvaluationRequest(BaseModel):
    instance_id: int = Field(..., ge=1)
    observed_parameters: dict[str, Any] | None = None
    source: Literal["live", "manual", "inspection"] = "live"


class ConfigDriftAcceptRequest(BaseModel):
    reason: str = Field(..., min_length=2, max_length=2000)


class ConfigRemediationCreateRequest(BaseModel):
    instance_id: int = Field(..., ge=1)
    baseline_id: int = Field(..., ge=1)
    drift_ids: list[int] | None = Field(None, max_length=500)


class ConfigRemediationSubmitRequest(BaseModel):
    expected_target_identity: str | None = Field(None, max_length=255)
    maintenance_window_start: datetime | None = None
    maintenance_window_end: datetime | None = None
    rollback_script: str | None = Field(None, max_length=100000)

    @model_validator(mode="after")
    def validate_window(self):
        if bool(self.maintenance_window_start) != bool(self.maintenance_window_end):
            raise ValueError("维护窗口开始和结束时间必须同时填写")
        if (
            self.maintenance_window_start
            and self.maintenance_window_end
            and self.maintenance_window_end <= self.maintenance_window_start
        ):
            raise ValueError("维护窗口结束时间必须晚于开始时间")
        return self


class ConfigRemediationApplyRequest(BaseModel):
    confirm: bool = False


class ConfigRemediationVerifyRequest(BaseModel):
    observed_parameters: dict[str, Any] | None = None
