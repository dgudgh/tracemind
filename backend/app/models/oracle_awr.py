import logging
from datetime import datetime

from sqlalchemy import Integer, String, DateTime, Text, BigInteger, Float, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

logger = logging.getLogger("app.models.oracle_awr")


class AwrReport(Base):
    __tablename__ = "db_awr_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    db_name: Mapped[str] = mapped_column(String(100), default="")
    dbid: Mapped[str] = mapped_column(String(100), default="")
    instance_number: Mapped[int] = mapped_column(Integer, default=1)
    instance_name: Mapped[str] = mapped_column(String(100), default="")
    host_name: Mapped[str] = mapped_column(String(100), default="")
    report_type: Mapped[str] = mapped_column(String(50), default="INSTANCE")
    report_format: Mapped[str] = mapped_column(String(20), default="HTML")
    begin_snap: Mapped[int] = mapped_column(Integer, default=0)
    end_snap: Mapped[int] = mapped_column(Integer, default=0)
    begin_time: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    file_name: Mapped[str] = mapped_column(String(500), default="")
    file_path: Mapped[str] = mapped_column(String(1000), default="")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    error_message: Mapped[str] = mapped_column(Text, default="")
    # Analysis fields
    performance_score: Mapped[float] = mapped_column(Float, nullable=True)
    cpu_score: Mapped[float] = mapped_column(Float, nullable=True)
    memory_score: Mapped[float] = mapped_column(Float, nullable=True)
    io_score: Mapped[float] = mapped_column(Float, nullable=True)
    analysis_results = mapped_column(JSON, nullable=True)
    recommendations = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
