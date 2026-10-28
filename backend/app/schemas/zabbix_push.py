from pydantic import BaseModel, Field


class ZabbixPushConfigCreate(BaseModel):
    config_name: str = Field(..., min_length=1, max_length=100)
    zabbix_server: str = Field(..., min_length=1, max_length=200)
    zabbix_port: int = Field(10051, ge=1, le=65535)
    zabbix_host: str = Field(..., min_length=1, max_length=200)
    enabled: int = Field(1, ge=0, le=1)
    timeout_seconds: int = Field(10, ge=1, le=60)
    remark: str | None = Field(None, max_length=500)


class ZabbixPushConfigUpdate(BaseModel):
    config_name: str | None = Field(None, min_length=1, max_length=100)
    zabbix_server: str | None = Field(None, min_length=1, max_length=200)
    zabbix_port: int | None = Field(None, ge=1, le=65535)
    zabbix_host: str | None = Field(None, min_length=1, max_length=200)
    enabled: int | None = Field(None, ge=0, le=1)
    timeout_seconds: int | None = Field(None, ge=1, le=60)
    remark: str | None = Field(None, max_length=500)


class ZabbixAlertPushRequest(BaseModel):
    alert_id: str = Field(..., min_length=1, max_length=200)
    status: int = Field(1, ge=0, le=1)
    level: int = Field(2, ge=0, le=5)
    message: str = Field(..., min_length=1, max_length=500)
    detail: dict | None = None


class ZabbixAlertRecoverRequest(BaseModel):
    message: str | None = Field(None, max_length=500)
