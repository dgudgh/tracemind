from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, DECIMAL, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapacityPolicy(Base):
    __tablename__ = "db_capacity_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    frequency_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    forecast_days_threshold: Mapped[int] = mapped_column(Integer, default=15)
    abnormal_growth_mb: Mapped[Decimal] = mapped_column(DECIMAL(18, 2), default=10240)
    abnormal_growth_pct: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), default=30)
    retention_days: Mapped[int] = mapped_column(Integer, default=180)
    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Anti-flap controls (ISS-25). Without them a single sample decides the outcome: a metric
    # that oscillates around its threshold opens and closes an alert on every run.
    sustained_minutes: Mapped[int] = mapped_column(Integer, default=0)
    recovery_margin_pct: Mapped[Decimal] = mapped_column(DECIMAL(8, 2), default=10)
    last_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    data_freshness_status: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBCapacityAlert(Base):
    __tablename__ = "db_capacity_alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), default="tablespace")
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    metric_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    threshold_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    isolate_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    isolate_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    #: First time the breach was old enough to be published. Empty means "seen, but still inside
    #: the sustained window" - the row exists so the streak is observable, not yet alertable.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
