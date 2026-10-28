from typing import List, Optional

from pydantic import BaseModel, Field


class RebuildIndexPlanRequest(BaseModel):
    owner: str = Field(..., description="索引所属 Schema")
    index_name: str = Field(..., description="索引名")
    partition_name: Optional[str] = Field(None, description="分区名（分区索引重建时需要）")
    subpartition_name: Optional[str] = Field(None, description="子分区名（子分区索引重建时需要）")
    tablespace_name: Optional[str] = Field(None, description="重建目标表空间")
    online: bool = Field(True, description="是否 ONLINE 重建")
    parallel: int = Field(1, ge=1, le=16, description="并行度")


class CreateIndexPlanRequest(BaseModel):
    owner: str = Field(..., description="表所属 Schema")
    table_name: str = Field(..., description="表名")
    index_name: str = Field(..., description="索引名")
    columns: List[str] = Field(..., min_length=1, description="索引列列表")
    index_type: str = Field("NORMAL", description="NORMAL / UNIQUE / BITMAP")
    tablespace_name: Optional[str] = Field(None, description="索引表空间")
    online: bool = Field(True, description="是否 ONLINE 创建")
    parallel: int = Field(1, ge=1, le=16, description="并行度")
    compress: bool = Field(False, description="是否启用压缩")


class CheckDuplicateRequest(BaseModel):
    owner: str = Field(..., description="表所属 Schema")
    table_name: str = Field(..., description="表名")
    columns: List[str] = Field(..., min_length=1, description="待检查的索引列")
