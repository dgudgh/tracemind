from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCheckResult(Base):
    __tablename__ = "db_check_result"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)

    check_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    check_item: Mapped[str | None] = mapped_column(String(100), nullable=True)
    check_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metric_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    threshold_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    finding: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
