from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AIDiagnosisRequest(BaseModel):
    instance_id: int | None = None
    db_type: str = Field(..., pattern="^(oracle|mysql|postgresql|sqlserver)$")
    instance: str
    time_range: str | None = None
    metrics: dict = Field(default_factory=dict)
    user_question: str | None = None


class AIDiagnosisResponse(BaseModel):
    summary: str
    risk_level: str
    root_causes: list[str]
    evidences: list[str]
    business_impact: list[str]
    suggestions: list[str]
    sql_optimization: list[str]
    operation_steps: list[str]
    rollback_plan: list[str]
    execution_status: str = "unknown"
    data_quality: str = "missing"
    limitations: list[str] = Field(default_factory=list)


class DiagnosisClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    cause: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class ModelDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=4000)
    risk_level: Literal["normal", "info", "warning", "critical", "unknown"]
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    root_causes: list[DiagnosisClaim] = Field(max_length=8)
    recommendations: list[str] = Field(min_length=1, max_length=12)
    missing_evidence: list[str] = Field(max_length=12)
