from pydantic import BaseModel, Field


class SystemAuditCleanupPlanRequest(BaseModel):
    retention_days: int = Field(30, ge=1, le=3650, description="审计记录保留天数")
    include_fga: bool = Field(True, description="是否同时生成 FGA_LOG$ 清理 SQL")
    include_location_plan: bool = Field(False, description="是否生成审计表迁移建议 SQL")
    audit_trail_location: str = Field("SYSAUX", description="审计表建议迁移目标表空间")


class SysauxAwrCleanupPlanRequest(BaseModel):
    retention_days: int = Field(30, ge=1, le=3650, description="AWR 快照保留天数")


class SysauxAwrRetentionPlanRequest(BaseModel):
    retention_days: int = Field(30, ge=1, le=3650, description="AWR 保留天数")
    interval_minutes: int = Field(60, ge=10, le=1440, description="AWR 采集间隔，单位分钟")
