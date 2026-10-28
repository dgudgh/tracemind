from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, DECIMAL, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBSqlProfile(Base):
    """SQL 画像 — 按 sql_id 聚合的资源画像与自动分析结论。
    数据源：实时 V$SQL / performance_schema，并融合已持久化的 AWR sql_analysis 与慢SQL。"""

    __tablename__ = "db_sql_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)

    sql_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    schema_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    executions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    cpu_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    avg_elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2), nullable=True)
    buffer_gets: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_processed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    involved_objects: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # [{type, detail, severity}]
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # live|awr|slow_sql

    capture_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
