"""A recoverable receipt for an atomic set of frozen plans, not execution acceptance."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityPreparation(Base):
    __tablename__ = "db_capability_preparation"

    preparation_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_bindings_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("requested_by", "request_key_hash", name="uq_capability_preparation_request"),
        CheckConstraint("requested_by > 0 AND total >= 1 AND total <= 100 "
                        "AND length(input_hash) = 64 AND length(request_key_hash) = 64 AND length(idempotency_key) > 0",
                        name="ck_capability_preparation_binding"),
    )
