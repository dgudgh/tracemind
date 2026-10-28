from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiagnosisResult(Base):
    __tablename__ = "diagnosis_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    instance_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    rule_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=80)
    object_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evidence_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
