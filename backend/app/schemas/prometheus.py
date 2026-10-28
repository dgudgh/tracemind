from pydantic import BaseModel, Field


class PrometheusConfigUpsert(BaseModel):
    config_name: str = Field("默认 Prometheus", min_length=1, max_length=100)
    prometheus_url: str | None = Field(None, max_length=500)
    alertmanager_url: str | None = Field(None, max_length=500)
    enabled: int = Field(1, ge=0, le=1)
    remark: str | None = Field(None, max_length=500)
