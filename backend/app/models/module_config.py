import logging
from datetime import datetime

from sqlalchemy import Integer, String, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

logger = logging.getLogger("app.models.module_config")


class ModuleConfig(Base):
    __tablename__ = "module_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    module_name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="oracle")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
