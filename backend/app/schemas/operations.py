from pydantic import BaseModel, Field


class OracleOperationRequest(BaseModel):
    action: str = Field(..., pattern="^(trace|health|pfile|controlfile|tablespace|archive|sql)$")
    sql_text: str | None = None


class ArchiveCleanupPlanRequest(BaseModel):
    retention_days: int = Field(7, ge=1, le=30)
    cleanup_mode: str = Field("standard", pattern="^(standard|aggressive)$")
    force_cleanup: bool = False


class TablespaceExpansionPlanRequest(BaseModel):
    tablespace_name: str
    datafile_path: str | None = None
    initial_size_gb: float = Field(1.0, ge=0.1, le=100)
    max_size_gb: float | None = Field(30, ge=1, le=500)
    autoextend: str = Field("on", pattern="^(on|off)$")
