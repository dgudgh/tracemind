from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBWorkloadReport(Base):
    __tablename__ = "db_workload_report"
    __table_args__ = (
        Index("ix_workload_report_instance_time", "instance_id", "create_time"),
        Index("ix_workload_report_engine_status", "db_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("db_instance.id", ondelete="CASCADE"), nullable=False)
    instance_name: Mapped[str] = mapped_column(String(100), nullable=False)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="collecting")
    capability_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    license_status: Mapped[str] = mapped_column(String(30), nullable=False, default="not_required")
    redaction_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="fingerprint_only")
    cost_level: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    row_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    collection_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    load_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    waits_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    top_sql_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    io_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    memory_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    findings_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    capabilities_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    errors_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
