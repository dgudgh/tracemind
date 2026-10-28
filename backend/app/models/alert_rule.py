from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBAlertRule(Base):
    __tablename__ = "db_alert_rule"
    __table_args__ = (
        UniqueConstraint("rule_name", "metric_name", "level", name="uq_alert_rule_name_metric_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    rule_type: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    metric_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    consecutive_times: Mapped[int] = mapped_column(Integer, default=1)
    operator: Mapped[str | None] = mapped_column(String(20), nullable=True)
    threshold: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    source: Mapped[str | None] = mapped_column(String(100), default="告警规则.xlsx")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
