from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.core.engines import DATABASE_NAME_ENGINES, DB_TYPE_PATTERN


class DBInstanceBase(BaseModel):
    instance_name: str = Field(..., max_length=100)
    # 白名单来自 app.core.engines.DB_TYPES 单一来源，避免与连接分发/能力目录各写一份。
    db_type: str = Field(..., pattern=DB_TYPE_PATTERN)
    db_version: str | None = Field(None, max_length=100)
    patch_info: str | None = None
    host: str = Field(..., max_length=100)
    port: int = Field(..., ge=1, le=65535)
    oracle_connect_type: str | None = Field(None, pattern="^(service_name|sid)$")
    service_name: str | None = Field(None, max_length=100)
    sid: str | None = Field(None, max_length=100)
    database_name: str | None = Field(None, max_length=100)
    schema_name: str | None = Field(None, max_length=100)
    sslmode: str | None = Field(None, max_length=30)
    encrypt: str | None = Field(None, max_length=20)
    trust_server_certificate: str | None = Field(None, max_length=20)
    driver_name: str | None = Field(None, max_length=100)
    certificate_profile_id: int | None = Field(None, ge=1)
    tls_server_name: str | None = Field(None, max_length=255)
    tls_verify: int = Field(1, ge=0, le=1)
    account_type: str = Field("system", pattern="^(system|sysdba|business)$")
    username: str = Field(..., max_length=100)
    env_type: str | None = Field(None, max_length=30)
    architecture_type: str | None = Field(None, max_length=50)
    role_type: str | None = Field(None, max_length=50)
    system_name: str | None = Field(None, max_length=100)
    factory_name: str | None = Field(None, max_length=100)
    line_name: str | None = Field(None, max_length=100)
    tenant_id: int | None = Field(None, ge=1)
    organization_id: int | None = Field(None, ge=1)
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)
    ssh_host: str | None = Field(None, max_length=100)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    ssh_username: str | None = Field(None, max_length=100)
    ssh_password: str | None = None

    @model_validator(mode="after")
    def validate_engine_fields(self):
        if self.db_type == "oracle":
            if self.oracle_connect_type == "sid" and not self.sid and not self.service_name:
                raise ValueError("Oracle SID is required when oracle_connect_type=sid")
            if self.oracle_connect_type == "sid" and not self.sid and self.service_name:
                self.sid = self.service_name
            if self.oracle_connect_type != "sid" and not self.service_name and not self.sid:
                raise ValueError("Oracle service_name is required")
            if self.oracle_connect_type != "sid" and not self.service_name and self.sid:
                self.service_name = self.sid
        if self.db_type in DATABASE_NAME_ENGINES and not self.database_name:
            self.database_name = None
        if self.db_type != "oracle" and self.account_type == "sysdba":
            self.account_type = "system"
        return self


class DBInstanceCreate(DBInstanceBase):
    password: str | None = Field(None, min_length=1)
    password_secret_ref: str | None = Field(None, min_length=1, max_length=500)
    ssh_password_secret_ref: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_secret_sources(self):
        if bool(self.password) == bool(self.password_secret_ref):
            raise ValueError("必须且只能提供数据库密码或外部秘密引用")
        if self.ssh_password and self.ssh_password_secret_ref:
            raise ValueError("SSH 密码与外部秘密引用不能同时提交")
        return self


class DBInstanceConnectionTest(DBInstanceBase):
    instance_id: int | None = Field(None, ge=1)
    password: str | None = Field(None, min_length=0)
    password_secret_ref: str | None = Field(None, min_length=1, max_length=500)
    ssh_password_secret_ref: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_secret_sources(self):
        if self.password and self.password_secret_ref:
            raise ValueError("数据库密码与外部秘密引用不能同时提交")
        if self.ssh_password and self.ssh_password_secret_ref:
            raise ValueError("SSH 密码与外部秘密引用不能同时提交")
        return self


class DBInstanceUpdate(BaseModel):
    instance_name: str | None = Field(None, max_length=100)
    db_version: str | None = Field(None, max_length=100)
    patch_info: str | None = None
    host: str | None = Field(None, max_length=100)
    port: int | None = Field(None, ge=1, le=65535)
    oracle_connect_type: str | None = Field(None, pattern="^(service_name|sid)$")
    service_name: str | None = Field(None, max_length=100)
    sid: str | None = Field(None, max_length=100)
    database_name: str | None = Field(None, max_length=100)
    schema_name: str | None = Field(None, max_length=100)
    sslmode: str | None = Field(None, max_length=30)
    encrypt: str | None = Field(None, max_length=20)
    trust_server_certificate: str | None = Field(None, max_length=20)
    driver_name: str | None = Field(None, max_length=100)
    certificate_profile_id: int | None = Field(None, ge=1)
    tls_server_name: str | None = Field(None, max_length=255)
    tls_verify: int | None = Field(None, ge=0, le=1)
    account_type: str | None = Field(None, pattern="^(system|sysdba|business)$")
    username: str | None = Field(None, max_length=100)
    password: str | None = Field(None, min_length=0)
    password_secret_ref: str | None = Field(None, min_length=1, max_length=500)
    env_type: str | None = Field(None, max_length=30)
    architecture_type: str | None = Field(None, max_length=50)
    role_type: str | None = Field(None, max_length=50)
    system_name: str | None = Field(None, max_length=100)
    factory_name: str | None = Field(None, max_length=100)
    line_name: str | None = Field(None, max_length=100)
    tenant_id: int | None = Field(None, ge=1)
    organization_id: int | None = Field(None, ge=1)
    enabled: int | None = Field(None, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)
    ssh_host: str | None = Field(None, max_length=100)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    ssh_username: str | None = Field(None, max_length=100)
    ssh_password: str | None = None
    ssh_password_secret_ref: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_secret_sources(self):
        if self.password and self.password_secret_ref:
            raise ValueError("数据库密码与外部秘密引用不能同时提交")
        if self.ssh_password and self.ssh_password_secret_ref:
            raise ValueError("SSH 密码与外部秘密引用不能同时提交")
        return self


class DBInstanceResponse(DBInstanceBase):
    id: int
    ssh_password: str | None = Field(None, exclude=True)
    has_password: bool = False
    credential_source: str = "none"
    credential_requires_rotation: bool = False
    password_rotated_at: datetime | None = None
    has_ssh_password: bool = False
    ssh_credential_source: str = "none"
    ssh_credential_requires_rotation: bool = False
    ssh_password_rotated_at: datetime | None = None
    create_time: datetime | None
    update_time: datetime | None

    model_config = {"from_attributes": True}


class DBInstanceCredentialRotate(BaseModel):
    credential_kind: str = Field("database", pattern="^(database|ssh)$")
    password: str | None = Field(None, min_length=1)
    secret_ref: str | None = Field(None, min_length=1, max_length=500)
    reason: str = Field(..., min_length=3, max_length=500)

    @model_validator(mode="after")
    def validate_secret_sources(self):
        if self.password and self.secret_ref:
            raise ValueError("密码与外部秘密引用不能同时提交")
        return self


class DBInstanceCredentialAuditResponse(BaseModel):
    id: int
    instance_id: int
    credential_kind: str
    action: str
    actor: str
    source_before: str | None = None
    source_after: str | None = None
    outcome: str
    reason: str | None = None
    create_time: datetime | None = None

    model_config = {"from_attributes": True}
