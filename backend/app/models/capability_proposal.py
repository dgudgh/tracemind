"""A read-only receipt for one structurally gated model proposal, never consent to act."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBCapabilityProposal(Base):
    __tablename__ = "db_capability_proposal"

    proposal_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    requested_by: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_config_id: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(200), nullable=False)
    proposal_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("requested_by", "request_key_hash", name="uq_capability_proposal_request"),
        CheckConstraint("requested_by > 0 AND provider_config_id > 0 "
                        "AND length(input_hash) = 64 AND length(request_key_hash) = 64 "
                        "AND length(proposal_hash) = 64 AND length(catalog_hash) = 64 "
                        "AND length(provider_revision) = 64 AND length(idempotency_key) > 0",
                        name="ck_capability_proposal_binding"),
    )
