from datetime import datetime

from sqlalchemy import DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBSuggestion(Base):
    """统一优化建议表 — 各性能模块(AWR/慢SQL/容量/巡检/锁/AI)的产出归一化到这里，
    形成"发现问题 → 建议 → 工单"闭环的中枢。每个来源只负责写入，画像/总览只负责读取。"""

    __tablename__ = "db_suggestion"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # 来源与去重键: awr | slow_sql | capacity | inspection | lock | ai
    source: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)

    problem_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="medium", index=True)  # high|medium|low
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open|acked|converted|closed
    ticket_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
