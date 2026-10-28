"""版本能力边界：开源版 30 个模块 / 企业版 43 个模块。

覆盖七件事：
1. 默认（enterprise）完全不限制，现有部署行为不变；
2. 模块层：43 个企业模块全部不可见，30 个开源模块全部可见；
3. 接口层：企业路径被拦、开源路径放行（只拦一层等于没拦）；
4. 同前缀下的段级切分（性能分析与运维共用前缀，只能按段切）；
5. 引擎白名单与前端 `engines: [...]` 声明一致；
6. 规则表里出现的段必须真实存在于路由表，防止写成"看起来像护栏的常量"；
7. 模块清单与 `DEFAULT_MODULES` 完全对齐（不漏不重）。
"""
import re
from pathlib import Path

import pytest

from app.core import edition

_ROOT = Path(__file__).resolve().parents[2]
_MENU_CONFIG = _ROOT / "frontend" / "src" / "menuConfig.jsx"

#: 开源版可用的 30 个模块（能力矩阵已改为无页面，不在模块表内）
OSS_MODULES = (
    "asset.center", "home.dashboard", "diagnose.smart",
    "monitor.alert.unified", "monitor.alert.db", "monitor.observability",
    "inspect.overview", "inspect.run", "inspect.tasks", "inspect.reports",
    "perf.slowsql", "perf.lock", "perf.baseline", "perf.capacity", "perf.awr.report",
    "ops.configGovernance", "ops.overview", "ops.space",
    "ops.log", "ops.session", "ops.object", "ops.security",
    "report.unified", "report.alert",
    "system.user", "system.params", "system.ai", "system.schedule", "moduleConfig",
    "platform.engines",
)

#: 企业版独有的 43 个模块
EE_MODULES = (
    "perf.overview", "perf.workload", "perf.sql", "perf.index", "perf.table",
    "perf.trend", "perf.optimization",
    "perf.awr.history", "perf.awr.compare", "perf.awr.collect",
    "backup.overview", "backup.policy", "backup.plan", "backup.restore",
    "backup.report", "backup.platform",
    "rescue.overview", "rescue.cases", "rescue.tools", "rescue.agents",
    "rescue.tasks", "rescue.reports", "rescue.approvals",
    "protection.automation",
    "diagnose.investigation", "diagnose.incidents",
    "diagnose.snapshot.agents", "diagnose.snapshot.policies",
    "inspect.compare", "inspect.policies", "inspect.templates",
    "integration.zabbix", "integration.prometheus", "integration.notify", "integration.itsm",
    "monitor.observability.logs", "monitor.observability.grafana", "monitor.observability.apm",
    "system.identity", "system.aiEvaluation",
    "ops.ha", "ops.governance", "ops.governance.objectStorage",
)


@pytest.fixture
def oss(monkeypatch):
    monkeypatch.setattr(edition, "BASIC_ONLY", True)
    monkeypatch.setattr(edition, "EDITION", "oss")


def test_default_edition_does_not_restrict_anything():
    """未设置 TRACEMIND_EDITION 时必须放行，否则会拖垮既有部署。"""
    assert edition.BASIC_ONLY is False
    assert edition.path_allowed("/api/v1/performance/overview") is True
    assert edition.path_allowed("/api/v1/backup-center/catalog") is True
    assert edition.path_allowed("/api/v1/operations/dm/1/overview") is True
    assert edition.module_allowed("perf.sql") is True


def test_counts_are_30_and_43():
    assert len(OSS_MODULES) == 30
    assert len(EE_MODULES) == 43
    assert len(set(OSS_MODULES)) == 30
    assert len(set(EE_MODULES)) == 43
    assert not set(OSS_MODULES) & set(EE_MODULES)


def test_module_boundary_matches_default_modules_exactly(oss):
    """与企业版模块清单必须逐项对齐：既不漏、也不多。"""
    from app.api.v1.module_config import DEFAULT_MODULES

    all_keys = {m["module_key"] for m in DEFAULT_MODULES}
    assert len(all_keys) == 73
    assert all_keys == set(OSS_MODULES) | set(EE_MODULES)

    blocked = {k for k in all_keys if not edition.module_allowed(k)}
    assert blocked == set(EE_MODULES), f"不一致：{sorted(blocked ^ set(EE_MODULES))}"


def test_oss_keeps_all_30_module_keys(oss):
    for key in OSS_MODULES:
        assert edition.module_allowed(key) is True, key


def test_oss_blocks_all_43_module_keys(oss):
    for key in EE_MODULES:
        assert edition.module_allowed(key) is False, key


def test_oss_keeps_public_paths(oss):
    """开源功能对应的接口必须全部放行——任何一条被拦都会把页面弄坏。"""
    for path in (
        # 基础
        "/api/v1/instances", "/api/v1/cmdb/assets", "/api/v1/assets",
        "/api/v1/system/dashboard-stats", "/api/v1/modules", "/api/v1/users",
        "/api/v1/auth/login", "/api/v1/engine-catalog",
        "/api/v1/engine-catalog/drivers",
        # 诊断工作台
        "/api/v1/diagnosis/rules", "/api/v1/diagnosis/results",
        "/api/v1/diagnosis-center/incidents", "/api/v1/diagnosis-center/diagnose",
        # 告警与可观测总览
        "/api/v1/alert-events", "/api/v1/observability/overview",
        "/api/v1/observability/sources",
        # 巡检 4 项
        "/api/v1/inspection/tasks", "/api/v1/inspection/results",
        "/api/v1/inspection/report/1",
        # 性能分析 5 项（慢 SQL / 锁等待 / 基线 / 容量 / AWR 综合）
        "/api/v1/slow-sql", "/api/v1/performance/baseline/1/collect",
        "/api/v1/capacity/overview", "/api/v1/performance/baseline",
        "/api/v1/oracle/1/awr/reports", "/api/v1/oracle/1/awr/upload",
        "/api/v1/oracle/1/awr/db-info",
        "/api/v1/oracle/1/slow-sql-realtime", "/api/v1/oracle/1/top-sql",
        "/api/v1/mysql-ops/1/slow-sql", "/api/v1/mysql/1/slow-sql",
        "/api/v1/oracle/1/capacity/summary",
        # 运维 8 项
        "/api/v1/operations/capability-matrix",
        "/api/v1/operations/config-governance/summary",
        "/api/v1/operations/oracle/1/overview",
        "/api/v1/operations/oracle/1/tablespaces",
        "/api/v1/operations/oracle/1/sessions",
        "/api/v1/oracle/1/tablespaces", "/api/v1/oracle/1/users",
        "/api/v1/oracle/1/undo/overview", "/api/v1/oracle/1/temp/overview",
        "/api/v1/oracle/1/stats/tables",
        "/api/v1/mysql-ops/1/binlog", "/api/v1/mysql-ops/1/locks/transactions",
        "/api/v1/mysql-ops/1/users",
        # 报告
        "/api/v1/reports/catalog", "/api/v1/reports/alert",
    ):
        assert edition.path_allowed(path) is True, path


def test_oss_blocks_enterprise_paths(oss):
    for path in (
        # 数据保护
        "/api/v1/backup-center/catalog", "/api/v1/recovery/cases",
        "/api/v1/automation/plans",
        # 智能诊断 / 事故现场
        "/api/v1/incidents", "/api/v1/incidents/1/probes",
        "/api/v1/incident-agents", "/api/v1/incident-snapshots/1/evidence",
        "/api/v1/incident-trigger-rules", "/api/v1/scene-captures",
        # 集成中心
        "/api/v1/zabbix/hosts", "/api/v1/prometheus/alerts",
        "/api/v1/notify/channels", "/api/v1/itsm/tickets",
        # 系统
        "/api/v1/identity/providers", "/api/v1/diagnosis-evaluation/runs",
        # 数据治理
        "/api/v1/data-governance/jobs", "/api/v1/data-governance/object-storage/config",
        # 性能分析：10 项企业
        "/api/v1/sql-profiles/1",
        "/api/v1/performance/overview",
        "/api/v1/performance/workload-reports",
        "/api/v1/performance/sql-profile/1",
        "/api/v1/performance/index-findings/1",
        "/api/v1/performance/table-profile/1",
        "/api/v1/performance/problem-trend",
        "/api/v1/performance/suggestions",
        # 可观测三个子项
        "/api/v1/observability/logs",
        "/api/v1/observability/grafana/templates",
        "/api/v1/observability/apm/trace",
        # 巡检高级
        "/api/v1/inspection/policies", "/api/v1/inspection/policy-templates",
        "/api/v1/inspection/custom-metrics/test",
        # Oracle 引擎段的性能项
        "/api/v1/oracle/1/indexes/unusable", "/api/v1/oracle/1/unused-indexes",
        "/api/v1/oracle/1/table-usage/index-advice", "/api/v1/oracle/1/similar-tables",
        "/api/v1/oracle/1/index-fragmentation", "/api/v1/oracle/1/table-fragmentation",
        "/api/v1/oracle/1/dataguard/overview", "/api/v1/oracle/1/dg-status",
        # AWR 对比与采集（导出/上传/报告列表仍开源，见上一条测试）
        "/api/v1/oracle/1/awr/compare", "/api/v1/oracle/1/awr/collect-now",
        "/api/v1/oracle/1/awr/collect-policy", "/api/v1/oracle/awr/collect-policies",
        # MySQL 高可用与索引
        "/api/v1/mysql-ops/1/mgr", "/api/v1/mysql-ops/1/replication",
        "/api/v1/mysql-ops/1/indexes/duplicate", "/api/v1/mysql-ops/1/tables-indexes/overview",
        "/api/v1/mysql/1/replication",
        # operations 下的高可用 section
        "/api/v1/operations/postgresql/1/ha/ag-status",
        "/api/v1/operations/sqlserver/1/ha/replicas",
        # 扩展引擎运维
        "/api/v1/operations/dm/modules", "/api/v1/operations/kingbasees/modules",
    ):
        assert edition.path_allowed(path) is False, path


def test_same_prefix_split_by_segment(oss):
    """同一个一级前缀下，开源段与付费段要能分开——这是一刀切最容易错的地方。"""
    # /performance：baseline 开源，overview 企业
    assert edition.path_allowed("/api/v1/performance/baseline/1/collect") is True
    assert edition.path_allowed("/api/v1/performance/overview") is False
    # /observability：总览与数据源开源，logs/grafana/apm 企业
    assert edition.path_allowed("/api/v1/observability/overview") is True
    assert edition.path_allowed("/api/v1/observability/logs") is False
    # /oracle/{id}：运维段开源，性能段企业
    assert edition.path_allowed("/api/v1/oracle/1/tablespaces") is True
    assert edition.path_allowed("/api/v1/oracle/1/indexes/unusable") is False
    # AWR 内部再切一刀
    assert edition.path_allowed("/api/v1/oracle/1/awr/reports") is True
    assert edition.path_allowed("/api/v1/oracle/1/awr/compare") is False
    # /inspection：任务/报告开源，策略/模板企业
    assert edition.path_allowed("/api/v1/inspection/tasks") is True
    assert edition.path_allowed("/api/v1/inspection/policies") is False


def test_denial_distinguishes_three_reasons(oss):
    module_payload = edition.denial("/api/v1/backup-center/catalog")
    assert module_payload["code"] == "edition_module_unavailable"
    assert module_payload["edition"] == "oss"
    assert "暂不支持" in module_payload["message"]

    engine_payload = edition.denial("/api/v1/operations/dm/1/overview")
    assert engine_payload["code"] == "edition_engine_unavailable"
    assert engine_payload["engine"] == "dm"

    capability_payload = edition.denial("/api/v1/oracle/1/awr/compare")
    assert capability_payload["code"] == "edition_capability_unavailable"
    assert capability_payload["capability"] == "compare"


def test_non_api_paths_are_not_touched(oss):
    for path in ("/health", "/static/app.js", "/metrics", "/docs"):
        assert edition.path_allowed(path) is True, path


def test_rule_segments_exist_in_real_routes():
    """规则里的字面段必须真实出现在路由表，否则护栏会退化成"看起来像护栏的常量"。"""
    import json
    import subprocess
    import sys

    backend = str(_ROOT / "backend")
    code = (
        "import json;"
        "from app.main import app;"
        "segs=set();"
        "[segs.update([s for s in getattr(r,'path','').split('/') if s]) for r in app.routes];"
        "print(json.dumps(sorted(segs)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, capture_output=True, text=True,
        env={"PYTHONPATH": backend, "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr[-800:]
    route_segments = set(json.loads(result.stdout.strip().splitlines()[-1]))

    # `dm` / `kingbasees` 的运维路由目前是 0 条（见 ISS-71）。规则先占位，
    # 让这两个引擎一旦接入运维就自动落入拦截范围；此处豁免存在性检查。
    PLACEHOLDER = {"dm", "kingbasees"}
    literal = {seg for rule in edition.PAID_ROUTE_RULES for seg in rule
               if seg not in ("{}", "*")} - PLACEHOLDER
    missing = sorted(literal - route_segments)
    assert not missing, f"规则包含路由表中不存在的段: {missing}"


def test_engine_whitelist_matches_frontend_declaration():
    """后端引擎白名单与前端 `engines: [...]` 声明必须同一组值。"""
    assert _MENU_CONFIG.is_file(), f"找不到前端菜单配置: {_MENU_CONFIG}"
    text = _MENU_CONFIG.read_text(encoding="utf-8")
    declared = set()
    for block in re.finditer(r"engines:\s*\[([^\]]*)\]", text):
        declared.update(re.findall(r"[\"']([a-z][a-z0-9_-]*)[\"']", block.group(1)))
    assert declared, "前端没有任何 engines 声明，护栏失效"
    assert declared == set(edition.OSS_OPS_ENGINES), (
        f"前端声明 {sorted(declared)} 与后端白名单 {sorted(edition.OSS_OPS_ENGINES)} 不一致"
    )


def test_performance_baseline_and_capacity_stay_public(oss):
    """回归护栏：慢 SQL / 锁等待 / 性能基线 / 容量分析 / AWR 综合 一旦被误拦即红。

    这五项是最后一轮才划入开源版的，最容易被"整域按 perf. 前缀拦掉"的写法误伤。
    """
    assert edition.module_allowed("perf.slowsql") is True
    assert edition.module_allowed("perf.lock") is True
    assert edition.module_allowed("perf.baseline") is True
    assert edition.module_allowed("perf.capacity") is True
    assert edition.module_allowed("perf.awr.report") is True
    assert edition.module_allowed("perf.awr.history") is False
