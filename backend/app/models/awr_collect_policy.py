from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBAwrCollectPolicy(Base):
    """AWR 自动采集策略 — 定时导出最近快照区间的 AWR 并分析入库。"""

    __tablename__ = "db_awr_collect_policy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    days_window: Mapped[int] = mapped_column(Integer, default=1)
    report_format: Mapped[str] = mapped_column(String(20), default="HTML")
    report_type: Mapped[str] = mapped_column(String(50), default="INSTANCE")
    auto_analyze: Mapped[int] = mapped_column(Integer, default=1)

    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
