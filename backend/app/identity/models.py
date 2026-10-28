from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class IdentityProvider(Base):
    __tablename__ = "identity_provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class GroupMapping(Base):
    __tablename__ = "identity_group_mapping"
    __table_args__ = (UniqueConstraint("provider_id", "external_group", name="uq_identity_group_mapping"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_group: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    module_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ExternalIdentity(Base):
    __tablename__ = "external_identity"
    __table_args__ = (UniqueConstraint("provider_id", "subject", name="uq_external_identity_subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    groups: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class LoginAudit(Base):
    __tablename__ = "identity_login_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    masked_username: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


IdentityGroupMapping = GroupMapping
