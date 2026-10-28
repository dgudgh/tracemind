from typing import Optional

from pydantic import BaseModel, Field


class FlashbackQueryRequest(BaseModel):
    schema_name: str = Field(..., description="Schema 名称")
    table_name: str = Field(..., description="表名")
    columns: str = Field("*", description="查询字段，默认 *")
    where_clause: Optional[str] = Field(None, description="WHERE 条件")
    flashback_type: str = Field("timestamp", description="timestamp / scn")
    flashback_time: Optional[str] = Field(None, description="闪回时间点，格式 YYYY-MM-DD HH24:MI:SS")
    scn: Optional[int] = Field(None, description="SCN")
    limit: int = Field(100, ge=1, le=1000, description="最大返回行数")


class RecyclebinRestorePlanRequest(BaseModel):
    owner: str = Field(..., description="对象所属 Schema")
    object_name: str = Field(..., description="回收站对象名（BIN$...）")
    original_name: Optional[str] = Field(None, description="原始表名")
    restore_name: Optional[str] = Field(None, description="恢复后重命名")


class TableRestorePlanRequest(BaseModel):
    owner: str = Field(..., description="表所属 Schema")
    table_name: str = Field(..., description="表名")
    restore_type: str = Field("timestamp", description="timestamp / scn")
    restore_time: Optional[str] = Field(None, description="恢复时间点，格式 YYYY-MM-DD HH24:MI:SS")
    scn: Optional[int] = Field(None, description="恢复 SCN")
    auxiliary_dest: Optional[str] = Field(None, description="RMAN 辅助目录")
    remap_name: Optional[str] = Field(None, description="恢复后重命名")


class RmanRestorePlanRequest(BaseModel):
    recovery_scope: str = Field("database", description="database / tablespace / datafile / controlfile")
    target_name: Optional[str] = Field(None, description="表空间名、数据文件号或控制文件说明")
    restore_type: str = Field("latest", description="latest / timestamp / scn")
    restore_time: Optional[str] = Field(None, description="恢复时间点，格式 YYYY-MM-DD HH24:MI:SS")
    scn: Optional[int] = Field(None, description="恢复 SCN")
    include_open_resetlogs: bool = Field(False, description="是否生成 OPEN RESETLOGS 步骤")
