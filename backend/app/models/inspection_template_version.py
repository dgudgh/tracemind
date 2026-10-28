from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InspectionTemplateVersion(Base):
    """Append-only history, deliberately independent of the live policy lifecycle."""

    __tablename__ = "inspection_template_version"
    __table_args__ = (
        UniqueConstraint("policy_id", "revision", name="uq_inspection_template_version_revision"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    policy_key: Mapped[str] = mapped_column(String(100), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    source_policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


@event.listens_for(InspectionTemplateVersion, "before_update")
@event.listens_for(InspectionTemplateVersion, "before_delete")
def _reject_history_mutation(mapper, connection, target):
    raise ValueError("Inspection template versions are immutable")
