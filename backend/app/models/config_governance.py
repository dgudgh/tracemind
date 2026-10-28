from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBConfigBaseline(Base):
    __tablename__ = "db_config_baseline"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    db_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    version_pattern: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="global", index=True)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBConfigBaselineItem(Base):
    __tablename__ = "db_config_baseline_item"
    __table_args__ = (
        UniqueConstraint("baseline_id", "category", "parameter_name", name="uq_config_baseline_item_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    baseline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="parameter", index=True)
    parameter_name: Mapped[str] = mapped_column(String(160), nullable=False)
    desired_value: Mapped[str] = mapped_column(Text, nullable=False)
    comparator: Mapped[str] = mapped_column(String(20), nullable=False, default="eq")
    value_type: Mapped[str] = mapped_column(String(20), nullable=False, default="string")
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning")
    restart_required: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    remediation_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBConfigSnapshot(Base):
    __tablename__ = "db_config_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    baseline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    baseline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="live", index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="success", index=True)
    observed_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expected_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    observed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compliant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    drift_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collector_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DBConfigDrift(Base):
    __tablename__ = "db_config_drift"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "baseline_id",
            "category",
            "parameter_name",
            name="uq_config_drift_current_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    baseline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    baseline_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    parameter_name: Mapped[str] = mapped_column(String(160), nullable=False)
    comparator: Mapped[str] = mapped_column(String(20), nullable=False)
    desired_value: Mapped[str] = mapped_column(Text, nullable=False)
    actual_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning", index=True)
    restart_required: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open", index=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DBConfigRemediation(Base):
    __tablename__ = "db_config_remediation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    baseline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    drift_ids_json: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    plan_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rollback_script: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_script: Mapped[str] = mapped_column(Text, nullable=False)
    validation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", index=True)
    execution_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="approval_required")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    automation_plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    approval_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    apply_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    applied_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DBConfigGovernanceAudit(Base):
    __tablename__ = "db_config_governance_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
