from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, DECIMAL, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBAlertEvent(Base):
    __tablename__ = "db_alert_event"
    __table_args__ = (
        UniqueConstraint("event_uid", name="uq_db_alert_event_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_uid: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    correlation_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning", index=True)
    status: Mapped[str] = mapped_column(String(30), default="firing", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    object_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    metric_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    metric_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 4), nullable=True)
    threshold_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 4), nullable=True)
    labels_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    annotations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    suppress_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    maintenance_window_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    impact_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBAlertActionLog(Base):
    __tablename__ = "db_alert_action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    action_status: Mapped[str] = mapped_column(String(30), default="plan_only", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    target: Mapped[str | None] = mapped_column(String(300), nullable=True)
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    dead_letter_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DBAlertSourceLink(Base):
    __tablename__ = "db_alert_source_link"
    __table_args__ = (
        UniqueConstraint("source", "source_event_uid", name="uq_db_alert_source_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_event_uid: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class DBAlertLifecycleLog(Base):
    __tablename__ = "db_alert_lifecycle_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DBAlertMaintenanceWindow(Base):
    __tablename__ = "db_alert_maintenance_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    object_name_pattern: Mapped[str | None] = mapped_column(String(300), nullable=True)
    metric_name_pattern: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
