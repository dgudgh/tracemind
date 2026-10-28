from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class CertificateProfileCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    profile_type: str = Field("pem", pattern="^(pem|oracle_wallet)$")
    ca_certificate_pem: str | None = Field(None, max_length=262_144)
    client_certificate_pem: str | None = Field(None, max_length=262_144)
    private_key: str | None = Field(None, max_length=262_144)
    private_key_secret_ref: str | None = Field(None, max_length=500)
    wallet_directory: str | None = Field(None, max_length=500)
    wallet_password: str | None = Field(None, max_length=65_536)
    wallet_password_secret_ref: str | None = Field(None, max_length=500)
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def validate_profile(self):
        if self.private_key and self.private_key_secret_ref:
            raise ValueError("客户端私钥与外部私钥引用不能同时提交")
        if self.wallet_password and self.wallet_password_secret_ref:
            raise ValueError("Wallet 密码与外部秘密引用不能同时提交")
        if self.profile_type == "pem" and not self.ca_certificate_pem and not self.client_certificate_pem:
            raise ValueError("PEM 证书配置至少需要 CA 或客户端证书")
        if self.profile_type == "pem" and self.client_certificate_pem and not (self.private_key or self.private_key_secret_ref):
            raise ValueError("客户端证书必须配置私钥或外部私钥引用")
        if self.profile_type == "oracle_wallet" and not self.wallet_directory:
            raise ValueError("Oracle Wallet 必须配置服务器绝对目录")
        return self


class CertificateProfileUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=120)
    ca_certificate_pem: str | None = Field(None, max_length=262_144)
    client_certificate_pem: str | None = Field(None, max_length=262_144)
    private_key: str | None = Field(None, max_length=262_144)
    private_key_secret_ref: str | None = Field(None, max_length=500)
    wallet_directory: str | None = Field(None, max_length=500)
    wallet_password: str | None = Field(None, max_length=65_536)
    wallet_password_secret_ref: str | None = Field(None, max_length=500)
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def validate_secret_sources(self):
        if self.private_key and self.private_key_secret_ref:
            raise ValueError("客户端私钥与外部私钥引用不能同时提交")
        if self.wallet_password and self.wallet_password_secret_ref:
            raise ValueError("Wallet 密码与外部秘密引用不能同时提交")
        return self


class CertificateProfileResponse(BaseModel):
    id: int
    name: str
    profile_type: str
    fingerprint_sha256: str | None = None
    subject: str | None = None
    issuer: str | None = None
    serial_number: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None
    enabled: int
    remark: str | None = None
    has_ca_certificate: bool = False
    has_client_certificate: bool = False
    has_private_key: bool = False
    private_key_source: str = "none"
    has_wallet_password: bool = False
    wallet_password_source: str = "none"
    lifecycle_status: str = "unknown"
    create_time: datetime | None = None
    update_time: datetime | None = None


class CertificateProfileAuditResponse(BaseModel):
    id: int
    profile_id: int
    action: str
    actor: str
    outcome: str
    detail: str | None = None
    create_time: datetime | None = None

    model_config = {"from_attributes": True}
