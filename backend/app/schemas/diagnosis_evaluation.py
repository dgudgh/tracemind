from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class DiagnosisExpectedCausePayload(BaseModel):
    cause_code: str = Field(..., min_length=2, max_length=120)
    label: str = Field(..., min_length=2, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("cause_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


class DiagnosisEvalDatasetCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    description: str | None = Field(None, max_length=4000)
    owner: str | None = Field(None, max_length=100)


class DiagnosisEvalCaseCreate(BaseModel):
    case_key: str = Field(..., min_length=2, max_length=80)
    title: str = Field(..., min_length=2, max_length=240)
    db_type: Literal["oracle", "mysql", "postgresql", "sqlserver", "generic"]
    domain: Literal["database", "host", "network", "storage", "application"] = "database"
    severity: Literal["info", "warning", "critical"] = "warning"
    input_json: dict[str, Any]
    expected_root_causes: list[DiagnosisExpectedCausePayload] = Field(..., min_length=1, max_length=20)
    required_evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    counter_evidence: list[str] = Field(default_factory=list, max_length=100)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=100)
    expected_impact: str | None = Field(None, max_length=4000)
    validation_steps: list[str] = Field(default_factory=list, max_length=100)
    expert_label_by: str | None = Field(None, max_length=100)
    status: Literal["draft", "active", "disabled"] = "active"

    @field_validator("case_key")
    @classmethod
    def normalize_case_key(cls, value: str) -> str:
        return value.strip().lower().replace(" ", "-")


class DiagnosisEvalCaseUpdate(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=240)
    severity: Literal["info", "warning", "critical"] | None = None
    input_json: dict[str, Any] | None = None
    expected_root_causes: list[DiagnosisExpectedCausePayload] | None = Field(None, min_length=1, max_length=20)
    required_evidence_refs: list[str] | None = Field(None, max_length=100)
    counter_evidence: list[str] | None = Field(None, max_length=100)
    forbidden_claims: list[str] | None = Field(None, max_length=100)
    expected_impact: str | None = Field(None, max_length=4000)
    validation_steps: list[str] | None = Field(None, max_length=100)
    expert_label_by: str | None = Field(None, max_length=100)
    status: Literal["draft", "active", "disabled"] | None = None


class DiagnosisEvalRunRequest(BaseModel):
    dataset_id: int = Field(..., ge=1)
    mode: Literal["rule", "model", "hybrid"] = "rule"
    provider_config_id: int | None = Field(None, ge=1)
    prompt_version: str = Field("evidence-contract-v1", min_length=2, max_length=80)
    engine_version: str = Field("rca-v1", min_length=2, max_length=80)
    predictions_by_case: dict[str, list[dict[str, Any]]] | None = None


class DiagnosisEvalIncidentCaseRequest(BaseModel):
    dataset_id: int = Field(..., ge=1)
    case_key: str | None = Field(None, max_length=80)
    expert_root_cause_code: str = Field(..., min_length=2, max_length=120)
    expert_root_cause_label: str = Field(..., min_length=2, max_length=300)
    expert_label_by: str | None = Field(None, max_length=100)
    validation_steps: list[str] = Field(default_factory=list, max_length=100)


class DiagnosisEvalDatasetCloneRequest(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=160)
    description: str | None = Field(None, max_length=4000)
