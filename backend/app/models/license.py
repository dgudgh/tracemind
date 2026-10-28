from datetime import datetime

from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class License(Base):
    __tablename__ = "sys_license"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_code: Mapped[str] = mapped_column(String(128), index=True)
    customer: Mapped[str] = mapped_column(String(200))
    product: Mapped[str] = mapped_column(String(100))
    expire_date: Mapped[str] = mapped_column(String(20))
    features: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
