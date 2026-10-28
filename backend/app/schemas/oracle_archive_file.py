from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ArchiveFileBaseRequest(BaseModel):
    owner: str = Field(..., description="源 Schema")
    table_name: str = Field(..., description="源表名")
    archive_column: str = Field(..., description="归档时间字段")
    start_date: Optional[str] = Field(None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="结束日期 YYYY-MM-DD")
    retention_months: Optional[int] = Field(None, ge=1, le=120, description="保留月份，未填起止日期时按该值生成条件")
    directory_name: str = Field("ARCHIVE_DIR", description="Oracle Directory 名称")
    directory_path: str = Field("/backup/archive", description="OS 文件目录")
    file_prefix: Optional[str] = Field(None, description="文件名前缀，默认 OWNER_TABLE")
    split_by_month: bool = Field(False, description="起止日期跨月时按月拆分脚本")

    @model_validator(mode="before")
    @classmethod
    def normalize_blank_values(cls, data):
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key, value in list(cleaned.items()):
            if value == "":
                cleaned[key] = None
        return cleaned


class CsvPlanRequest(ArchiveFileBaseRequest):
    csv_method: Literal["SQLCL", "SQLPLUS", "PYTHON"] = Field("SQLCL", description="CSV 导出方式")
    encoding: str = Field("UTF-8-SIG", description="CSV 编码")
    separator: str = Field(",", description="字段分隔符")
    batch_size: int = Field(10000, ge=100, le=100000, description="Python 方式每批行数")
    compress: bool = Field(False, description="是否生成压缩命令")


class ExpdpPlanRequest(ArchiveFileBaseRequest):
    parallel: int = Field(4, ge=1, le=32, description="EXPDP 并行度")
    parallel_degree: Optional[int] = Field(None, ge=1, le=32, description="兼容字段：EXPDP 并行度")
    compression: Literal["ALL", "DATA_ONLY", "NONE"] = Field("ALL", description="EXPDP 压缩")
    generate_impdp: bool = Field(True, description="是否生成 IMPDP 恢复命令")
    remap_schema: Optional[str] = Field(None, description="IMPDP remap_schema 目标 Schema")
    remap_table: Optional[str] = Field(None, description="IMPDP remap_table 目标表")

    @model_validator(mode="before")
    @classmethod
    def normalize_parallel_degree(cls, data):
        data = super().normalize_blank_values(data)
        if isinstance(data, dict) and data.get("parallel_degree") and not data.get("parallel"):
            data["parallel"] = data["parallel_degree"]
        return data


class DirectoryPlanRequest(BaseModel):
    directory_name: str = Field("ARCHIVE_DIR", description="Directory 名称")
    directory_path: str = Field("/backup/archive", description="OS 路径")
    owner: Optional[str] = Field(None, description="默认授权用户")
    grant_to: Optional[str] = Field(None, description="授权用户")

    @model_validator(mode="before")
    @classmethod
    def normalize_blank_values(cls, data):
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key, value in list(cleaned.items()):
            if value == "":
                cleaned[key] = None
        return cleaned


class VerifyPlanRequest(ArchiveFileBaseRequest):
    primary_key_columns: Optional[str] = Field(None, description="主键/唯一键列，逗号分隔，用于生成去重校验 SQL")


class FullArchiveFilePlanRequest(CsvPlanRequest, ExpdpPlanRequest):
    file_type: Literal["CSV", "EXPDP", "BOTH"] = Field("EXPDP", description="文件类型")
    generate_directory: bool = Field(True, description="是否生成 Directory 检查/创建脚本")
    generate_verify: bool = Field(True, description="是否生成校验 SQL")
    generate_cleanup: bool = Field(False, description="是否生成生产库清理 SQL")
    cleanup_batch_size: int = Field(10000, ge=100, le=100000, description="生产清理批大小")
    primary_key_columns: Optional[str] = Field(None, description="主键/唯一键列，逗号分隔，用于生成去重校验 SQL")
