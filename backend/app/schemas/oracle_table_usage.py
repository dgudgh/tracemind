from typing import List, Optional

from pydantic import BaseModel, Field


class IndexObjectPlanRequest(BaseModel):
    owner: str
    index_name: str


class TableUsageIndexAdviceRequest(BaseModel):
    owner: str
    table_name: str
    sql_id: Optional[str] = None
    days: int = Field(30, ge=1, le=365)
    source: str = Field("AWR", pattern="^(AWR|CURRENT)$")


class TableUsageCreateIndexPlanRequest(BaseModel):
    owner: str
    table_name: str
    index_name: str
    columns: List[str]
    index_type: str = Field("NORMAL", pattern="^(NORMAL|UNIQUE|BITMAP)$")
    tablespace_name: Optional[str] = None
    online: bool = True
    parallel: int = Field(1, ge=1, le=32)
    compress: bool = False
