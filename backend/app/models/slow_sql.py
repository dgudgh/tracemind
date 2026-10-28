from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, DECIMAL, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBSlowSQL(Base):
    __tablename__ = "db_slow_sql"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sql_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    executions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    avg_elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    max_elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    total_elapsed_ms: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    logical_reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    physical_reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_processed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    involved_tables: Mapped[str | None] = mapped_column(Text, nullable=True)
    mes_module: Mapped[str | None] = mapped_column(String(100), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    optimize_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)

    capture_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
