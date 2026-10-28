from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiagnosisEvalDataset(Base):
    __tablename__ = "diagnosis_eval_dataset"
    __table_args__ = (UniqueConstraint("name", "version_no", name="uq_diag_eval_dataset_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)
    is_builtin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagnosisEvalCase(Base):
    __tablename__ = "diagnosis_eval_case"
    __table_args__ = (UniqueConstraint("dataset_id", "case_key", name="uq_diag_eval_case_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    case_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    db_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(40), nullable=False, default="database", index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning")
    input_json: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    expected_root_causes: Mapped[list] = mapped_column(JSON, nullable=False)
    required_evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    counter_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    forbidden_claims: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expected_impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expert_label_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expert_label_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    source_incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagnosisEvalRun(Base):
    __tablename__ = "diagnosis_eval_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    provider_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="rule", index=True)
    model_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False, default="evidence-contract-v1")
    engine_version: Mapped[str] = mapped_column(String(80), nullable=False, default="rca-v1")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", index=True)
    threshold_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    case_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    case_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    case_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top1_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    top3_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    ungrounded_fact_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    rule_fallback_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    p95_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DiagnosisEvalCaseResult(Base):
    __tablename__ = "diagnosis_eval_case_result"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_diag_eval_run_case"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success", index=True)
    expected_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    predictions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rejected_predictions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    top1_hit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top3_hit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_coverage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ungrounded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ungrounded_fact_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rule_fallback_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    degraded_to_rule: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DiagnosisEvalAudit(Base):
    __tablename__ = "diagnosis_eval_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    before_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
