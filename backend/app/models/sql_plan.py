from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, DECIMAL, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBSqlPlan(Base):
    """Unified SQL execution plan snapshot for Oracle/MySQL/PostgreSQL/SQL Server."""

    __tablename__ = "db_sql_plan"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sql_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    child_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)

    plan_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_tree: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    plan_summary: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    cost: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    rows_estimate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    executions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    buffer_gets: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    capture_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DBSqlPlanHistory(Base):
    """Daily or sampling history of a SQL plan and resource profile."""

    __tablename__ = "db_sql_plan_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sql_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    schema_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    executions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    cpu_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    buffer_gets: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_processed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DBSqlPlanRisk(Base):
    """Detected risks in a SQL execution plan."""

    __tablename__ = "db_sql_plan_risk"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sql_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    risk_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    operation: Mapped[str | None] = mapped_column(String(160), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DBSqlPlanAdvice(Base):
    """Plan-only tuning advice generated from SQL profile and plan risks."""

    __tablename__ = "db_sql_plan_advice"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sql_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    advice_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="plan_only")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
