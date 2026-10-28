"""Immutable binding between one durable execution and its domain result."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityEffectReceipt(Base):
    __tablename__ = "db_capability_effect_receipt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    capability_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, index=True)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint(
            "definition_version > 0 AND requested_by > 0 "
            "AND length(target_hash) = 64 AND length(params_hash) = 64 "
            "AND length(result_hash) = 64",
            name="ck_capability_effect_receipt_binding",
        ),
    )


def _immutable(*_args, **_kwargs):
    raise ValueError("capability_effect_receipt_immutable")


event.listen(DBCapabilityEffectReceipt, "before_update", _immutable)
event.listen(DBCapabilityEffectReceipt, "before_delete", _immutable)
