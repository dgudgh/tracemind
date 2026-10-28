from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIReport(Base):
    __tablename__ = "db_ai_report"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    user_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    diagnosis: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
