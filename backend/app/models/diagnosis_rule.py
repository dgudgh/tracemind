from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiagnosisRule(Base):
    __tablename__ = "diagnosis_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    rule_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    condition_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    action_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
