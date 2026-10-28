import os
import secrets
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.runtime.paths import ensure_frozen_runtime_root


def _get_app_dir():
    if getattr(sys, "frozen", False):
        return str(ensure_frozen_runtime_root())
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _data_dir() -> Path:
    if getattr(sys, "frozen", False):
        path = ensure_frozen_runtime_root() / "data"
    else:
        path = Path(_get_app_dir()).parent / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _default_database_url():
    filename = "mesguard.db" if getattr(sys, "frozen", False) else "mesguard-dev.db"
    return f"sqlite:///{str(_data_dir() / filename).replace(os.sep, '/')}"


def _default_db_type():
    return "sqlite"


def _default_app_mode():
    if getattr(sys, "frozen", False):
        return "exe_sqlite"
    return "dev"


def _default_secret_key() -> str:
    secret_path = _data_dir() / ".secret_key"
    try:
        if secret_path.is_file():
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        return value
    except FileExistsError:
        return secret_path.read_text(encoding="utf-8").strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=os.path.join(_get_app_dir(), ".env"))

    DATABASE_URL: str = _default_database_url()
    DB_TYPE: str = _default_db_type()

    API_V1_PREFIX: str = "/api/v1"
    PROJECT_NAME: str = "TraceMind AI 智能运维平台"
    VERSION: str = "1.0.0"
    DEBUG: bool = False
    SQL_ECHO: bool = False
    # New collection via typed plans is released explicitly after site acceptance.
    PLAN_INSPECTION_EXECUTION_ENABLED: bool = False
    # The API process consumes durable capability plans by default. Disable this
    # when deployment runs scripts/capability_worker.py as a separate service.
    CAPABILITY_WORKER_ENABLED: bool = True
    DATABASE_CONNECT_TIMEOUT_SECONDS: int = Field(default=8, ge=1, le=300)
    DATABASE_READ_TIMEOUT_SECONDS: int = Field(default=15, ge=1, le=600)
    DATABASE_WRITE_TIMEOUT_SECONDS: int = Field(default=15, ge=1, le=600)
    DATABASE_POOL_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=300)
    SECRET_KEY: str = _default_secret_key()
    SECRET_KEY_PREVIOUS: str = ""
    SECRETS_MAX_VALUE_BYTES: int = Field(default=65_536, ge=1_024, le=1_048_576)
    SECRETS_RESOLVE_TIMEOUT_SECONDS: int = Field(default=5, ge=1, le=60)
    SECRETS_VAULT_ADDR: str = ""
    SECRETS_VAULT_TOKEN_ENV: str = "TRACEMIND_VAULT_TOKEN"
    SECRETS_VAULT_NAMESPACE: str = ""
    SECRETS_VAULT_VERIFY_TLS: bool = True
    AUTH_TOKEN_EXPIRE_SECONDS: int = Field(default=28_800, ge=300, le=604_800)
    AUTH_SESSION_IDLE_SECONDS: int = Field(default=7_200, ge=300, le=86_400)
    AUTH_MFA_CHALLENGE_SECONDS: int = Field(default=300, ge=60, le=900)
    MFA_ISSUER: str = "TraceMind AI"
    MFA_REQUIRED_FOR_ADMIN: bool = False
    JWT_PRIVATE_KEY_PATH: str = ""
    SECURITY_AUDIT_RETENTION_DAYS: int = Field(default=365, ge=30, le=3650)
    APP_MODE: str = _default_app_mode()
    CORS_ORIGINS: str = "http://127.0.0.1:3300,http://localhost:3300"
    INCIDENT_SNAPSHOT_ENABLED: bool = True
    INCIDENT_SNAPSHOT_STORAGE_DIR: str = ""
    INCIDENT_SNAPSHOT_STORAGE_BACKEND: str = Field(default="local", pattern="^(local|s3)$")
    INCIDENT_SNAPSHOT_S3_ENDPOINT_URL: str = ""
    INCIDENT_SNAPSHOT_S3_BUCKET_NAME: str = ""
    INCIDENT_SNAPSHOT_S3_PREFIX: str = "incident-snapshots"
    INCIDENT_SNAPSHOT_S3_REGION: str = "us-east-1"
    INCIDENT_SNAPSHOT_S3_ACCESS_KEY_SECRET: str = ""
    INCIDENT_SNAPSHOT_S3_SECRET_KEY_SECRET: str = ""
    INCIDENT_SNAPSHOT_S3_VERIFY_TLS: bool = True
    INCIDENT_SNAPSHOT_S3_OBJECT_LOCK_ENABLED: bool = False
    INCIDENT_AGENT_HEARTBEAT_TIMEOUT_SECONDS: int = 120
    INCIDENT_SNAPSHOT_MAX_BYTES: int = 4_294_967_296
    INCIDENT_SNAPSHOT_MAX_ACTIVE_UPLOADS_PER_AGENT: int = 4
    INCIDENT_SNAPSHOT_MAX_ACTIVE_BYTES_PER_AGENT: int = 8_589_934_592
    INCIDENT_SNAPSHOT_MAX_MANIFEST_BYTES: int = 2_097_152
    INCIDENT_SNAPSHOT_STALE_UPLOAD_SECONDS: int = 900
    INCIDENT_COMMAND_SIGNING_KEY_PATH: str = ""
    INCIDENT_COMMAND_PREVIOUS_PUBLIC_KEYS: str = ""
    INCIDENT_AGENT_REQUEST_MAX_SKEW_SECONDS: int = Field(default=300, ge=30, le=3600)
    INCIDENT_AGENT_KEY_LIFETIME_DAYS: int = Field(default=90, ge=1, le=730)
    INCIDENT_AGENT_KEY_ROTATION_OVERLAP_SECONDS: int = Field(default=900, ge=0, le=86_400)
    KERNEL_CORE_OBJECT_SHA256_ALLOWLIST: str = "9206a0e2e6e3703240a4a0bf1405f907568ff926307d83602cad128030a90460"
    RECOVERY_STORAGE_DIR: str = ""
    RECOVERY_AGENT_HEARTBEAT_TIMEOUT_SECONDS: int = Field(default=120, ge=15, le=3600)
    RECOVERY_UPLOAD_CHUNK_MAX_BYTES: int = Field(default=8_388_608, ge=65_536, le=67_108_864)
    RECOVERY_UPLOAD_MAX_BYTES: int = Field(default=4_294_967_296_000, ge=1_048_576)
    ARCHIVE_EXECUTION_STORAGE_DIR: str = ""
    ARCHIVE_EXPORT_MAX_ROWS: int = Field(default=1_000_000, ge=1_000, le=100_000_000)
    ARCHIVE_EXPORT_SAMPLE_ROWS: int = Field(default=20, ge=1, le=1_000)
    ARCHIVE_RECOVERY_MAX_BYTES: int = Field(default=4_294_967_296, ge=1_048_576)
    ARCHIVE_ENCRYPTION_KEY: str = Field(default="", repr=False, exclude=True)
    ARCHIVE_ENCRYPTION_KEY_PREVIOUS: str = Field(default="{}", repr=False, exclude=True)
    ARCHIVE_ALLOW_UNENCRYPTED_EXPORTS: bool = False
    ARCHIVE_STORAGE_COST_USD_PER_GB_MONTH: float = Field(default=0.023, ge=0, le=100)
    ARCHIVE_EXECUTION_COST_USD_PER_GB: float = Field(default=0.01, ge=0, le=100)

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def secret_key_previous(self) -> list[str]:
        return [value.strip() for value in self.SECRET_KEY_PREVIOUS.split(",") if value.strip()]

    @property
    def kernel_core_object_sha256_allowlist(self) -> set[str]:
        values = {
            value.strip().lower()
            for value in self.KERNEL_CORE_OBJECT_SHA256_ALLOWLIST.split(",")
            if value.strip()
        }
        return {
            value
            for value in values
            if len(value) == 64 and all(char in "0123456789abcdef" for char in value)
        }

    @property
    def jwt_private_key_path(self) -> Path:
        if self.JWT_PRIVATE_KEY_PATH.strip():
            return Path(self.JWT_PRIVATE_KEY_PATH).expanduser().resolve()
        return _data_dir() / ".auth_signing_key.pem"

    @property
    def archive_execution_storage_dir(self) -> Path:
        path = (
            Path(self.ARCHIVE_EXECUTION_STORAGE_DIR).expanduser()
            if self.ARCHIVE_EXECUTION_STORAGE_DIR.strip()
            else _data_dir() / "archives"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @property
    def incident_command_signing_key_path(self) -> Path:
        path = (
            Path(self.INCIDENT_COMMAND_SIGNING_KEY_PATH).expanduser()
            if self.INCIDENT_COMMAND_SIGNING_KEY_PATH.strip()
            else _data_dir() / ".incident_command_signing_key.pem"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @property
    def incident_snapshot_s3_prefix(self) -> str:
        return "/".join(
            part for part in self.INCIDENT_SNAPSHOT_S3_PREFIX.strip().strip("/").split("/")
            if part
        )

    @property
    def recovery_storage_dir(self) -> Path:
        path = (
            Path(self.RECOVERY_STORAGE_DIR).expanduser()
            if self.RECOVERY_STORAGE_DIR.strip()
            else _data_dir() / "recovery"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

settings = Settings()
