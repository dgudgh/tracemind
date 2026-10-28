"""诊断中心共享常量：七层模型、层级映射、优先级模板。"""
from __future__ import annotations

# 七层诊断模型
LAYER_BUSINESS = "业务层"
LAYER_APP = "应用层"
LAYER_NETWORK = "网络层"
LAYER_OS = "操作系统层"
LAYER_STORAGE = "存储层"
LAYER_DB = "数据库层"
LAYER_SQL = "SQL层"

LAYERS = [
    LAYER_BUSINESS,
    LAYER_APP,
    LAYER_NETWORK,
    LAYER_OS,
    LAYER_STORAGE,
    LAYER_DB,
    LAYER_SQL,
]

# 规则诊断 category → 七层
RULE_CATEGORY_TO_LAYER = {
    "space": LAYER_STORAGE,
    "archive": LAYER_STORAGE,
    "performance": LAYER_SQL,
    "sql_index": LAYER_SQL,
    "transaction": LAYER_DB,
    "ha": LAYER_DB,
    "external_monitor": LAYER_OS,
    "overview": LAYER_DB,
}

# 默认根因优先级（高 → 低），用于权重调节
DEFAULT_PRIORITY = [LAYER_NETWORK, LAYER_STORAGE, LAYER_OS, LAYER_DB, LAYER_SQL, LAYER_APP, LAYER_BUSINESS]

# RAC 场景优先级：网络 > Cache Fusion(数据库层 GC) > 存储 > 内存(OS) > CPU(OS)
RAC_PRIORITY = [LAYER_NETWORK, LAYER_DB, LAYER_STORAGE, LAYER_OS, LAYER_SQL, LAYER_APP, LAYER_BUSINESS]

# AWR 重点等待事件 → 层 + 含义
WAIT_EVENT_LAYER = {
    "log file sync": (LAYER_STORAGE, "Redo 写入延迟，commit 等待 LGWR 落盘"),
    "db file sequential read": (LAYER_STORAGE, "单块读延迟，多为索引读 + 存储/IO 慢"),
    "db file scattered read": (LAYER_SQL, "多块读，常见于全表扫描"),
    "direct path read temp": (LAYER_SQL, "TEMP 表空间排序/哈希溢出"),
    "direct path write temp": (LAYER_SQL, "TEMP 写溢出"),
    "gc cr request": (LAYER_NETWORK, "RAC Cache Fusion 跨节点读，私网/互联延迟"),
    "gc buffer busy": (LAYER_DB, "RAC 全局缓存热块争用"),
    "library cache lock": (LAYER_DB, "共享池对象锁，常因 DDL/解析风暴"),
    "cursor: pin s wait on x": (LAYER_DB, "游标争用，硬解析或绑定变量风暴"),
    "enq: tx - row lock contention": (LAYER_DB, "行锁争用"),
    "read by other session": (LAYER_STORAGE, "热块读争用"),
}


def severity_score(severity: str | None) -> float:
    """严重度 → 数值权重。"""
    v = str(severity or "").lower()
    if v in ("critical", "high", "严重", "高"):
        return 1.0
    if v in ("warning", "warn", "medium", "中"):
        return 0.6
    if v in ("info", "low", "提示", "低", "normal"):
        return 0.2
    return 0.4


def priority_weight(layer: str | None, priority: list[str]) -> float:
    """层在优先级列表中的位置 → 权重(0.4~1.0)，越靠前越高。"""
    if not layer or layer not in priority:
        return 0.5
    idx = priority.index(layer)
    n = max(1, len(priority) - 1)
    return round(1.0 - 0.6 * (idx / n), 3)
