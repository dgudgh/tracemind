from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBIndexFinding(Base):
    """索引分析发现 — 失效/重复/未使用/低效/缺失索引。"""

    __tablename__ = "db_index_finding"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # unused | redundant | duplicate | invalid | low_efficiency | missing
    finding_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    schema_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    table_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    index_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    severity: Mapped[str] = mapped_column(String(20), default="medium")  # high|medium|low
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    ddl: Mapped[str | None] = mapped_column(Text, nullable=True)

    capture_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
