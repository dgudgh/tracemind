from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.secrets import secret_source


class ConnectionCertificateProfile(Base):
    __tablename__ = "connection_certificate_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    profile_type: Mapped[str] = mapped_column(String(30), nullable=False, default="pem")
    ca_certificate_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_certificate_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    wallet_directory: Mapped[str | None] = mapped_column(String(500), nullable=True)
    wallet_password_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    not_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key_cipher)

    @property
    def private_key_source(self) -> str:
        return secret_source(self.private_key_cipher)

    @property
    def has_wallet_password(self) -> bool:
        return bool(self.wallet_password_cipher)

    @property
    def wallet_password_source(self) -> str:
        return secret_source(self.wallet_password_cipher)

    @property
    def lifecycle_status(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.not_after:
            return "not_applicable" if self.profile_type == "oracle_wallet" else "unknown"
        expires_at = self.not_after
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if expires_at <= now:
            return "expired"
        if (expires_at - now).days <= 30:
            return "expiring"
        return "valid"


class ConnectionCertificateAudit(Base):
    __tablename__ = "connection_certificate_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
