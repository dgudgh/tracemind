import {
  aiConfigApi,
  capacityApi,
  genericOpsApi,
  mysqlOpsApi,
  oracleOpsApi,
  settingsApi,
  systemApi,
} from "../apiClients";
import { listAssistantCapabilities } from "./assistantApi";
import {
  applyAssetDiscovery,
  getAssetOverview,
  getAssetTree,
  listCmdbSources,
} from "./cmdbApi";
import {
  collectIndexFindings,
  collectPerformanceBaseline,
  collectSqlProfile,
  collectTableProfile,
  getPerformanceOverview,
  getProblemTrend,
  refreshPerformanceSuggestions,
  collectWorkloadReport,
} from "./performanceApi";
import {
  collectAwrNow,
  getAwrCollectPolicies,
  getAwrReports,
} from "./performanceAwrApi";
import {
  getOracleSlowSqlRealtime,
} from "./inspectionApi";
import { slowSqlConclusion, slowSqlSnapshot } from "../SlowSqlResult";
import {
  getCapacityAlertBundle,
} from "./capacityAlertsApi";
import {
  getGovernanceBundle,
  scanGovernanceCandidates,
} from "./dataGovernanceApi";
import {
  autoBindObservability,
  buildObservabilityFaultTimeline,
  getObservabilityBundle,
  getObservabilityLinkageHealth,
  listGrafanaTemplates,
  listObservabilitySources,
  queryApmTrace,
  queryObservabilityLogs,
  syncPrometheusStandardMetrics,
} from "./observabilityApi";
import {
  getUnifiedAlertBundle,
  importCapacityAlerts,
  importExternalAlerts,
} from "./unifiedAlertEventsApi";
import {
  getUnifiedReports,
  getUnifiedReportStats,
} from "./reportApi";
import {
  getSchedulerStatus,
  getSchedulerTasks,
} from "./schedulerApi";
import {
  getItsmBundle,
  getNotifyBundle,
} from "./workflowApi";
import {
  getUserManagementBundle,
} from "./userManagementApi";
import { configGovernanceApi } from "./configGovernanceApi";
import {
  listFaultPatternMatches,
  listKernelCapabilities,
  listProbeLeases,
  listSceneCaptures,
  listStateTimeline,
} from "./stateInvestigationApi";

const NAVIGATION_TERMS = ["打开", "进入", "跳转", "带我去"];
const PARAMETER_EDIT_TERMS = ["配置", "新增", "添加", "编辑", "修改", "设置"];
const EXECUTION_TERMS = [
  "帮我",
  "请",
  "执行",
  "运行",
  "开始",
  "立即",
  "采集",
  "刷新",
  "同步",
  "识别",
  "发现",
  "测试",
  "检查",
  "检测",
  "分析",
  "查询",
  "查看",
  "生成",
  "导入",
  "导出",
  "巡检",
  "备份",
  "恢复",
  "还原",
];

const INSTANCE_TARGETS = new Set([
  "monitor.alert.db",
  "monitor.observability",
  "monitor.observability.logs",
  "monitor.observability.apm",
  "inspect.overview",
  "inspect.compare",
  "perf.sql",
  "perf.workload",
  "perf.slowsql",
  "perf.lock",
  "perf.index",
  "perf.table",
  "perf.optimization",
  "perf.baseline",
  "perf.trend",
  "perf.capacity",
  "perf.awr.report",
  "perf.awr.history",
  "perf.awr.compare",
  "perf.awr.collect",
  "ops.overview",
  "ops.log",
  "ops.session",
  "ops.space",
  "ops.object",
  "ops.governance",
  "ops.security",
  "ops.ha",
  "backup.plan",
  "backup.restore",
  "backup.platform",
]);

const TARGET_TITLES = {
  "home.dashboard": "加载平台运行总览",
  "asset.center": "识别并汇总资产",
  "monitor.alert.unified": "汇总综合告警",
  "monitor.alert.db": "分析容量告警",
  "monitor.observability": "检查可观测联动",
  "inspect.overview": "加载巡检总览",
  "inspect.tasks": "查询巡检任务",
  "inspect.reports": "查询巡检报告",
  "perf.slowsql": "分析慢 SQL",
  "perf.lock": "分析锁等待",
  "perf.baseline": "采集性能基线",
  "perf.capacity": "采集容量快照",
  "perf.awr.report": "采集 AWR",
  "ops.configGovernance": "检查配置基线与漂移",
  "ops.overview": "采集运行概览",
  "ops.log": "采集日志状态",
  "ops.session": "分析会话事务",
  "ops.space": "分析空间使用",
  "ops.object": "采集对象统计",
  "ops.security": "检查用户安全",
  "report.unified": "汇总统一报告",
  "report.alert": "汇总告警报告",
  "system.user": "加载用户与权限",
  "system.params": "加载系统参数",
  "system.ai": "检查 AI 配置",
  "system.schedule": "检查调度任务",
};

const SUPPORTED_TARGETS = new Set(Object.keys(TARGET_TITLES));

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[\s_\-:：/\\()[\]{}.,，。;；'"“”‘’!?！？]+/g, "");
}

function includesAny(text, terms) {
  return terms.some((term) => text.includes(normalizeText(term)));
}

function isNavigationOnly(text) {
  const navigation = includesAny(text, NAVIGATION_TERMS);
  const execution = includesAny(
    text,
    EXECUTION_TERMS.filter((term) => !["帮我", "请", "查看", "查询"].includes(term)),
  );
  return navigation && !execution;
}

const PROVISION_COMMAND = /(添加|新增|新建|创建|编辑|修改|配置).*(数据库)?实例|(数据库)?实例.*(添加|新增|新建|创建|编辑|修改|配置)/;

export function isInstanceProvisioningCommand(value) {
  return PROVISION_COMMAND.test(normalizeText(value));
}

function needsParameterEditing(text) {
  return includesAny(text, PARAMETER_EDIT_TERMS)
    && !includesAny(text, ["测试", "同步", "识别", "刷新", "采集", "执行", "运行"]);
}

export function resolveAssistantActionRequest(value, intent) {
  const text = normalizeText(value);
  const targetKey = String(intent?.target_key || intent?.targetKey || "");
  if (!text || !targetKey || targetKey === "diagnose.smart" || targetKey === "inspect.run") return null;
  // Creating or editing an instance is page navigation, not a database operation that needs
  // parameters on an existing instance. Let the asset page open its "新增实例" form.
  if (targetKey === "asset.center" && isInstanceProvisioningCommand(text)) return null;
  if (!SUPPORTED_TARGETS.has(targetKey) || isNavigationOnly(text)) return null;

  return {
    key: targetKey,
    targetKey,
    title: TARGET_TITLES[targetKey] || intent?.target_name || intent?.title || "执行平台功能",
    targetName: intent?.target_name || intent?.title || TARGET_TITLES[targetKey] || "平台功能",
    command: String(value || "").trim(),
    targetDbType: String(intent?.target_db_type || intent?.targetDbType || "").toLowerCase(),
    requiresInstance: INSTANCE_TARGETS.has(targetKey),
    requiresParameters: needsParameterEditing(text),
  };
}

function cloneState(state) {
  return {
    ...state,
    metrics: state.metrics.map((item) => ({ ...item })),
    steps: state.steps.map((item) => ({ ...item })),
  };
}

function initialState(request) {
  return {
    id: `assistant-action-${Date.now()}`,
    title: request.title,
    targetKey: request.targetKey,
    targetName: request.targetName,
    instanceId: request.instanceId || null,
    command: request.command,
    status: "running",
    outcome: null,
    percent: 12,
    summary: "",
    notice: "",
    error: "",
    preview: "",
    metrics: [],
    steps: [
      { key: "understand", title: "理解指令", status: "success", detail: request.command },
      { key: "context", title: "校验执行上下文", status: "running", detail: "正在确认实例、数据库类型和权限边界" },
      { key: "execute", title: "调用平台功能", status: "pending", detail: request.targetName },
      { key: "result", title: "整理执行结果", status: "pending", detail: "等待功能返回" },
    ],
  };
}

function updateStep(state, key, patch) {
  const row = state.steps.find((item) => item.key === key);
  if (row) Object.assign(row, patch);
}

function withTimeout(promise, timeoutMs) {
  let timer;
  const timeout = new Promise((_, reject) => {
    const timeoutSeconds = Math.max(1, Math.round(timeoutMs / 1000));
    timer = setTimeout(() => reject(new Error(`执行超过 ${timeoutSeconds} 秒，已停止等待结果`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function unwrapResult(result) {
  if (result?.__assistantResult) return result;
  return { data: result };
}

function safePreview(value) {
  if (value == null) return "";
  try {
    const text = JSON.stringify(value, null, 2);
    return text.length > 7000 ? `${text.slice(0, 7000)}\n...结果已截断` : text;
  } catch {
    return String(value);
  }
}

function collectCount(value) {
  if (Array.isArray(value)) return value.length;
  if (!value || typeof value !== "object") return null;
  if (Number.isFinite(Number(value.total))) return Number(value.total);
  if (Number.isFinite(Number(value.count))) return Number(value.count);
  if (Array.isArray(value.items)) return value.items.length;
  if (Array.isArray(value.rows)) return value.rows.length;
  return null;
}

function presentation(result, request) {
  const wrapped = unwrapResult(result);
  const data = wrapped.data;
  const metrics = [...(wrapped.metrics || [])];
  const count = collectCount(data);
  if (count !== null && !metrics.some((item) => item.label === "返回记录")) {
    metrics.push({ label: "返回记录", value: count });
  }
  const summary = wrapped.summary
    || data?.message
    || (typeof data?.summary === "string" ? data.summary : "")
    || `${request.targetName}执行完成`;
  return {
    summary,
    notice: wrapped.notice || "",
    metrics: metrics.slice(0, 8),
    preview: safePreview(data),
    executionResult: wrapped.executionResult || null,
  };
}

export async function runAssistantActionProgress({
  request,
  instance,
  execute,
  onProgress,
  timeoutMs = 90_000,
}) {
  const state = initialState(request);
  const emit = () => onProgress?.(cloneState(state));
  emit();

  if (request.requiresParameters) {
    updateStep(state, "context", {
      status: "failed",
      detail: "该操作需要业务参数或变更确认",
    });
    state.status = "needs_input";
    state.percent = 25;
    state.error = "已识别目标功能，但不能猜测配置值或删除对象。请点击“打开功能”补充参数并确认。";
    emit();
    return cloneState(state);
  }

  if (request.requiresInstance && !instance?.id) {
    updateStep(state, "context", {
      status: "failed",
      detail: "未找到可执行的数据库实例",
    });
    state.status = "needs_input";
    state.percent = 25;
    state.error = "请先在右上角选择数据库实例，或在指令中明确 Oracle、MySQL、PostgreSQL、SQL Server。";
    emit();
    return cloneState(state);
  }

  updateStep(state, "context", {
    status: "success",
    detail: instance
      ? `${instance.instance_name || instance.name} · ${(instance.db_type || "").toUpperCase()} · ${instance.host}:${instance.port}`
      : "无需指定数据库实例",
  });
  updateStep(state, "execute", { status: "running", detail: `正在调用${request.targetName}` });
  state.percent = 45;
  emit();

  try {
    const result = await withTimeout(Promise.resolve(execute(request, { instance })), timeoutMs);
    const payload = unwrapResult(result).data;
    const backendStatus = String(payload?.status || "").toLowerCase();
    const outcome = String(payload?.outcome ?? backendStatus).toLowerCase();
    state.outcome = payload?.outcome ?? null;
    const nonSuccess = ["unsupported", "blocked", "unknown", "expired", "cancelled", "needs_input", "failed", "degraded", "error", "plan_only"];
    const nonFinal = ["draft", "validated", "awaiting_approval", "approved", "queued", "pending", "running", "accepted"];
    const stopped = nonSuccess.includes(outcome) || nonFinal.includes(outcome) ? outcome
      : nonFinal.includes(backendStatus) ? backendStatus
        : (backendStatus || payload?.outcome != null) && !["success", "succeeded", "ok", "healthy", "available", "partial"].includes(outcome) ? "unknown" : null;
    if (stopped) {
      const view = presentation(result, request);
      state.status = ["degraded", "error"].includes(stopped) ? "failed" : stopped;
      state.percent = nonFinal.includes(stopped) ? 45 : 25;
      state.summary = payload?.message || payload?.outcome_reason || payload?.error
        || (stopped === "plan_only" ? `${request.targetName}仅生成方案，尚未执行变更` : "功能尚未确认完成，请查看返回结果与限制");
      state.error = stopped === "plan_only" ? "" : state.summary;
      state.notice = stopped === "plan_only" ? [view.notice, "当前仅生成方案，尚未执行变更"].filter(Boolean).join(" ") : view.notice;
      state.preview = view.preview;
      state.code = payload?.code || null;
      updateStep(state, "execute", { status: state.status, detail: state.summary });
      updateStep(state, "result", { status: "pending", detail: "未确认执行完成" });
      emit();
      return cloneState(state);
    }
    updateStep(state, "execute", { status: "success", detail: `${request.targetName}已返回结果` });
    updateStep(state, "result", { status: "running", detail: "正在整理关键指标和结果摘要" });
    state.percent = 82;
    emit();

    const view = presentation(result, request);
    state.status = outcome === "partial" ? "partial" : "success";
    state.percent = 100;
    state.summary = view.summary;
    state.notice = view.notice;
    state.metrics = view.metrics;
    state.preview = view.preview;
    state.executionResult = view.executionResult;
    if (state.status === "partial") {
      state.summary = `${request.targetName}部分完成`;
      state.notice = payload.message || "部分步骤未完成，请查看结果中的限制与错误";
    }
    updateStep(state, "result", { status: state.status === "success" ? "success" : "warning", detail: state.notice || view.summary });
  } catch (error) {
    state.status = "failed";
    state.percent = 100;
    state.error = String(error?.message || error || "功能执行失败");
    updateStep(state, "execute", { status: "failed", detail: state.error });
    updateStep(state, "result", { status: "failed", detail: "未生成有效结果" });
  }
  emit();
  return cloneState(state);
}

function result(data, summary, options = {}) {
  return {
    __assistantResult: true,
    data,
    summary,
    notice: options.notice || "",
    metrics: options.metrics || [],
    executionResult: options.executionResult || null,
  };
}

function commandHas(request, terms) {
  return includesAny(normalizeText(request.command), terms);
}

// Code-owned routing only. Catalog bindings authorize these branches, never dispatch URLs.
const ACTION_BINDINGS = {
  "home.dashboard": ["platform.dashboard.read", "read"],
  "asset.center": ["asset.inventory.read", "inventory"],
  "monitor.alert.unified": ["monitor.alert.read", "alerts"],
  "monitor.alert.db": ["monitor.capacity_alert.read", "alerts"],
  "monitor.observability": ["monitor.observability.read", "linkage_health"],
  "inspect.overview": ["inspection.run.query", "jobs"],
  "inspect.tasks": ["inspection.run.query", "jobs"],
  "perf.slowsql": ["performance.slow_sql.read", "read"],
  "perf.lock": ["performance.lock.read", "read"],
  "perf.baseline": ["performance.baseline.collect", "collect"],
  "perf.capacity": ["performance.capacity.collect", "collect"],
  "perf.awr.report": ["performance.awr.collect", "collect"],
  "ops.configGovernance": ["operations.config_governance.read", "read"],
  "ops.overview": ["operations.runtime_overview.read", "read"],
  "ops.log": ["operations.log.read", "read"],
  "ops.session": ["operations.session.read", "read"],
  "ops.space": ["operations.space.read", "read"],
  "ops.object": ["operations.object.read", "read"],
  "ops.security": ["operations.security.read", "read"],
  "report.unified": ["report.catalog.read", "read"],
  "report.alert": ["monitor.alert.read", "read"],
  "system.user": ["platform.user.read", "read"],
  "system.params": ["platform.settings.read", "read"],
  "system.ai": ["platform.ai_config.read", "read"],
  "system.schedule": ["platform.scheduler.read", "read"],
};
const SEMANTIC_GAPS = new Set([
  "inspect.reports", "inspect.compare", "perf.awr.compare", "rescue.reports", "rescue.approvals",
]);
const SERVER_CAPABILITIES = {
  "inspection.run.query": "inspection.run_list/v1",
  "inspection.template.read": "inspection.policy_list/v1",
  "operations.capability_matrix.read": "operations.capability_matrix",
  // C13 batch 1 live readers. The schema string must equal the registry's output_schema;
  // a mismatch is reported as server_contract_not_supported instead of executing.
  "operations.runtime_overview.read": "object",
  "operations.space.read": "object",
  "operations.log.read": "object",
  "operations.session.read": "object",
  "performance.lock.read": "engine_specific_response",
  "performance.slow_sql.read": "object",
  // C13 batch 2 platform readers. `protection.backup_center.read` and
  // `performance.overview.read` refuse a scoped caller; the dashboard applies its own scope.
  "platform.dashboard.read": "object",
  "performance.overview.read": "object",
  "operations.ha.read": "object",
  // C13 batch 3: scope-aware inventories and stored integration/recovery/catalogue reads.
  "asset.inventory.read": "object",
  "report.catalog.read": "object",
  "governance.object_storage.read": "object",
  // C13 batch 4: platform/integration configuration reads (admin actions, no target scope).
  "platform.settings.read": "object",
  // C13 batch 5: user directory, capacity alerts, backup platform registry and agent tools.
  "platform.user.read": "object",
  "monitor.capacity_alert.read": "object",
  // C13 batch 6: incidents, recovery tasks, automation registers and evaluation summary.
  "platform.ai_evaluation.read": "object",
  "diagnosis.incident.read": "object",
  // C13 batch 7: AI provider metadata and stored governance candidates.
  "platform.ai_config.read": "object",
  "governance.candidate.read": "object",
  // C13 batch 8: identity, scheduler, unified alerts and problem trend.
  "platform.identity.read": "object",
  "platform.scheduler.read": "object",
  "monitor.alert.read": "object",
  "performance.trend.read": "object",
  // C13 batch 9: snapshot agent/rule registers and the Grafana template catalogue.
  "diagnosis.snapshot_agent.read": "object",
  "diagnosis.snapshot_rule.read": "object",
  "monitor.grafana_template.read": "object",
  "operations.config_governance.read": "object",
  "diagnosis.investigation.read": "object",
  "monitor.log.read": "object",
  "monitor.apm_trace.read": "object",
  "operations.security.read": "object",
  "performance.awr.query": "object",
  "performance.capacity.collect": "capability.effect_result/v1",
  "performance.table_profile.collect": "capability.effect_result/v1",
  "performance.index.collect": "capability.effect_result/v1",
  "performance.workload.collect": "capability.effect_result/v1",
  "performance.sql_profile.collect": "capability.effect_result/v1",
  "governance.candidate.scan": "capability.effect_result/v1",
  "operations.startup.plan": "capability.effect_result/v1",
  "operations.shutdown.plan": "capability.effect_result/v1",
  "monitor.observability.bind": "capability.effect_result/v1",
  "monitor.alert.sync": "capability.effect_result/v1",
  "monitor.observability.read": "object",
  "monitor.apm_source.read": "object",
  "operations.object.read": "object",
  "operations.switchover.plan": "capability.effect_result/v1",
  "report.document.generate": "report.document_result/v1",
  // C14 effects: approval-gated collections (reached through a confirmed plan).
  "monitor.capacity.execute": "capability.effect_result/v1",
  "performance.baseline.collect": "capability.effect_result/v1",
  "diagnosis.root_cause.execute": "diagnosis.root_cause_result/v1",
};
// Effect contracts are persisted products rather than flexible read projections. Pin their
// declaration version so an older producer cannot be admitted under the current result schema.
const PINNED_SERVER_VERSIONS = {
  "monitor.alert.sync": 3,
  "monitor.capacity.execute": 3,
  "monitor.observability.bind": 3,
  "performance.workload.collect": 3,
  "performance.sql_profile.collect": 3,
  "performance.index.collect": 3,
  "performance.table_profile.collect": 3,
  "performance.baseline.collect": 3,
  "performance.capacity.collect": 3,
  "operations.startup.plan": 3,
  "operations.shutdown.plan": 3,
  "operations.switchover.plan": 3,
  "governance.candidate.scan": 3,
  "report.document.generate": 3,
  "diagnosis.root_cause.execute": 1,
};
const PREPARATION_REASONS = {
  authentication_required: "登录状态已失效，请重新登录后重试。",
  permission_denied: "当前账号没有读取能力目录的权限，未执行业务请求。",
  catalog_unavailable: "能力目录加载失败，暂时无法校验执行权限，请稍后重试。",
  invalid_capability_catalog: "能力目录响应格式异常，无法安全校验，未执行业务请求。",
  semantic_gap: "该报告、对比或审批功能尚未接入小助手，不能用任务列表或历史记录代替。",
  unknown_target: "该功能尚未接入小助手执行入口，未执行业务请求。",
  ambiguous_capability: "能力目录存在重复定义，无法确定安全的执行入口。",
  capability_not_authorized: "该能力未登记或当前账号无权使用，未执行业务请求。",
  stale_definition_version: "能力版本与当前支持的执行定义不一致，请检查版本后重试。",
  definition_not_synced: "能力定义尚未同步，当前不能执行。",
  definition_out_of_sync: "能力定义与登记信息不一致，当前不能执行。",
  capability_disabled: "该能力已停用，未执行业务请求。",
  capability_unavailable: "该能力当前不可用，未执行业务请求。",
  handler_not_wired: "该功能尚未接通执行处理器，未执行业务请求。",
  server_contract_not_supported: "服务端能力契约已变化，当前客户端无法安全创建清单。",
  engine_not_authorized: "当前数据库类型不在该能力的授权支持范围内。",
  engine_not_supported: "该能力尚不支持当前数据库类型。",
  scope_not_supported: "该能力尚不支持当前账号的资产范围限制，未执行业务请求。",
  target_engine_mismatch: "指令中的数据库类型与所选实例不一致，请重新选择实例。",
  missing_instance: "请先选择有权访问的数据库实例，再执行该功能。",
  binding_not_supported: "该指令分支与当前数据库类型没有匹配的能力接线，未执行业务请求。",
  missing_parameters: "该操作需要补充业务参数或变更确认，请打开对应功能完成确认。",
  plan_confirmation_required: "请先审查并确认执行清单，确认前不会读取业务数据。",
};
const INTENT_NOTICES = {
  "ops.object": "Oracle 对象查询实际会采集并保存表画像，不是纯只读查询。",
  "report.alert": "当前汇总告警数据，不会生成独立报告文件。",
  "report.unified": "当前读取统一报告汇总，不等同于新版报告制品目录。",
};
const TRACE_ID = /(?:trace\s*id|traceid|链路id)[\s:：=]*([a-zA-Z0-9._-]+)/i;

function actionBinding(request, engine) {
  const target = request?.targetKey;
  if (!Object.hasOwn(ACTION_BINDINGS, target)) return null;
  if (target === "report.unified" && commandHas(request, ["生成", "制作", "出具"])) {
    return ["report.document.generate", "generate"];
  }
  if (target === "diagnose.investigation" && commandHas(request, ["根因诊断", "根因分析", "端到端"])) {
    return ["diagnosis.root_cause.execute", "root_cause"];
  }
  if (target === "asset.center" && commandHas(request, ["自动识别", "识别", "发现", "同步", "刷新拓扑"])) {
    return ["asset.discovery.execute", "discovery"];
  }
  if (target === "monitor.alert.unified" && commandHas(request, ["同步", "导入", "刷新"])) return ["monitor.alert.sync", "import_alerts"];
  if (target === "monitor.alert.db" && commandHas(request, ["采集", "刷新"])) return ["monitor.capacity.execute", "collect"];
  if (target === "monitor.observability" && commandHas(request, ["自动绑定", "绑定"])) return ["monitor.observability.bind", "auto_bind"];
  if (target === "monitor.observability.logs" && commandHas(request, ["时间线", "故障链"])) return ["monitor.timeline.read", "fault_timeline"];
  if (target === "monitor.observability.apm" && TRACE_ID.test(request.command)) return ["monitor.apm_trace.read", "trace_id"];
  if (engine === "oracle") {
    if (target === "ops.overview") {
      if (commandHas(request, ["启动", "开库"])) return ["operations.startup.plan", "startup"];
      if (commandHas(request, ["关闭", "停库"])) return ["operations.shutdown.plan", "shutdown"];
    }
    if (target === "ops.ha" && commandHas(request, ["切换", "switchover"])) return ["operations.switchover.plan", "switchover"];
    if (target === "ops.object") return ["operations.object.read", "table_profile"];
  }
  if (target === "ops.governance") {
    if (["postgresql", "sqlserver"].includes(engine)) return ["governance.candidate.read", "governance_section"];
    if (commandHas(request, ["扫描", "识别", "分析"])) return ["governance.candidate.scan", "scan"];
  }
  if (["integration.zabbix", "integration.prometheus"].includes(target) && commandHas(request, ["同步", "采集", "刷新"])) {
    return [`${target}.sync`, "sync"];
  }
  return ACTION_BINDINGS[target];
}

export async function prepareAssistantAction(request, { instance } = {}) {
  request = { ...request, command: String(request?.command || "") };
  instance = instance ? { ...instance } : undefined;
  const target = request?.targetKey;
  const engine = String(instance?.db_type || request?.targetDbType || "").toLowerCase();
  const selection = actionBinding(request, engine);
  const preparation = (status, reason, capability) => ({
    status, reason, summary: Object.hasOwn(PREPARATION_REASONS, reason)
      ? PREPARATION_REASONS[reason] : PREPARATION_REASONS.capability_unavailable,
    ...(capability ? { capability } : {}),
  });
  const unsupported = (reason, capability) => preparation("unsupported", reason, capability);
  let catalog;
  try {
    catalog = await withTimeout(listAssistantCapabilities(), 90_000);
  } catch (error) {
    return unsupported(error?.status === 401 ? "authentication_required"
      : error?.status === 403 ? "permission_denied"
        : error?.code === "assistant_response_invalid" ? "invalid_capability_catalog" : "catalog_unavailable");
  }
  if (catalog?.schema !== "capability.registry/v1" || !Array.isArray(catalog.items)) return unsupported("invalid_capability_catalog");
  if (SEMANTIC_GAPS.has(target)) return unsupported("semantic_gap");
  if (!selection) return unsupported("unknown_target");
  const [capabilityId, branch] = selection;
  const candidates = catalog.items.filter((item) => item?.capability_id === capabilityId);
  if (candidates.length !== 1) return unsupported(candidates.length ? "ambiguous_capability" : "capability_not_authorized");
  const capability = candidates[0];
  const server = Object.hasOwn(SERVER_CAPABILITIES, capabilityId);
  // Client branches are audited for v1; server readers carry the current catalog version into a confirmed plan.
  if (!Number.isSafeInteger(capability.version) || capability.version < 1
      || capability.definition_version !== capability.version
      || (!server && capability.version !== 1)) return unsupported("stale_definition_version", capability);
  if (capability.enabled !== true) return unsupported(capability.unavailable_reason || "capability_disabled", capability);
  if (capability.availability !== "available" || capability.unavailable_reason) return unsupported(capability.unavailable_reason || "capability_unavailable", capability);
  if (capability.execution !== (server ? "server" : "client")
      || capability.handler_status !== (server ? "wired" : "client_bound")
      || (server && capability.plan_executable !== true)) return unsupported("handler_not_wired", capability);
  if (server && (capability.output_schema !== SERVER_CAPABILITIES[capabilityId]
      || (Object.hasOwn(PINNED_SERVER_VERSIONS, capabilityId)
        && capability.version !== PINNED_SERVER_VERSIONS[capabilityId]))) {
    return unsupported("server_contract_not_supported", capability);
  }
  if (!Array.isArray(capability.engines) || !Array.isArray(capability.authorized_engines)
      || !(engine ? capability.engines.includes(engine) && capability.authorized_engines.includes(engine)
        : capability.authorized_engines.some((value) => capability.engines.includes(value)))) return unsupported("engine_not_authorized", capability);
  if (instance?.db_type && request?.targetDbType && engine !== String(request.targetDbType).toLowerCase()) return unsupported("target_engine_mismatch", capability);
  if ((capability.requires_instance || INSTANCE_TARGETS.has(target))
      && (!Number.isSafeInteger(instance?.id) || instance.id <= 0 || !instance.db_type)) {
    return preparation("needs_input", "missing_instance", capability);
  }
  const bindings = (Array.isArray(capability.bindings) ? capability.bindings : []).filter((binding) =>
    binding?.target_key === target && binding.branch === branch && (binding.engine === undefined || binding.engine === engine));
  if (bindings.length !== 1) return unsupported("binding_not_supported", capability);
  const summary = (bindings[0].intent_gap && INTENT_NOTICES[target]) || TARGET_TITLES[target];
  if (request?.requiresParameters || needsParameterEditing(normalizeText(request?.command))) return preparation("needs_input", "missing_parameters", capability);
  if (server) return preparation("needs_input", "plan_confirmation_required", capability);
  return { status: "ready", capability, summary };
}

function defaultBackupType(engine, restore = false) {
  if (restore) return "restore";
  return {
    oracle: "rman_full",
    mysql: "mysqldump_full",
    postgresql: "pg_dump_full",
    sqlserver: "full",
  }[engine] || "full";
}

function opsSection(targetKey, engine) {
  const capability = targetKey.slice("ops.".length);
  if (engine === "postgresql") {
    return {
      overview: "overview",
      log: "wal",
      session: "sessionTransaction",
      space: "space",
      object: "objectStats",
      ha: "ha",
      governance: "governance",
    }[capability];
  }
  return {
    overview: "overview",
    log: "logs",
    session: "sessionTransaction",
    space: "space",
    object: "objectStats",
    ha: "ha",
    governance: "governance",
  }[capability];
}

async function executeOps(request, instance) {
  if (request.targetKey === "ops.configGovernance") {
    return result(
      await Promise.all([
        configGovernanceApi.summary(),
        configGovernanceApi.baselines({ status: "active" }),
        configGovernanceApi.drifts({ status: "active", limit: 100 }),
      ]),
      "配置基线、采集状态与活动漂移检查完成",
    );
  }
  const engine = String(instance.db_type || "").toLowerCase();
  const id = instance.id;
  if (engine === "postgresql" || engine === "sqlserver") {
    const section = opsSection(request.targetKey, engine);
    if (!section) throw new Error(`${engine.toUpperCase()} 暂不支持该运维能力`);
    const data = await genericOpsApi.section(engine, id, section);
    return result(data, `${request.targetName}采集完成`);
  }

  if (engine === "mysql") {
    const endpoint = {
      "ops.overview": "/basic-info",
      "ops.log": "/binlog/overview",
      "ops.session": "/locks/transactions",
      "ops.space": "/capacity/schemas",
      "ops.object": "/tables-indexes/overview",
      "ops.security": "/users",
    }[request.targetKey];
    if (request.targetKey === "ops.governance") {
      const data = commandHas(request, ["扫描", "识别", "分析"])
        ? await scanGovernanceCandidates(id, 1_000_000)
        : await getGovernanceBundle(id);
      return result(data, "MySQL 数据治理分析完成");
    }
    if (!endpoint) throw new Error("MySQL 暂不支持该运维能力");
    return result(await mysqlOpsApi.section(id, endpoint), `${request.targetName}采集完成`);
  }

  if (engine !== "oracle") throw new Error(`暂不支持 ${(engine || "当前数据库").toUpperCase()}`);
  if (request.targetKey === "ops.overview") {
    if (commandHas(request, ["启动", "开库"])) {
      return result(
        await oracleOpsApi.startupScript(id, "open"),
        "Oracle 启动方案已生成",
        { notice: "仅生成可审查脚本，未直接启动数据库。" },
      );
    }
    if (commandHas(request, ["关闭", "停库"])) {
      return result(
        await oracleOpsApi.shutdownScript(id, "immediate"),
        "Oracle 关闭方案已生成",
        { notice: "仅生成可审查脚本，未直接关闭数据库。" },
      );
    }
    return result(await oracleOpsApi.basicInfo(id), "Oracle 运行概览采集完成");
  }
  if (request.targetKey === "ops.log") {
    return result(
      await Promise.all([oracleOpsApi.archiveConfig(id), oracleOpsApi.archiveSpaceDetail(id)]),
      "Oracle 归档与 FRA 状态采集完成",
    );
  }
  if (request.targetKey === "ops.session") {
    return result(
      await Promise.all([oracleOpsApi.lockedObjects(id), oracleOpsApi.blockingTree(id)]),
      "Oracle 会话、锁和阻塞链分析完成",
    );
  }
  if (request.targetKey === "ops.space") {
    return result(await oracleOpsApi.tablespaces(id), "Oracle 表空间使用情况采集完成");
  }
  if (request.targetKey === "ops.object") {
    return result(await collectTableProfile(id), "Oracle 对象与表画像采集完成");
  }
  if (request.targetKey === "ops.security") {
    return result(
      await Promise.all([oracleOpsApi.users(id), oracleOpsApi.roles(id)]),
      "Oracle 用户与角色检查完成",
    );
  }
  if (request.targetKey === "ops.ha") {
    if (commandHas(request, ["切换", "switchover"])) {
      return result(
        await oracleOpsApi.dataGuardSwitchoverCheckPlan(id),
        "Data Guard 切换检查方案已生成",
        { notice: "仅执行切换前检查并生成方案，未直接切换主备。" },
      );
    }
    return result(await oracleOpsApi.dataGuardOverview(id), "Oracle Data Guard 状态检查完成");
  }
  if (request.targetKey === "ops.governance") {
    const data = commandHas(request, ["扫描", "识别", "分析"])
      ? await scanGovernanceCandidates(id, 1_000_000)
      : await getGovernanceBundle(id);
    return result(data, "Oracle 数据治理分析完成");
  }
  throw new Error("Oracle 暂不支持该运维能力");
}

async function executePerformance(request, instance) {
  const id = instance.id;
  const engine = String(instance.db_type || "").toLowerCase();
  switch (request.targetKey) {
    case "perf.workload":
      return result(await collectWorkloadReport(id, {
        source: "auto",
        window_minutes: 60,
        row_limit: 20,
        include_sql_text: false,
        sensitive_data_approved: false,
        oracle_diagnostics_pack_confirmed: false,
      }), "引擎深度报告采集完成");
    case "perf.sql":
      return result(await collectSqlProfile(id), "SQL 画像采集完成");
    case "perf.slowsql":
      if (engine === "oracle") {
        const data = await getOracleSlowSqlRealtime(id, { category: "top_avg", hours: 1 });
        const snapshot = slowSqlSnapshot(data, id, { category: "top_avg", hours: 1 });
        const conclusion = slowSqlConclusion(snapshot);
        return result(data, conclusion.lines.join(" "), {
          notice: `${conclusion.scope} ${conclusion.limitation}`,
          executionResult: { engine, instanceId: id, slowSql: snapshot },
        });
      }
      if (engine === "mysql") {
        const data = await mysqlOpsApi.slowSqlTop(id, { time_range: "24h", limit: 200 });
        return result(data, "MySQL 慢 SQL 查询已返回", { executionResult: { engine, instanceId: id, data } });
      }
      throw new Error(`${engine.toUpperCase()} 尚未接入慢 SQL 分析，未生成报告`);
    case "perf.lock":
      if (engine === "oracle") {
        return result(
          await Promise.all([oracleOpsApi.lockedObjects(id), oracleOpsApi.blockingTree(id)]),
          "Oracle 锁等待与阻塞链分析完成",
        );
      }
      if (engine === "mysql") return result(await mysqlOpsApi.lockTransactions(id), "MySQL 锁等待与阻塞链分析完成");
      return result(await genericOpsApi.section(engine, id, "sessionTransaction"), `${engine.toUpperCase()} 锁等待分析完成`);
    case "perf.index":
      return result(await collectIndexFindings(id), "索引发现采集完成");
    case "perf.table":
      return result(await collectTableProfile(id), "表画像采集完成");
    case "perf.optimization":
      return result(await refreshPerformanceSuggestions(id), "优化建议刷新完成");
    case "perf.baseline":
      return result(await collectPerformanceBaseline(id), "性能基线采集完成");
    case "perf.trend":
      return result(await getProblemTrend({ instance_id: id }), "问题趋势分析完成");
    case "perf.capacity":
      return result(await capacityApi.collect(id), "容量快照采集完成");
    case "perf.awr.report":
    case "perf.awr.collect":
      if (engine !== "oracle") throw new Error("AWR 仅支持 Oracle 实例");
      return result(await collectAwrNow(id), "AWR 采集完成");
    case "perf.awr.history":
      if (engine !== "oracle") throw new Error("AWR 仅支持 Oracle 实例");
      return result(await getAwrReports(id), "AWR 历史报告加载完成");
    default:
      throw new Error("该性能功能尚未接入直接执行");
  }
}

async function executeIntegration(request, instance) {
  if (request.targetKey === "integration.zabbix") {
    const bundle = await getZabbixBundle();
    if (!commandHas(request, ["同步", "采集", "刷新"])) return result(bundle, "Zabbix 配置、主机与问题加载完成");
    const config = bundle?.[0]?.items?.find((item) => ![false, 0, "0"].includes(item.enabled))
      || bundle?.[0]?.items?.[0];
    if (!config?.id) throw new Error("没有可用的 Zabbix 配置");
    const synced = await Promise.all([syncZabbixHosts(config.id), syncZabbixProblems(config.id)]);
    return result(synced, "Zabbix 主机与问题同步完成");
  }
  if (request.targetKey === "integration.prometheus") {
    const bundle = await getPrometheusBundle();
    if (!commandHas(request, ["同步", "采集", "刷新"])) return result(bundle, "Prometheus 配置与告警加载完成");
    const sources = await listObservabilitySources();
    const source = (sources.items || sources || []).find((item) => item.source_type === "prometheus" && ![false, 0, "0"].includes(item.enabled));
    if (!source?.id) throw new Error("没有可用的 Prometheus 数据源");
    return result(await syncPrometheusStandardMetrics(source.id), "Prometheus 标准指标同步完成");
  }
  if (request.targetKey === "integration.notify") return result(await getNotifyBundle(), "通知渠道与发送日志检查完成");
  if (request.targetKey === "integration.itsm") return result(await getItsmBundle(), "ITSM 配置与工单状态检查完成");
  throw new Error("该集成功能尚未接入直接执行");
}

export async function executeAssistantActionRequest(request, { instance } = {}) {
  request = { ...request, command: String(request?.command || "") };
  instance = instance ? { ...instance } : undefined;
  const prepared = await prepareAssistantAction(request, { instance });
  if (prepared.status !== "ready") {
    return result({ status: prepared.status, code: prepared.reason, message: prepared.summary }, prepared.summary);
  }
  const response = await executeLegacyAssistantAction(request, { instance });
  if (prepared.summary && prepared.summary !== TARGET_TITLES[request.targetKey]) {
    response.notice = [response.notice, prepared.summary].filter(Boolean).join(" ");
  }
  return response;
}

async function executeLegacyAssistantAction(request, { instance } = {}) {
  const id = instance?.id;
  const engine = String(instance?.db_type || request.targetDbType || "").toLowerCase();

  if (request.targetKey.startsWith("ops.") && request.targetKey !== "ops.governance.objectStorage") {
    return executeOps(request, instance);
  }
  if (request.targetKey.startsWith("perf.") && request.targetKey !== "perf.overview") {
    return executePerformance(request, instance);
  }
  if (request.targetKey.startsWith("integration.")) {
    return executeIntegration(request, instance);
  }

  switch (request.targetKey) {
    case "home.dashboard":
      return result(await systemApi.dashboardStats(), "平台运行总览加载完成");
    case "asset.center": {
      if (commandHas(request, ["自动识别", "识别", "发现", "同步", "刷新拓扑"])) {
        const data = await applyAssetDiscovery({ probe_live: true });
        const normalized = data.normalized || {};
        return result(data, "资产自动识别完成", {
          metrics: [
            { label: "主机", value: normalized.host_count || 0 },
            { label: "集群", value: normalized.cluster_count || 0 },
            { label: "实例", value: normalized.instance_count || 0 },
            { label: "采集问题", value: data.warning_count || 0 },
          ],
        });
      }
      return result(
        await Promise.all([getAssetOverview(), getAssetTree(), listCmdbSources()]),
        "内部 CMDB、资产树与外部来源加载完成",
      );
    }
    case "diagnose.incidents":
      return result(await listIncidents({ limit: 100 }), "事故、快照状态与根因候选汇总完成");
    case "diagnose.investigation":
      return result(
        await Promise.all([
          listStateTimeline({ limit: 200 }),
          listFaultPatternMatches({ limit: 100 }),
          listSceneCaptures({ limit: 200 }),
          listProbeLeases({ limit: 200 }),
          listKernelCapabilities(),
        ]),
        "状态转换、故障模式、现场采集、探针和内核能力检查完成",
      );
    case "diagnose.snapshot.agents":
      return result(await listIncidentAgents({ limit: 200 }), "现场 Agent、心跳与缓存状态检查完成");
    case "diagnose.snapshot.policies":
      return result(await listIncidentTriggerRules(), "本地触发规则与生效版本检查完成");
    case "monitor.alert.unified": {
      if (commandHas(request, ["同步", "导入", "刷新"])) {
        return result(
          await Promise.all([importCapacityAlerts(), importExternalAlerts()]),
          "容量告警和外部告警同步完成",
        );
      }
      return result(await getUnifiedAlertBundle(), "综合告警汇总完成");
    }
    case "monitor.alert.db":
      return result(
        commandHas(request, ["采集", "刷新"])
          ? await capacityApi.collect(id)
          : await getCapacityAlertBundle({ status: "all", instanceId: id }),
        "容量告警分析完成",
      );
    case "monitor.observability":
      if (commandHas(request, ["自动绑定", "绑定"])) {
        return result(await autoBindObservability(), "可观测数据源自动绑定完成");
      }
      return result(
        await getObservabilityLinkageHealth({ instanceId: id, probeSources: true, probeTimeoutSeconds: 3 }),
        "数据库、Zabbix、Prometheus 与 Alertmanager 联动检查完成",
      );
    case "monitor.observability.logs":
      if (commandHas(request, ["时间线", "故障链"])) {
        return result(
          await buildObservabilityFaultTimeline({ instance_id: id, query: request.command, time_range: "1h" }),
          "故障时间线生成完成",
        );
      }
      return result(
        await queryObservabilityLogs({ instance_id: id, query: request.command, time_range: "1h", limit: 100 }),
        "最近一小时关联日志查询完成",
      );
    case "monitor.observability.grafana":
      return result(await listGrafanaTemplates(), "Grafana 模板加载完成");
    case "monitor.observability.apm": {
      const traceId = request.command.match(TRACE_ID)?.[1];
      if (!traceId) {
        return result(await getObservabilityBundle(), "APM 数据源状态检查完成", {
          notice: "如需查询具体链路，请在指令中附带 Trace ID。",
        });
      }
      return result(await queryApmTrace({ trace_id: traceId, instance_id: id }), `Trace ${traceId} 查询完成`);
    }
    case "inspect.reports":
    case "inspect.compare":
      return result({ status: "unsupported", code: "semantic_gap" }, "巡检报告与对比尚未接入");
    case "perf.overview":
      return result(await getPerformanceOverview(), "性能总览采集完成");
    case "backup.overview":
    case "backup.report":
      return result(await getBackupCenterBundle({ force: true }), "备份状态、目录与报告加载完成");
    case "backup.policy":
      return result(await listBackupPolicies(), "备份策略、调度与保护目标加载完成");
    case "backup.plan":
    case "backup.restore": {
      const restore = request.targetKey === "backup.restore";
      const data = await buildBackupCenterPlan({
        engine,
        instance_id: id,
        plan_type: defaultBackupType(engine, restore),
        storage_type: "local",
        backup_dir: engine === "sqlserver" ? "D:\\backup" : `/backup/${engine}`,
      });
      return result(data, restore ? "安全恢复演练方案已生成" : "备份方案已生成", {
        notice: "已生成可审查方案，未绕过确认直接在服务器执行。",
      });
    }
    case "backup.platform":
      return result(
        await Promise.all([
          listBackupPlatformConfigs(),
          listBackupPlatformRuns(),
          getBackupPlatformHealth(id),
          listBackupRestoreDrills(id),
        ]),
        "备份平台配置、作业和恢复演练状态检查完成",
      );
    case "rescue.overview":
      return result(
        await Promise.all([recoveryApi.engines(), recoveryApi.agents(), recoveryApi.tasks({ limit: 100 })]),
        "数据抢救能力、Agent 与任务加载完成",
      );
    case "rescue.cases":
      return result(
        await Promise.all([recoveryApi.cases({ limit: 100 }), recoveryApi.sourceImages({ limit: 100 })]),
        "抢救案件、源镜像与证据链加载完成",
      );
    case "rescue.tools":
      return result(
        await Promise.all([recoveryApi.agentTools(), recoveryApi.toolAuthorizations()]),
        "抢救工具清单与授权状态检查完成",
      );
    case "rescue.agents":
      return result(await recoveryApi.agents(), "远程 Agent 状态检查完成");
    case "rescue.tasks":
      return result(await recoveryApi.tasks({ limit: 100 }), "抢救任务加载完成");
    case "rescue.reports":
    case "rescue.approvals":
      return result({ status: "unsupported", code: "semantic_gap" }, "抢救报告与审批尚未接入");
    case "protection.automation":
      return result(
        await Promise.all([listAutomationPlans(), listAutomationApprovals(), listAutomationRuns()]),
        "自动化方案、审批与执行记录加载完成",
      );
    case "report.unified":
      return result(
        await Promise.all([getUnifiedReportStats(), getUnifiedReports("all", 200)]),
        "统一报告汇总完成",
      );
    case "report.alert":
      return result(await getUnifiedAlertBundle(), "告警报告数据汇总完成");
    case "system.user":
      return result(await getUserManagementBundle(), "用户、模块与密码策略加载完成");
    case "system.identity":
      return result(
        await Promise.all([listIdentityProviders(), listIdentityLoginAudit()]),
        "企业身份源与登录审计检查完成",
      );
    case "system.params":
      return result(await settingsApi.list(), "系统参数加载完成");
    case "system.ai":
      return result(await aiConfigApi.list(), "AI Provider 与模型配置检查完成");
    case "system.aiEvaluation":
      return result(await diagnosisEvaluationApi.summary(), "诊断金标集、准确率与质量门禁检查完成");
    case "system.schedule":
      return result(
        await Promise.all([getSchedulerStatus(), getSchedulerTasks(), getAwrCollectPolicies()]),
        "调度器、任务和 AWR 采集策略检查完成",
      );
    case "ops.governance.objectStorage":
      return result(
        await Promise.all([listObjectStorageConfigs(), listArchiveLifecycleRequests()]),
        "对象存储配置与生命周期请求检查完成",
      );
    default:
      return result({ status: "unsupported", code: "unknown_target" }, "该功能尚未接入直接执行");
  }
}
