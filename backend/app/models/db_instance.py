from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.secrets import local_secret_requires_rotation, secret_source


class DBInstance(Base):
    __tablename__ = "db_instance"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_name: Mapped[str] = mapped_column(String(100), nullable=False)
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    db_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    patch_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str] = mapped_column(String(100), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)

    oracle_connect_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    service_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sslmode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    encrypt: Mapped[str | None] = mapped_column(String(20), nullable=True)
    trust_server_certificate: Mapped[str | None] = mapped_column(String(20), nullable=True)
    driver_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    certificate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("connection_certificate_profile.id"), nullable=True
    )
    tls_server_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tls_verify: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    account_type: Mapped[str | None] = mapped_column(String(30), nullable=True, default="system")

    username: Mapped[str] = mapped_column(String(100), nullable=False)
    password_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    password_rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    env_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    architecture_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    role_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    system_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    factory_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    line_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)

    ssh_host: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=22)
    ssh_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ssh_password_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    ssh_password_rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    certificate_profile = relationship("ConnectionCertificateProfile", lazy="select")

    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    @property
    def has_password(self) -> bool:
        return bool(self.password_cipher)

    @property
    def credential_source(self) -> str:
        return secret_source(self.password_cipher)

    @property
    def credential_requires_rotation(self) -> bool:
        return local_secret_requires_rotation(self.password_cipher)

    @property
    def has_ssh_password(self) -> bool:
        return bool(self.ssh_password_cipher)

    @property
    def ssh_credential_source(self) -> str:
        return secret_source(self.ssh_password_cipher)

    @property
    def ssh_credential_requires_rotation(self) -> bool:
        return local_secret_requires_rotation(self.ssh_password_cipher)


class DBInstanceCredentialAudit(Base):
    __tablename__ = "db_instance_credential_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    credential_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    source_before: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_after: Mapped[str | None] = mapped_column(String(30), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
