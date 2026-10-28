from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Tenant(Base):
    __tablename__ = "security_tenant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Organization(Base):
    __tablename__ = "security_organization"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_security_organization_tenant_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    organization_type: Mapped[str] = mapped_column(String(30), nullable=False, default="department", index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class UserOrganization(Base):
    __tablename__ = "security_user_organization"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_security_user_organization"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    is_primary: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AccessRole(Base):
    __tablename__ = "security_access_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    builtin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class RoleActionPermission(Base):
    __tablename__ = "security_role_action_permission"
    __table_args__ = (UniqueConstraint("role_id", "action_key", name="uq_security_role_action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserAccessRole(Base):
    __tablename__ = "security_user_access_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_security_user_access_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserAssetScope(Base):
    __tablename__ = "security_user_asset_scope"
    __table_args__ = (
        UniqueConstraint("user_id", "scope_type", "scope_id", name="uq_security_user_asset_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    access_level: Mapped[str] = mapped_column(String(20), nullable=False, default="read", index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuthSession(Base):
    __tablename__ = "security_auth_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jti_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="db", index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    password_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    mfa_verified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    client_ip: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class UserMfaCredential(Base):
    __tablename__ = "security_user_mfa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    secret_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    recovery_code_hashes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    last_totp_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success", index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="system", index=True)
    target_type: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    details: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
