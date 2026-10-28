from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EcosystemIntegrationConfig(Base):
    __tablename__ = "ecosystem_integration_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    secret_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class EcosystemIntegrationEvent(Base):
    __tablename__ = "ecosystem_integration_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="planned", index=True)
    level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class EcosystemIntegrationPlan(Base):
    __tablename__ = "ecosystem_integration_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    plan_name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="plan_only", index=True)
    sections_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
