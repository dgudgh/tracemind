from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ItsmConfig(Base):
    __tablename__ = "itsm_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(50), default="TOKEN")
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    password_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=15)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ItsmFieldMapping(Base):
    __tablename__ = "itsm_field_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mesguard_field: Mapped[str] = mapped_column(String(100), nullable=False)
    external_field: Mapped[str] = mapped_column(String(200), nullable=False)
    default_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    required: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ItsmTicket(Base):
    __tablename__ = "itsm_ticket"
    __table_args__ = (
        UniqueConstraint(
            "config_id",
            "idempotency_key",
            name="uq_itsm_ticket_config_idempotency_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    external_ticket_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    external_ticket_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    provider_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    config_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source_ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)

    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    db_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sub_category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    mesguard_status: Mapped[str] = mapped_column(String(50), default="created", index=True)
    external_status: Mapped[str | None] = mapped_column(String(100), nullable=True)

    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reporter: Mapped[str | None] = mapped_column(String(200), nullable=True)

    create_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    close_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ItsmTicketSyncLog(Base):
    __tablename__ = "itsm_ticket_sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    external_ticket_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sync_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sync_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    old_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ItsmTicketRule(Base):
    __tablename__ = "itsm_ticket_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    alert_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    db_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    env_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_create: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    suppress_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
