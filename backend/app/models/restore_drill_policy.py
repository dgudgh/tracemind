from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBRestoreDrillPolicy(Base):
    __tablename__ = "db_restore_drill_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_backup_policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    drill_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    schedule: Mapped[str] = mapped_column(String(100), nullable=False, default="manual")
    rto_target_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retry_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_source_operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class RestoreDrillPolicyAudit(Base):
    __tablename__ = "restore_drill_policy_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
