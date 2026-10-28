from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBPerfMetricPolicy(Base):
    """性能基线采集策略 — 每实例一条，由统一定时任务调度。"""

    __tablename__ = "db_perf_metric_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)

    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
