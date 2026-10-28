from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NotifyChannel(Base):
    __tablename__ = "notify_channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    channel_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class NotifyPolicy(Base):
    __tablename__ = "notify_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    level_filter: Mapped[str] = mapped_column(String(200), default="all")
    metric_filter: Mapped[str | None] = mapped_column(String(500), nullable=True)
    time_filter: Mapped[str | None] = mapped_column(String(200), nullable=True)
    throttle_minutes: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class NotifyPolicyChannel(Base):
    __tablename__ = "notify_policy_channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class NotifyTemplate(Base):
    __tablename__ = "notify_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    channel_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title_template: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class NotifyEvent(Base):
    __tablename__ = "notify_event"
    __table_args__ = (UniqueConstraint("event_key", name="uq_notify_event_event_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    metric_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class NotifyLog(Base):
    __tablename__ = "notify_log"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "policy_id",
            "channel_id",
            "recipient",
            name="uq_notify_log_delivery",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(String(50), nullable=False)
    recipient: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class NotifyThrottle(Base):
    __tablename__ = "notify_throttle"
    __table_args__ = (
        UniqueConstraint("policy_id", "throttle_key", name="uq_notify_throttle_policy_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    throttle_key: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    last_sent_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotifyRecipient(Base):
    __tablename__ = "notify_recipient"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(50), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
