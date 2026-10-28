from decimal import Decimal

from pydantic import BaseModel, Field


class CapacityPolicyUpdate(BaseModel):
    enabled: int = Field(1, ge=0, le=1)
    frequency_minutes: int = Field(1440, ge=5, le=43200)
    forecast_days_threshold: int = Field(15, ge=1, le=365)
    abnormal_growth_mb: Decimal = Field(Decimal("10240"), ge=0)
    abnormal_growth_pct: Decimal = Field(Decimal("30"), ge=0)
    retention_days: int = Field(180, ge=7, le=3650)
    #: Breach must persist this long before it is published at all (0 = publish immediately).
    sustained_minutes: int = Field(0, ge=0, le=1440)
    #: Hysteresis: how far back inside the threshold a metric must recover before closing.
    recovery_margin_pct: Decimal = Field(Decimal("10"), ge=0, le=100)
