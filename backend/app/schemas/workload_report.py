from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkloadReportCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "auto",
        "dynamic_views",
        "awr",
        "performance_schema",
        "pg_stat_statements",
        "query_store",
        "dynamic_management_views",
    ] = "auto"
    window_minutes: int = Field(60, ge=5, le=1440)
    window_start: str | None = None
    window_end: str | None = None
    row_limit: int = Field(20, ge=5, le=100)
    include_sql_text: bool = False
    sensitive_data_approved: bool = False
    oracle_diagnostics_pack_confirmed: bool = False

    @model_validator(mode="after")
    def validate_sensitive_data_gate(self):
        if self.include_sql_text and not self.sensitive_data_approved:
            raise ValueError("展示 SQL 原文前必须确认已获得敏感数据访问授权")
        if self.source == "awr" and not self.oracle_diagnostics_pack_confirmed:
            raise ValueError("采集 Oracle AWR 前必须确认已获得 Diagnostics Pack 许可")
        return self


class WorkloadReportCompareRequest(BaseModel):
    left_report_id: int = Field(gt=0)
    right_report_id: int = Field(gt=0)


class WorkloadReportListQuery(BaseModel):
    instance_id: int | None = None
    db_type: str | None = None
    status: str | None = None
    limit: int = Field(100, ge=1, le=500)


class WorkloadReportSummary(BaseModel):
    id: int
    report_no: str
    instance_id: int
    instance_name: str
    db_type: str
    source: str
    window_start: datetime
    window_end: datetime
    status: str
    capability_status: str
    license_status: str
    redaction_mode: str
    cost_level: str
    row_limit: int
    collection_duration_ms: int
    error_message: str | None = None
    create_time: datetime
