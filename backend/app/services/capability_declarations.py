"""Static batch-2 action declarations, not execution or field-acceptance claims.

Paths are API-client paths (without /api/v1 or query strings). All requests in
a composite binding participate in authorization; the primary request is not a
permission substitute. The registry owns permission and maturity derivation.
"""
from __future__ import annotations


EFFECT_RESULT_SCHEMA = "capability.effect_result/v1"
EFFECT_RESULT_VERSION = 3


_ENGINES = ("oracle", "mysql", "postgresql", "sqlserver")


def _binding(target_key, paths, *, branch="read", method="GET", engine=None,
             intent_gap=None):
    if isinstance(paths, str):
        paths = (paths,)
    endpoints = tuple(
        {"endpoint_template": path if isinstance(path, str) else path[1],
         "method": method if isinstance(path, str) else path[0]}
        for path in paths
    )
    value = {"target_key": target_key, "branch": branch, **endpoints[0]}
    if len(endpoints) > 1:
        value["endpoints"] = endpoints
    if engine is not None:
        value["engine"] = engine
    if intent_gap:
        value["intent_gap"] = intent_gap
    return value


def _declaration(capability_id, title, bindings=(), *, kind=None,
                 engines=_ENGINES, requires_instance=False, handler=None,
                 reason=None, risk=None, timeout_seconds=60,
                 verification="Validate returned data and preserve missing/unknown evidence.",
                 output_schema="object", catalog_key=None, declared_maturity=None,
                 version=1, release_gate=None,
                 stop_safe_point="Between requests; in-flight requests are not cancelled."):
    kind = kind or capability_id.rsplit(".", 1)[1]
    value = {
        "capability_id": capability_id,
        "version": version,
        "title": title,
        "domain": capability_id.split(".", 1)[0],
        "kind": kind,
        "execution": "server" if handler else "client",
        "handler": handler,
        "handler_status": "wired" if handler else (
            "declared_not_wired" if reason else "client_bound"),
        "engines": engines,
        "requires_instance": requires_instance,
        "risk": risk or ("medium" if kind in {"collect", "scan", "refresh", "sync", "bind", "execute"} else "low"),
        "timeout_seconds": timeout_seconds,
        "budget": {"max_steps": 20, "max_fanout": 1, "max_retries": 0},
        "stop_safe_point": stop_safe_point,
        "verification": verification,
        "output_schema": output_schema,
        "bindings": bindings,
    }
    if reason:
        value["reason"] = reason
    if catalog_key:
        value["catalog_key"] = catalog_key
    if release_gate:
        value["release_gate"] = release_gate
    if declared_maturity is not None:
        value["declared_maturity"] = declared_maturity
    return value


def _ops_bindings(target, oracle_paths, mysql_path, pg_section, ss_section=None):
    return (
        _binding(target, oracle_paths, engine="oracle"),
        _binding(target, "/mysql-ops/{instance_id}" + mysql_path, engine="mysql"),
        _binding(target, "/operations/postgresql/{instance_id}/" + pg_section,
                 engine="postgresql"),
        _binding(target, "/operations/sqlserver/{instance_id}/" + (ss_section or pg_section),
                 engine="sqlserver"),
    )


_ALERT_BUNDLE = (
    "/alert-events", "/alert-events/stats", "/alert-events/actions",
    "/zabbix/problems", "/prometheus/alerts", "/zabbix-push/logs",
    "/alert-events/maintenance-windows",
)
_ZABBIX_BUNDLE = (
    "/zabbix/configs", "/zabbix/hosts", "/zabbix/problems",
    "/zabbix-push/config", "/zabbix-push/logs",
)
_PROMETHEUS_BUNDLE = ("/prometheus/config", "/prometheus/alerts")
_GOVERNANCE_BUNDLE = (
    "/data-governance/policies", "/data-governance/candidates", "/data-governance/jobs",
)


CAPABILITY_DECLARATIONS: tuple[dict, ...] = (
    _declaration("platform.dashboard.read", "Platform dashboard", (
        _binding("home.dashboard", "/system/dashboard-stats"),),
        version=2, handler="platform.dashboard",
        verification="Same dashboard builder as the web entry, with the caller's asset scope applied."),
    _declaration("platform.user.read", "Users and permission configuration", (
        _binding("system.user", ("/users", "/modules", "/users/password-policy", "/auth/current-user")),),
        handler="platform.users", version=2, output_schema="object",
        verification="Directory fields only; password hashes, MFA and session material are never selected."),
    _declaration("platform.identity.read", "Identity providers and login audit", (
        _binding("system.identity", ("/identity/providers", "/identity/login-audit")),),
        handler="platform.identity", version=2, output_schema="object",
        verification="Existing provider and login-audit readers; secret fields stay redacted."),
    _declaration("platform.settings.read", "System settings", (
        _binding("system.params", "/settings"),),
        handler="platform.settings", version=2, output_schema="object",
        verification="Stored settings only; the reader never seeds defaults."),
    _declaration("platform.ai_config.read", "AI provider configuration", (
        _binding("system.ai", "/ai-config"),),
        handler="platform.ai_config", version=2, output_schema="object",
        verification="Provider metadata only; endpoints and credential material are not selected."),
    _declaration("platform.ai_evaluation.read", "Diagnosis evaluation summary", (
        _binding("system.aiEvaluation", "/diagnosis-evaluation/summary"),),
        handler="ai.evaluation_summary", version=2, output_schema="object",
        verification="Stored evaluation aggregate; no model call and no re-scoring."),
    _declaration("platform.scheduler.read", "Scheduler and AWR collection policies", (
        _binding("system.schedule", ("/scheduler/status", "/scheduler/tasks", "/oracle/awr/collect-policies")),),
        handler="platform.scheduler", version=2, output_schema="object",
        verification="Reads the persisted lease and dispatch runs; a local variable cannot claim leadership."),
    _declaration("asset.inventory.read", "Asset inventory", (
        _binding("asset.center", ("/assets/overview", "/assets/tree", "/cmdb/sources"), branch="inventory"),),
        version=2, handler="asset.inventory", output_schema="object",
        verification="Normalized asset overview filtered by the caller's own asset scope."),
    _declaration("asset.discovery.execute", "Discover and normalize assets", (
        _binding("asset.center", "/assets/discovery/apply", method="POST", branch="discovery"),),
        kind="collect", timeout_seconds=300,
        verification="probe_live=true; inspect normalization counts and warnings, not a pure read."),
    _declaration("diagnosis.root_cause.execute", "Run evidence-backed root cause diagnosis", (
        _binding("diagnose.investigation", "/diagnosis-center/diagnose", method="POST", branch="root_cause"),),
        kind="collect", requires_instance=True, handler="diagnosis.root_cause", version=1,
        timeout_seconds=900, output_schema="diagnosis.root_cause_result/v1",
        verification="A persisted incident, its evidence chain and root-cause rows must match this exact plan execution. "
                     "The requested time window is context; collection is a current snapshot, not historical replay."),
    _declaration("monitor.alert.read", "Unified alert evidence", (
        _binding("monitor.alert.unified", _ALERT_BUNDLE, branch="alerts"),
        _binding("report.alert", _ALERT_BUNDLE, intent_gap="Alert report actually reads the alert bundle, not a report artifact.")),
        handler="monitor.alerts", version=2, output_schema="object",
        verification="Stored events narrowed by scope; SLA advancement stays in the scheduled consumer."),
    _declaration("monitor.alert.sync", "Import capacity and external alerts", (
        _binding("monitor.alert.unified", ("/alert-events/import/capacity", "/alert-events/import/external"),
                 method="POST", branch="import_alerts"),),
        handler="monitor.alert_import", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Event ids touched by this import are the evidence; an empty cache is partial, not success."),
    _declaration("monitor.capacity_alert.read", "Capacity alert evidence", (
        _binding("monitor.alert.db", ("/capacity/alerts", "/capacity/alerts/stats"), branch="alerts"),),
        requires_instance=False, handler="monitor.capacity_alerts", version=2, output_schema="object",
        verification="Existing capacity alert reader, narrowed by the caller's instance scope."),
    _declaration("monitor.capacity.execute", "Collect capacity snapshot", (
        _binding("monitor.alert.db", "/capacity/{instance_id}/collect", method="POST", branch="collect"),),
        kind="collect", requires_instance=True, timeout_seconds=300, version=EFFECT_RESULT_VERSION,
        handler="monitor.capacity_collect", output_schema=EFFECT_RESULT_SCHEMA,
        stop_safe_point="Between the fence check and the snapshot write; an interrupted write is retried.",
        verification="A stored snapshot row is the evidence; a collector that returns nothing is unknown, not success."),
    _declaration("monitor.observability.read", "Probe observability linkage", (
        _binding("monitor.observability", "/observability/linkage-health", branch="linkage_health"),),
        kind="read", requires_instance=True, handler="observability.linkage", version=2, output_schema="object",
        verification="Stored linkage health only (probe_sources=false); probing is not part of this read."),
    _declaration("monitor.observability.bind", "Auto-bind observability sources", (
        _binding("monitor.observability", "/observability/bindings/auto", method="POST", branch="auto_bind"),),
        handler="observability.bind", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Persisted instance-binding rows returned by this run are the evidence; no match is partial."),
    _declaration("monitor.log.read", "Query linked logs", (
        _binding("monitor.observability.logs", "/observability/logs/query", method="POST", branch="logs",
                 intent_gap="Client omits required source_id, start_time and end_time; explicit query parameters are needed."),),
        kind="query", requires_instance=True, handler="monitor.logs", version=2, output_schema="object",
        verification="Explicit source/window query through the existing adapter; records a query audit."),
    _declaration("monitor.timeline.read", "Build fault timeline", (
        _binding("monitor.observability.logs", "/observability/fault-timeline", method="POST", branch="fault_timeline",
                 intent_gap="Client sends query/time_range but no events or explicit time bounds."),),
        requires_instance=True,
        reason="Timeline is a client-side sort over events the page already holds; there is no "
               "server-side evidence source to read, so it is not exposed as a plan step."),
    _declaration("monitor.grafana_template.read", "Grafana templates", (
        _binding("monitor.observability.grafana", "/observability/grafana/templates"),),
        handler="grafana.templates", version=2, output_schema="object",
        verification="Static template catalogue; building/exporting a dashboard stays separate."),
    _declaration("monitor.apm_source.read", "APM and observability source status", (
        _binding("monitor.observability.apm", (
            "/observability/overview", "/observability/sources", "/observability/standard-metrics",
            "/observability/metric-mappings", "/observability/bindings", "/observability/policies",
            "/observability/tasks", "/observability/logs", "/observability/alerts",
            "/observability/exporters", "/observability/metrics/current", "/observability/metrics/history",
            "/observability/data-quality"), branch="source_status"),),
        kind="read", handler="observability.sources", version=2, output_schema="object",
        verification="Stored source/binding/policy registers and counts; this path does not seed mappings or policies."),
    _declaration("monitor.apm_trace.read", "Query APM trace", (
        _binding("monitor.observability.apm", "/observability/apm/trace", method="POST", branch="trace_id",
                 intent_gap="Client supplies trace_id but omits required source_id."),),
        kind="query", requires_instance=True, handler="monitor.apm", version=2, output_schema="object",
        verification="One trace from the configured APM adapter; the query audit is recorded and unsupported adapters fail, never fabricate."),
    _declaration("inspection.run.query", "Inspection jobs", (
        _binding("inspect.overview", "/inspection/jobs", branch="jobs",
                 intent_gap="Current frontend still reads /inspection/tasks (historical results); server reads jobs."),
        _binding("inspect.tasks", "/inspection/jobs", branch="jobs",
                 intent_gap="Current frontend still reads /inspection/tasks (historical results); server reads jobs."),
        _binding("inspect.overview", "/inspection/tasks", branch="historical_results",
                 intent_gap="Legacy frontend reads historical results, not the new server job list."),
        _binding("inspect.tasks", "/inspection/tasks", branch="historical_results",
                 intent_gap="Legacy frontend reads historical results, not the new server job list."),
        _binding("inspect.reports", "/inspection/tasks", branch="historical_results",
                 intent_gap="Report view actually lists run results; completed filter is ignored; report handler is not wired."),
        _binding("inspect.compare", "/inspection/tasks", branch="historical_results",
                 intent_gap="Comparison actually lists run results; no report comparison occurs; report handler is not wired.")),
        handler="inspection.list_jobs", output_schema="inspection.run_list/v1"),
    _declaration("inspection.report.read", "Inspection report intent", kind="query",
        reason="Independent report handler is not wired; inspect.reports/inspect.compare only bind to run result lists."),
    _declaration("inspection.template.read", "Inspection policies and templates", (
        _binding("inspect.policies", "/inspection/policies"),
        _binding("inspect.templates", "/inspection/policies")),
        handler="inspection.list_policies", output_schema="inspection.policy_list/v1",
        verification="Server must call list_policies(initialize_defaults=False); legacy GET may initialize defaults."),
    _declaration("inspection.run.execute", "Queue inspection run", (
        _binding("inspect.run", "/inspection/{db_type}/{instance_id}/run", method="POST", branch="queued_run"),),
        requires_instance=True, timeout_seconds=3600, version=2, handler="inspection.run_frozen",
        release_gate="inspection_plan",
        stop_safe_point="Before queue submission; stopping polling does not cancel the queued job.",
        verification="Read job identity and run readback; accepted/queued is not successful execution.",
        output_schema="inspection.plan_result/v1"),
    _declaration("performance.overview.read", "Performance overview", (
        _binding("perf.overview", "/performance/overview"),),
        version=2, handler="performance.overview",
        verification="GET aggregates stored scores and suggestions; unlike the design table, no live collection is called. "
                     "Scoped callers are refused because the aggregation is platform-wide."),
    _declaration("performance.workload.collect", "Engine workload report", (
        _binding("perf.workload", "/performance/workload-reports/{instance_id}/collect", method="POST", branch="collect"),),
        requires_instance=True, timeout_seconds=300, handler="performance.workload_collect",
        version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="The DBWorkloadReport row written by this run is the evidence; failed collection is failed."),
    _declaration("performance.sql_profile.collect", "Collect SQL profile", (
        _binding("perf.sql", "/performance/sql-profile/{instance_id}/collect", method="POST", branch="collect"),),
        requires_instance=True, timeout_seconds=300,
        handler="performance.sql_profile_collect", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Only rows stamped by this run are live evidence; merged offline profiles alone are partial. "
                     "PostgreSQL/SQL Server are unsupported, not live collection."),
    _declaration("performance.slow_sql.read", "Slow SQL evidence", (
        _binding("perf.slowsql", "/oracle/{instance_id}/slow-sql-realtime", engine="oracle"),
        _binding("perf.slowsql", "/mysql-ops/{instance_id}/slow-sql/top", engine="mysql")),
        engines=("oracle", "mysql"), requires_instance=True, version=2,
        handler="performance.slow_sql",
        verification="Live SQL inspection, not an offline read; retain source, time window and degraded status."),
    _declaration("performance.lock.read", "Lock and blocking evidence", (
        _binding("perf.lock", "/oracle/{instance_id}/blocking-tree", engine="oracle"),
        _binding("perf.lock", "/mysql-ops/{instance_id}/locks/transactions", engine="mysql"),
        _binding("perf.lock", "/operations/postgresql/{instance_id}/sessionTransaction", engine="postgresql"),
        _binding("perf.lock", "/operations/sqlserver/{instance_id}/sessionTransaction", engine="sqlserver")),
        engines=("oracle", "mysql", "postgresql", "sqlserver"), requires_instance=True,
        version=3, handler="performance.lock", output_schema="engine_specific_response",
        verification="Oracle queries the live blocking tree used by the Oracle lock page; MySQL returns live lock waits. "
                     "PostgreSQL/SQL Server return the live session section."),
    _declaration("performance.index.collect", "Collect index findings", (
        _binding("perf.index", "/performance/index-findings/{instance_id}/collect", method="POST", branch="collect"),),
        engines=("oracle", "mysql"), requires_instance=True, timeout_seconds=300,
        handler="performance.index_collect", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Finding rows written by this run are the evidence; zero findings is partial, not success. "
                     "Other engines return unsupported without publishing a snapshot."),
    _declaration("performance.table_profile.collect", "Collect table profile", (
        _binding("perf.table", "/performance/table-profile/{instance_id}/collect", method="POST", branch="collect"),),
        engines=("oracle", "mysql"), requires_instance=True, timeout_seconds=300,
        handler="performance.table_profile_collect", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Rows with this capture_time are the evidence; a complete empty snapshot is partial. "
                     "Other engines return unsupported without publishing a snapshot."),
    _declaration("performance.suggestion.refresh", "Rebuild performance suggestions", (
        _binding("perf.optimization", "/performance/suggestions/refresh", method="POST", branch="refresh"),),
        requires_instance=True,
        reason="Approval-gated server execution is not wired yet: a rebuild can legitimately write zero rows, "
               "so there is no row-level evidence that would distinguish success from a skipped run."),
    _declaration("performance.baseline.collect", "Collect performance baseline", (
        _binding("perf.baseline", "/performance/baseline/{instance_id}/collect", method="POST", branch="collect"),),
        requires_instance=True, timeout_seconds=300, version=EFFECT_RESULT_VERSION,
        handler="performance.baseline_collect", output_schema=EFFECT_RESULT_SCHEMA,
        verification="The DBPerfMetric rows written after this step started, with the same count and names."),
    _declaration("performance.trend.read", "Problem trend", (
        _binding("perf.trend", "/performance/problem-trend"),),
        requires_instance=False, handler="performance.trend", version=2, output_schema="object",
        verification="Existing trend aggregate with a bounded day window; no new collection."),
    _declaration("performance.capacity.collect", "Collect capacity evidence", (
        _binding("perf.capacity", "/capacity/{instance_id}/collect", method="POST", branch="collect"),),
        requires_instance=True, timeout_seconds=300,
        handler="monitor.capacity_collect", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="The capacity policy row written by this run is the evidence, same as monitor.capacity.execute."),
    _declaration("performance.awr.collect", "Collect Oracle AWR", (
        _binding("perf.awr.report", "/oracle/{instance_id}/awr/collect-now", method="POST", branch="collect"),
        _binding("perf.awr.collect", "/oracle/{instance_id}/awr/collect-now", method="POST", branch="collect")),
        engines=("oracle",), requires_instance=True, timeout_seconds=300),
    _declaration("performance.awr.query", "Oracle AWR history", (
        _binding("perf.awr.history", "/oracle/{instance_id}/awr/reports", branch="history"),
        _binding("perf.awr.compare", "/oracle/{instance_id}/awr/reports", branch="history",
                 intent_gap="Compare only loads report history; no comparison endpoint is called.")),
        kind="read", engines=("oracle",), requires_instance=True,
        handler="performance.awr_history", version=2, output_schema="object",
        verification="Stored AWR report rows for the target; file/detail reads remain separate actions."),
    # 无独立页面：15 项能力 × 引擎的支持模式属说明性内容（见 docs/blueprint/engine-capabilities.md）。
    # 能力本身保留给 AI 调度与 API 直接调用，因此 target_key 为空——不能让模型导航到已不存在的页面。
    _declaration("operations.capability_matrix.read", "Engine capability matrix", (
        _binding(None, "/operations/capability-matrix"),),
        handler="operations.capability_matrix", output_schema="operations.capability_matrix"),
    _declaration("operations.config_governance.read", "Configuration baselines and drift", (
        _binding("ops.configGovernance", ("/operations/config-governance/summary",
                 "/operations/config-governance/baselines", "/operations/config-governance/drifts")),),
        handler="config.governance_summary", version=2, output_schema="object",
        verification="Shared summary service with the caller's scope; baselines and active drifts are read-only."),
    _declaration("operations.runtime_overview.read", "Live instance overview",
        _ops_bindings("ops.overview", "/oracle/{instance_id}/basic-info", "/basic-info", "overview"),
        requires_instance=True, catalog_key="runtime.overview", version=2,
        handler="operations.runtime_overview",
        verification="Live database inspection; do not interpret transport success as health."),
    _declaration("operations.log.read", "Live database log state",
        _ops_bindings("ops.log", ("/oracle/{instance_id}/archive-config", "/oracle/{instance_id}/archive-space-detail"),
                      "/binlog/overview", "wal", "logs"),
        requires_instance=True, catalog_key="logs.runtime", version=2, handler="operations.log"),
    _declaration("operations.session.read", "Live sessions and transactions", (
        _binding("ops.session", "/mysql-ops/{instance_id}/locks/transactions", engine="mysql"),
        _binding("ops.session", "/operations/postgresql/{instance_id}/sessionTransaction", engine="postgresql"),
        _binding("ops.session", "/operations/sqlserver/{instance_id}/sessionTransaction", engine="sqlserver")),
        engines=("mysql", "postgresql", "sqlserver"), requires_instance=True,
        catalog_key="runtime.sessions", version=2, handler="operations.session",
        verification="Oracle session detail exists only inside the router, so Oracle is not claimed."),
    _declaration("operations.space.read", "Live database space",
        _ops_bindings("ops.space", "/operations/oracle/{instance_id}/tablespaces", "/capacity/schemas", "space"),
        requires_instance=True, catalog_key="storage.capacity", version=2, handler="operations.space"),
    _declaration("operations.object.read", "Object and table statistics", (
        _binding("ops.object", "/performance/table-profile/{instance_id}", engine="oracle", branch="table_profile",
                 intent_gap="Oracle object read now returns the stored table profile; live collection is a separate action."),
        _binding("ops.object", "/mysql-ops/{instance_id}/tables-indexes/overview", engine="mysql"),
        _binding("ops.object", "/operations/postgresql/{instance_id}/objectStats", engine="postgresql"),
        _binding("ops.object", "/operations/sqlserver/{instance_id}/objectStats", engine="sqlserver")),
        kind="read", requires_instance=True, catalog_key="objects.statistics",
        handler="operations.objects", version=2, output_schema="object",
        verification="Returns stored table profiles for the target; live collection stays a separate action."),
    _declaration("operations.security.read", "Database users and roles", (
        _binding("ops.security", ("/oracle/{instance_id}/users", "/oracle/{instance_id}/roles"), engine="oracle"),
        _binding("ops.security", "/mysql-ops/{instance_id}/users", engine="mysql")),
        engines=("oracle", "mysql"), requires_instance=True,
        handler="operations.security", version=2, output_schema="object",
        verification="Engine account/role read; a failed connection raises instead of returning an empty list."),
    _declaration("operations.ha.read", "Replication and HA status",
        _ops_bindings("ops.ha", "/oracle/{instance_id}/dataguard/overview", "/replication/status", "ha"),
        requires_instance=True, version=2, handler="operations.ha",
        verification="Live replication/Data Guard inspection; transport success is not a healthy verdict."),
    _declaration("operations.startup.plan", "Oracle startup script", (
        _binding("ops.overview", "/oracle/{instance_id}/startup-script", engine="oracle", branch="startup"),),
        engines=("oracle",), requires_instance=True,
        handler="operations.startup_plan", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Generated script contains STARTUP and plan_only=true; no database startup is executed."),
    _declaration("operations.shutdown.plan", "Oracle shutdown script", (
        _binding("ops.overview", "/oracle/{instance_id}/shutdown-script", engine="oracle", branch="shutdown"),),
        engines=("oracle",), requires_instance=True,
        handler="operations.shutdown_plan", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Generated script contains SHUTDOWN and plan_only=true; no database shutdown is executed."),
    _declaration("operations.switchover.plan", "Data Guard switchover checks and plan", (
        _binding("ops.ha", "/oracle/{instance_id}/dataguard/switchover-check-plan", method="POST",
                 engine="oracle", branch="switchover"),), engines=("oracle",), requires_instance=True,
        kind="plan", handler="operations.switchover_plan", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Generated pre-check plan only (plan_only/execution_blocked); no switchover is executed."),
    _declaration("operations.termination.execute", "Terminate database session", requires_instance=True,
        reason="unsupported: real session termination is not authorized or wired; placeholders are not execution.",
        risk="high", catalog_key="runtime.terminate_session",
        declared_maturity={engine: "unsupported" for engine in _ENGINES},
        verification="Always unsupported; never invoke placeholder kill plans."),
    _declaration("protection.platform.read", "Backup platform health and drills", (
        _binding("backup.platform", ("/backup-center/platform/configs", "/backup-center/platform/runs",
                 "/backup-center/platform/health/{instance_id}", "/backup-center/platform/restore-drills")),),
        requires_instance=False, handler="protection.platform", version=2, output_schema="object",
        verification="Stored platform configs (no secret) plus recent runs and restore drills."),
    _declaration("protection.backup.execute", "Execute backup", requires_instance=True,
        reason="Backup execution has no assistant handler; catalog maturity must be derived by the registry.",
        risk="high", catalog_key="protection.backup"),
    _declaration("protection.restore.execute", "Execute restore", requires_instance=True,
        reason="Restore execution has no assistant handler; catalog maturity must be derived by the registry.",
        risk="high", catalog_key="protection.restore"),
    _declaration("protection.automation.read", "Automation plans approvals and runs", (
        _binding("protection.automation", ("/automation/plans", "/automation/approvals", "/automation/runs")),),
        handler="protection.automation", version=2, output_schema="object",
        verification="Reads the plan/approval/run registers; approval and execution stay separate actions."),
    _declaration("rescue.case.read", "Recovery cases and source images", (
        _binding("rescue.cases", ("/recovery/cases", "/recovery/source-images")),),
        version=2, handler="rescue.cases", output_schema="object",
        verification="Case registry read through the recovery governance service; no custody claim."),
    _declaration("rescue.tool.read", "Recovery tools and authorizations", (
        _binding("rescue.tools", ("/recovery/agent-tools", "/recovery/tool-authorizations")),),
        version=2, handler="rescue.tools", output_schema="object",
        verification="Tool catalogue plus authorizations; authorization is not execution."),
    _declaration("rescue.agent.read", "Recovery agents", (
        _binding("rescue.agents", "/recovery/agents"),),
        handler="rescue.agents", version=2, output_schema="object",
        verification="Agent tool inventory and authorizations from the recovery governance service."),
    _declaration("report.document.generate", "Generate unified operations / after-sales report", (
        _binding("report.unified", "/reports/catalog/snapshots", method="POST", branch="generate"),),
        kind="collect", engines=_ENGINES, requires_instance=True, handler="report.document_generate", version=3,
        output_schema="report.document_result/v1", timeout_seconds=300,
        verification="ReportArtifact id/hash/source_key are the evidence; no evidence is partial, AI failure degrades to rules."),
    _declaration("report.catalog.read", "Unified report summaries", (
        _binding("report.unified", ("/reports/unified/stats", "/reports/unified"),
                 intent_gap="Legacy unified report aggregation, not the newer /reports/catalog artifact API."),),
        version=2, handler="report.catalog", output_schema="object",
        verification="Signed artifact catalogue through the report center; metadata only, no content."),
    _declaration("integration.zabbix.read", "Zabbix configuration hosts and problems", (
        _binding("integration.zabbix", _ZABBIX_BUNDLE, branch="status"),),
        version=2, handler="integration.zabbix", output_schema="object",
        verification="Stored Zabbix configuration/bindings/problem cache; no live Zabbix API call."),
    _declaration("integration.zabbix.sync", "Synchronize Zabbix hosts and problems", (
        _binding("integration.zabbix", _ZABBIX_BUNDLE + (
            ("POST", "/zabbix/configs/{config_id}/sync-hosts"), ("POST", "/zabbix/problems/sync")),
            branch="sync"),), timeout_seconds=300,
        handler="integration.zabbix_sync", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Host mappings and problem rows written by this run are the evidence; "
                     "zero matches is partial and an evidence mismatch is failed."),
    _declaration("integration.prometheus.read", "Prometheus configuration and alerts", (
        _binding("integration.prometheus", _PROMETHEUS_BUNDLE, branch="status"),),
        version=2, handler="integration.prometheus", output_schema="object",
        verification="Stored observability sources and ingested alerts; no live scrape."),
    _declaration("integration.prometheus.sync", "Synchronize Prometheus standard metrics", (
        _binding("integration.prometheus", _PROMETHEUS_BUNDLE + ("/observability/sources",
            ("POST", "/observability/prometheus/sync-standard-metrics")), branch="sync"),),
        timeout_seconds=300, handler="integration.prometheus_sync", version=EFFECT_RESULT_VERSION, output_schema=EFFECT_RESULT_SCHEMA,
        verification="Rows stored in ObservabilityCurrentMetric by this run are the evidence; zero rows is partial."),
    _declaration("integration.notify.read", "Notification channels policies templates and logs", (
        _binding("integration.notify", ("/notify/channels", "/notify/policies", "/notify/templates", "/notify/logs")),),
        handler="integration.notify", version=2, output_schema="object",
        verification="Existing serializers redact channel secrets; templates expose names only."),
    _declaration("integration.itsm.read", "ITSM configuration tickets and rules", (
        _binding("integration.itsm", ("/itsm/configs", "/itsm/tickets", "/itsm/rules")),),
        handler="integration.itsm", version=2, output_schema="object",
        verification="Existing serializers redact configuration secrets; tickets are a bounded recent window."),
)
