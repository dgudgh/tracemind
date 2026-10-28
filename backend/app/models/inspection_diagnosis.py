from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBInspectionTask(Base):
    __tablename__ = "db_inspection_task"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    overall_score: Mapped[int | None] = mapped_column(nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    critical_count: Mapped[int] = mapped_column(default=0)
    warning_count: Mapped[int] = mapped_column(default=0)
    info_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DBInspectionModuleResult(Base):
    __tablename__ = "db_inspection_module_result"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    module_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    score: Mapped[int | None] = mapped_column(nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    top_risks: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    suggestions: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DBDiagnosisFinding(Base):
    __tablename__ = "db_diagnosis_finding"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    module_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    check_item: Mapped[str | None] = mapped_column(String(200), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    risk_score: Mapped[int | None] = mapped_column(nullable=True)
    finding: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    root_causes: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    business_impact: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    suggestions: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    operation_plan: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    rollback_plan: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    related_objects: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    related_sql: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
