from pydantic import BaseModel, Field


class PartitionPlanRequest(BaseModel):
    owner: str
    table_name: str
    partition_column: str
    partition_type: str = Field("RANGE_INTERVAL", pattern="^(RANGE|RANGE_INTERVAL|LIST|HASH)$")
    interval_unit: str = Field("MONTH", pattern="^(DAY|MONTH|YEAR)$")
    interval_value: int | None = Field(None, ge=1, le=999)
    start_less_than: str = Field("2025-01-01")
    method: str = Field("DBMS_REDEFINITION", pattern="^(DBMS_REDEFINITION|CTAS|ALTER_ONLINE)$")
    parallel: int = Field(4, ge=1, le=64)
    tablespace_name: str | None = None
    index_tablespace_name: str | None = None
    interim_table_name: str | None = None
    new_table_name: str | None = None
    backup_suffix: str = "BAK"
    online: bool = True
