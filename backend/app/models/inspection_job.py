from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBInspectionJob(Base):
    __tablename__ = "db_inspection_job"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    policy_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    parent_job_id: Mapped[str | None] = mapped_column(String(30), unique=True, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    progress_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Who asked for the cancel and when. Nullable because every job queued before the
    # control-plane migration had no way to record it; guessing a timestamp would be
    # worse than admitting the attribution is unknown.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # The queue's own ordering key: every list/reaper query scans this column.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
