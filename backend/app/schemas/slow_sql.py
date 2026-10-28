from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class SlowSQLAIRequest(BaseModel):
    provider_config_id: int = Field(gt=0)
    provider_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_model_context: bool = False


class OracleSQLAIRequest(SlowSQLAIRequest):
    sql_id: str = Field(pattern=r"^[a-z0-9]{13}$")
    child_number: int = Field(ge=0)
    con_id: int = Field(ge=0)
    plan_hash_value: int = Field(ge=0)


class SlowSQLResponse(BaseModel):
    id: int
    instance_id: int
    db_type: str
    sql_id: str | None
    schema_name: str | None
    sql_text: str | None
    executions: int | None
    avg_elapsed_ms: Decimal | None
    max_elapsed_ms: Decimal | None
    total_elapsed_ms: Decimal | None
    logical_reads: int | None
    physical_reads: int | None
    rows_processed: int | None
    involved_tables: str | None
    mes_module: str | None
    risk_level: str | None
    ai_summary: str | None
    optimize_suggestion: str | None
    capture_time: datetime | None
    create_time: datetime | None

    model_config = {"from_attributes": True}
