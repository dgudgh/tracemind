from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, DECIMAL, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBPerfMetric(Base):
    """性能指标时序快照 — 用于建立日/周/月基线并判断偏离。"""

    __tablename__ = "db_perf_metric"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    value: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 4), nullable=True)
    capture_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
