from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBInspectionPolicy(Base):
    """Unified inspection policy for templates, items, thresholds and actions."""

    __tablename__ = "db_inspection_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    db_types_json: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    categories_json: Mapped[list] = mapped_column(JSON, default=list)
    item_config_json: Mapped[list] = mapped_column(JSON, default=list)
    threshold_config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    action_config_json: Mapped[list] = mapped_column(JSON, default=list)
    report_config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    read_only: Mapped[int] = mapped_column(Integer, default=0)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
