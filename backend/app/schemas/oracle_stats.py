from typing import Optional

from pydantic import BaseModel, Field


class GatherTableStatsRequest(BaseModel):
    owner: str = Field(..., description="Schema 名称")
    table_name: str = Field(..., description="表名")
    partition_name: Optional[str] = Field(None, description="分区名（分区表时指定）")
    estimate_percent: str = Field("AUTO", description="采样比例，默认 AUTO")
    method_opt: str = Field("FOR ALL COLUMNS SIZE AUTO", description="列统计方式")
    cascade: bool = Field(True, description="是否同时收集索引统计信息")
    degree: int = Field(4, ge=1, le=32, description="并行度")
    granularity: str = Field("AUTO", description="分区表粒度：AUTO/PARTITION/GLOBAL")
    no_invalidate: str = Field("AUTO", description="游标失效策略：AUTO/TRUE/FALSE")


class GatherSchemaStatsRequest(BaseModel):
    owner: str = Field(..., description="Schema 名称")
    options: str = Field("GATHER STALE", description="收集选项：GATHER/GATHER STALE/GATHER EMPTY")
    estimate_percent: str = Field("AUTO", description="采样比例")
    method_opt: str = Field("FOR ALL COLUMNS SIZE AUTO", description="列统计方式")
    cascade: bool = Field(True, description="是否同时收集索引统计信息")
    degree: int = Field(4, ge=1, le=32, description="并行度")
    no_invalidate: str = Field("AUTO", description="游标失效策略")


class TableStatsPlanRequest(BaseModel):
    owner: str = Field(..., description="Schema 名称")
    table_name: str = Field(..., description="表名")
