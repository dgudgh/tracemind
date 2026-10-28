"""版本能力边界：开源版只开放基础模块。

设计原则
--------
1. **本仓库只有开源版**。`TRACEMIND_EDITION` 未设置时按 `oss` 处理；企业版是独立产品线，
   其实现不在本仓库内。保留该开关只为兼容从企业版迁移过来的部署配置。
2. **两层一致**。菜单（module_key）与接口（路径规则）必须覆盖同一批模块，
   否则会出现"菜单藏了但接口能调"的假象。
3. **独立于权限**。这是产品版本能力，不是账号权限，因此对 admin 同样生效；
   权限层（request_scope/action policy）继续按角色判断，两者互不替代。
4. **显式付费清单**。用"付费项"而非"免费白名单"：
   漏配的后果是"该隐藏的还看得见"（可补），而不是"基础功能被误锁"（不可用）。
5. **路径规则要能表达"前缀下的子段"**。性能分析与运维共用同一批前缀
   （`/oracle/{id}/awr` 与 `/oracle/{id}/tablespaces`、`/performance/baseline` 与
   `/performance/overview`），只能按段切，不能按一级前缀一刀切。

规则语言
--------
`PAID_ROUTE_RULES` 中每条规则是**路径段元组**，从 `/api/v1/` 之后开始逐段比对：

- 字面段（如 ``"performance"``）：必须相等。
- ``"{}"``：任意**纯数字**段（实例 id 的位置）。
- ``"*"``：任意单段（引擎名等变量）。

命中任一规则即拒绝。规则按"具体度"（段数）降序匹配，便于在 `denial()` 里给出准确原因。

付费范围（企业版，43 个模块）
--------------------------
- 性能分析 10 项：总览 / 引擎深度报告 / SQL 画像 / 索引分析 / 表画像 / 问题趋势 /
  优化建议 / AWR 历史 / AWR 对比 / AWR 自动采集
- 数据保护 14 项：backup.* / rescue.* / protection.*
- 智能诊断 4 项：状态与现场 / 事故中心 / 现场 Agent / 快照策略
- 巡检高级 3 项：历史对比 / 巡检策略 / 巡检模板
- 集成中心 4 项：Zabbix / Prometheus / 通知渠道 / 工单
- 可观测 3 项：日志关联 / Grafana 模板 / APM 链路
- 系统管理 2 项：企业认证 / 诊断评测
- 数据库运维 3 项：高可用 / 数据治理 / 归档对象存储

基础范围（开源版，31 个模块）
--------------------------
资产、工作台、诊断工作台、告警（综合/容量/可观测总览）、巡检 4 项、
性能分析 5 项（慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合）、
运维 8 项（能力矩阵/配置基线/运行概览/空间管理/日志管理/会话事务/对象统计/用户安全）、
报告 2 项、系统管理 5 项（用户管理/系统参数/AI 配置/调度任务/权限配置）、引擎与版本。
"""

from __future__ import annotations

import os

__all__ = [
    "EDITION",
    "BASIC_ONLY",
    "PAID_MODULE_PREFIXES",
    "PAID_ROUTE_RULES",
    "OSS_OPS_ENGINES",
    "module_allowed",
    "path_allowed",
    "denial",
]

ENTERPRISE = "enterprise"
OSS = "oss"

EDITION = (os.getenv("TRACEMIND_EDITION") or OSS).strip().lower()
BASIC_ONLY = EDITION == OSS

#: 企业版模块 key 前缀（与 api/v1/module_config.DEFAULT_MODULES 的 module_key 对应）。
#: 逐个列出而不是用 `perf.` 整域，因为性能分析里 5 项属开源版。
PAID_MODULE_PREFIXES: tuple[str, ...] = (
    # 性能分析：10 项（慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合 不在其中）
    "perf.overview",
    "perf.workload",
    "perf.sql",
    "perf.index",
    "perf.table",
    "perf.trend",
    "perf.optimization",
    "perf.awr.history",
    "perf.awr.compare",
    "perf.awr.collect",
    # 数据保护
    "backup.",
    "rescue.",
    "protection.",
    # 智能诊断：除诊断工作台（diagnose.smart）
    "diagnose.investigation",
    "diagnose.incidents",
    "diagnose.snapshot.",
    # 巡检：历史对比 / 策略 / 模板
    "inspect.compare",
    "inspect.policies",
    "inspect.templates",
    # 集成中心
    "integration.",
    # 可观测：三个子项（总览仍开源）
    "monitor.observability.logs",
    "monitor.observability.grafana",
    "monitor.observability.apm",
    # 系统管理
    "system.identity",
    "system.aiEvaluation",
    # 数据库运维：高可用 / 数据治理（含归档对象存储）
    "ops.ha",
    "ops.governance",
)

#: 企业版接口规则。每条是路径段元组，`{}` 匹配纯数字段，`*` 匹配任意单段。
PAID_ROUTE_RULES: tuple[tuple[str, ...], ...] = (
    # —— 整段拦截（该前缀下全部接口属企业版）——
    ("sql-profiles",),
    ("backup-center",),
    ("recovery",),
    ("automation",),
    ("incidents",),
    ("scene-captures",),
    ("incident-agents",),
    ("incident-snapshots",),
    ("incident-trigger-rules",),
    ("incident-audit",),
    ("incident-snapshot-health",),
    ("zabbix",),
    ("zabbix-push",),
    ("prometheus",),
    ("notify",),
    ("itsm",),
    ("identity",),
    ("diagnosis-evaluation",),
    ("data-governance",),
    # —— 性能分析：只有 baseline 开源，其余按二级段拦 ——
    ("performance", "overview"),
    ("performance", "workload-reports"),
    ("performance", "profile"),
    ("performance", "sql-profile"),
    ("performance", "index-findings"),
    ("performance", "table-profile"),
    ("performance", "problem-trend"),
    ("performance", "suggestions"),
    # —— 可观测：总览开源，三个子项企业 ——
    ("observability", "logs"),
    ("observability", "grafana"),
    ("observability", "apm"),
    # —— 巡检：策略 / 模板 / 自定义指标 ——
    ("inspection", "policies"),
    ("inspection", "policy-templates"),
    ("inspection", "custom-metrics"),
    # —— Oracle 引擎前缀下的企业段（statistics/undo/temp 等运维段不列，保持开源）——
    ("oracle", "{}", "indexes"),
    ("oracle", "{}", "unused-indexes"),
    ("oracle", "{}", "index-fragmentation"),
    ("oracle", "{}", "table-fragmentation"),
    ("oracle", "{}", "index-rebuild-script"),
    ("oracle", "{}", "drop-index-script"),
    ("oracle", "{}", "batch-create-index"),
    ("oracle", "{}", "table-usage"),
    ("oracle", "{}", "similar-tables"),
    ("oracle", "{}", "dataguard"),
    ("oracle", "{}", "dg-status"),
    # AWR：只拦"对比"与"自动采集"；导出/上传/报告列表归「AWR 综合」（开源）
    ("oracle", "{}", "awr", "compare"),
    ("oracle", "{}", "awr", "collect-now"),
    ("oracle", "{}", "awr", "collect-policy"),
    ("oracle", "awr", "collect-policies"),
    # —— MySQL 系列（slow-sql / top-sql / capacity 属开源，不列）——
    ("mysql-ops", "{}", "mgr"),
    ("mysql-ops", "{}", "replication"),
    ("mysql-ops", "{}", "indexes"),
    ("mysql-ops", "{}", "tables-indexes"),
    ("mysql", "{}", "replication"),
    # —— operations 下的高可用（PostgreSQL / SQL Server 走 section）——
    ("operations", "*", "{}", "ha"),
    # —— 扩展引擎运维（引擎接入开源，但其运维能力属企业版）——
    ("operations", "dm"),
    ("operations", "kingbasees"),
)

#: 开源版允许运维的数据库引擎。其余引擎的运维能力需要企业版。
#: 与前端 menuConfig 里 `engines: [...]` 的声明应为同一组值。
OSS_OPS_ENGINES: tuple[str, ...] = (
    "oracle",
    "mysql",
    "postgresql",
    "sqlserver",
)

#: 扩展引擎名（用于把"未授权"与"未实现"两种语义分开）。
_EXTENSION_ENGINES = frozenset({"dm", "kingbasees"})

#: 命中这些一级前缀的规则，拒绝原因归为"性能能力"而非"模块"。
_CAPABILITY_PREFIXES = frozenset({
    "performance", "observability", "oracle", "mysql", "mysql-ops", "postgresql", "sqlserver",
})

_API_PREFIX = "/api/v1/"


def module_allowed(module_key: str | None) -> bool:
    """开源版下，非基础模块一律不可见。"""
    if not BASIC_ONLY or not isinstance(module_key, str):
        return True
    return not any(module_key == p.rstrip(".") or module_key.startswith(p)
                   for p in PAID_MODULE_PREFIXES)


def _segments(path: str) -> list[str]:
    """拆出 `/api/v1/` 之后的路径段（忽略查询串与空段）。"""
    if not path.startswith(_API_PREFIX):
        return []
    rest = path[len(_API_PREFIX):].split("?", 1)[0]
    return [s for s in rest.split("/") if s]


def _matches(segments: list[str], rule: tuple[str, ...]) -> bool:
    """路径段是否命中一条规则（规则只需是路径的前缀）。"""
    if len(segments) < len(rule):
        return False
    for index, expected in enumerate(rule):
        actual = segments[index]
        if expected == "{}":
            if not actual.isdigit():
                return False
        elif expected == "*":
            continue
        elif expected != actual:
            return False
    return True


def _matched_rule(segments: list[str]) -> tuple[str, ...] | None:
    """返回命中的规则（具体度最高者）。"""
    best: tuple[str, ...] | None = None
    for rule in PAID_ROUTE_RULES:
        if _matches(segments, rule):
            if best is None or len(rule) > len(best):
                best = rule
    return best


def path_allowed(path: str) -> bool:
    """开源版下，企业版接口一律拒绝。

    非 `/api/v1/` 开头的路径（静态资源、健康检查、login 等）由认证层各自处理，这里不拦。
    """
    if not BASIC_ONLY:
        return True
    segments = _segments(path)
    if not segments:
        return True
    return _matched_rule(segments) is None


def denial(path: str) -> dict:
    """统一拒绝体：说明是版本能力限制，而不是权限不足或服务异常。"""
    segments = _segments(path)
    rule = _matched_rule(segments) or ()

    # 扩展引擎的运维：语义是"该引擎需企业版"，与"没实现"区分开。
    if (len(rule) >= 2 and rule[0] == "operations"
            and rule[1] in _EXTENSION_ENGINES):
        return {
            "code": "edition_engine_unavailable",
            "message": "此功能暂不支持",
            "edition": EDITION,
            "engine": rule[1],
            "path": path,
        }
    # 引擎前缀或性能前缀下的功能段：语义是"该项性能能力需企业版"。
    if rule and rule[0] in _CAPABILITY_PREFIXES:
        return {
            "code": "edition_capability_unavailable",
            "message": "此功能暂不支持",
            "edition": EDITION,
            "capability": rule[-1],
            "path": path,
        }
    return {
        "code": "edition_module_unavailable",
        "message": "此功能暂不支持",
        "edition": EDITION,
        "path": path,
    }
