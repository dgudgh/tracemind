from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ArchiveDbPlanRequest(BaseModel):
    source_instance_id: Optional[int] = Field(None, description="生产库实例 ID")
    target_instance_id: Optional[int] = Field(None, description="历史库实例 ID")

    source_owner: str = Field(..., description="生产库 Schema")
    source_table: str = Field(..., description="生产库表名")
    target_owner: str = Field("HIS_DATA", description="历史库 Schema")
    target_table: Optional[str] = Field(None, description="历史库表名")

    archive_column: str = Field(..., description="归档字段")
    retention_months: int = Field(6, ge=1, le=120, description="生产库保留月份")

    method: Literal["DB_LINK", "DATAPUMP_NETWORK_LINK"] = "DB_LINK"
    db_link_name: str = Field("PROD_MES_LINK", description="DB Link 名称")

    batch_size: int = Field(10000, ge=1000, le=100000)
    parallel_degree: int = Field(4, ge=1, le=16)

    history_partitioned: bool = True
    partition_interval: Literal["MONTH", "DAY", "YEAR"] = "MONTH"
    history_tablespace: str = Field("HIS_DATA_TS", description="历史表表空间")
    history_idx_tablespace: str = Field("HIS_IDX_TS", description="历史索引表空间")

    generate_delete_script: bool = True
    generate_verify_script: bool = True
    generate_stats_script: bool = True

    start_date: Optional[str] = Field(None, description="开始日期 YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="结束日期 YYYY-MM-DD")

    @model_validator(mode="before")
    @classmethod
    def apply_defaults_for_blank_values(cls, data):
        if not isinstance(data, dict):
            return data

        defaults = {
            "target_owner": "HIS_DATA",
            "retention_months": 6,
            "method": "DB_LINK",
            "db_link_name": "PROD_MES_LINK",
            "batch_size": 10000,
            "parallel_degree": 4,
            "history_partitioned": True,
            "partition_interval": "MONTH",
            "history_tablespace": "HIS_DATA_TS",
            "history_idx_tablespace": "HIS_IDX_TS",
            "generate_delete_script": True,
            "generate_verify_script": True,
            "generate_stats_script": True,
        }
        cleaned = dict(data)
        for key, value in list(cleaned.items()):
            if value == "":
                cleaned[key] = None
        for key, default in defaults.items():
            if cleaned.get(key) is None:
                cleaned[key] = default
        return cleaned
