from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, DECIMAL, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DBTableProfile(Base):
    """表画像 — 表大小/行数/访问与 DML 计数/全表扫描，及自动建议。"""

    __tablename__ = "db_table_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(nullable=False, index=True)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)

    schema_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    table_name: Mapped[str] = mapped_column(String(200), nullable=False)

    size_mb: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    index_mb: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 2), nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    reads: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    writes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    inserts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updates: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deletes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    full_scans: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    issues: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    capture_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
