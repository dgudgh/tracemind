"""引擎身份的唯一来源。

此前 `db_type` 白名单以正则字面量分别写在实例 Schema、连接分发、能力目录和前端表单里，
新增一个引擎必须同时改多处；漏一处就会出现「实例建得出来但连不上」或
「连得上但能力目录里查不到」这类半吊子接线。

达梦（dm）与人大金仓（kingbasees）是**扩展引擎**，不是原生四引擎：
它们与原生引擎共用实例/凭据/审批链路，但能力成熟度必须单独声明，
不得继承原生引擎的 ready 状态（EP-R03：不借协议兼容冒充 Oracle/PostgreSQL）。
"""

from __future__ import annotations

NATIVE_ENGINES = ("oracle", "mysql", "postgresql", "sqlserver")
EXTENSION_ENGINES = ("dm", "kingbasees")
DB_TYPES = NATIVE_ENGINES + EXTENSION_ENGINES

# 供 pydantic Field(pattern=...) 使用；与 DB_TYPES 必须一致，由测试保证。
DB_TYPE_PATTERN = "^(?:" + "|".join(DB_TYPES) + ")$"

ENGINE_LABELS = {
    "oracle": "Oracle",
    "mysql": "MySQL",
    "postgresql": "PostgreSQL",
    "sqlserver": "SQL Server",
    "dm": "达梦 DM",
    "kingbasees": "人大金仓 KingbaseES",
}

# 允许显式声明 database_name 的引擎；Oracle 用 service_name/sid，不在此列。
DATABASE_NAME_ENGINES = frozenset({"mysql", "postgresql", "sqlserver", "dm", "kingbasees"})

# 能力成熟度声明（按引擎，不按引擎族）。
# requires_runtime_validation 是**如实**状态：源码就绪但没有真机验收证据，
# 不得改写成 ready。翻成 ready 的唯一依据是真机取证记录。
ENGINE_MATURITY = {
    engine: ("requires_runtime_validation" if engine in EXTENSION_ENGINES else "ready")
    for engine in DB_TYPES
}


def is_known(engine: str | None) -> bool:
    return isinstance(engine, str) and engine in DB_TYPES


def is_extension(engine: str | None) -> bool:
    return isinstance(engine, str) and engine in EXTENSION_ENGINES
