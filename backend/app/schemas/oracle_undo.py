from pydantic import BaseModel, Field


class UndoRetentionPlanRequest(BaseModel):
    retention_seconds: int = Field(..., ge=60, le=31536000, description="UNDO 保留时间，单位秒")
    scope: str = Field("BOTH", pattern="^(MEMORY|SPFILE|BOTH)$", description="ALTER SYSTEM SCOPE")
    rac_all_instances: bool = Field(True, description="RAC 环境是否生成 SID='*'")
