from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, DECIMAL, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBObjectSizeDaily(Base):
    __tablename__ = "db_object_size_daily"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    row_count: Mapped[int | None] = mapped_column(nullable=True)
    data_size_mb: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    index_size_mb: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    total_size_mb: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    capacity_limit_known: Mapped[int] = mapped_column(Integer, default=1)
    stat_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
