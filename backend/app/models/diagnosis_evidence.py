from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiagnosisEvidence(Base):
    __tablename__ = "diagnosis_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    diagnosis_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    evidence_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    evidence_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evidence_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
