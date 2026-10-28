const lifecycle = {
  draft: ["草稿", "default"],
  validated: ["待确认执行", "default"],
  awaiting_approval: ["等待审批", "warning"],
  approved: ["已审批，尚未执行", "default"],
  running: ["运行中", "processing"],
};
const outcomes = {
  succeeded: ["计划成功", "success", "计划已结束；读取成功不代表巡检成功、数据库健康或已生成新报告。"],
  partial: ["部分成功", "warning", "部分步骤未成功，仅展示已保存的读取结果。"],
  failed: ["执行失败", "error", "计划执行失败，不推断缺失结果。"],
  cancelled: ["已取消", "default", "计划已在运行前取消。"],
  expired: ["已过期", "warning", "计划已过期，不能视为执行成功。"],
  unknown: ["结果未知", "warning", "结果无法确认，不推断成功或补造结果。"],
  plan_only: ["仅生成计划", "default", "仅生成计划，未执行实际操作。"],
  blocked: ["已阻止", "warning", "计划被阻止，未执行实际操作。"],
  unsupported: ["暂不支持", "warning", "该计划尚不支持执行。"],
  needs_input: ["待补充参数", "warning", "缺少必要参数；计划已保存，但没有执行。"],
};
const stepLabels = {
  pending: ["待执行", "default"], running: ["运行中", "processing"],
  succeeded: ["读取成功", "success"], failed: ["失败", "error"],
  partial: ["部分完成", "warning"],
  skipped: ["已跳过", "default"], cancelled: ["已取消", "default"],
  expired: ["已过期", "warning"], unknown: ["结果未知", "warning"],
};
const readSchemas = {
  "inspection.run.query": "inspection.run_list/v1",
  "inspection.template.read": "inspection.policy_list/v1",
  "operations.capability_matrix.read": "operations.capability_matrix",
};
const genericReadSchemas = new Set(["object", "engine_specific_response"]);
const commonEffects = new Map([
  ["monitor.alert.sync", false], ["monitor.capacity.execute", true], ["monitor.observability.bind", false],
  ["performance.workload.collect", true], ["performance.sql_profile.collect", true], ["performance.index.collect", true],
  ["performance.table_profile.collect", true], ["performance.baseline.collect", true], ["performance.capacity.collect", true],
  ["operations.startup.plan", true], ["operations.shutdown.plan", true], ["operations.switchover.plan", true],
  ["governance.candidate.scan", true], ["protection.backup.plan", true], ["protection.restore.plan", true],
  ["integration.zabbix.sync", false], ["integration.prometheus.sync", false],
]);
const planOnlyEffects = new Set(["operations.startup.plan", "operations.shutdown.plan", "operations.switchover.plan",
  "protection.backup.plan", "protection.restore.plan"]);
const terminalSteps = new Set(["succeeded", "partial", "failed", "skipped", "cancelled", "expired", "unknown"]);
const executedOutcomes = new Set(["succeeded", "partial", "failed", "cancelled", "expired", "unknown"]);
const planPattern = /^pl[a-f0-9]{26}$/;
const own = (value, key) => Object.hasOwn(value, key);
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value, max = 200) => typeof value === "string" ? value.slice(0, max) : "";
const saved = (plan) => record(plan) && typeof plan.plan_id === "string" && plan.plan_id.length === 28 && planPattern.test(plan.plan_id);
const stepsOf = (plan) => Array.isArray(plan?.steps) ? plan.steps.slice(0, 20).filter(record) : [];

// Stable editor keys keep dependency edges attached to their steps while reordering.
export function changeAssistantSteps(steps, action) {
  if (!Array.isArray(steps) || steps.length < 1 || steps.length > 20) throw new TypeError("Invalid draft steps");
  let next = steps.map((step) => ({ ...step, params: { ...step.params }, depends_on: [...step.depends_on] }));
  const index = next.findIndex((step) => step.key === action.key);
  if (action.type === "add") {
    if (next.length >= 20 || next.some((step) => step.key === action.step.key)) throw new TypeError("Step limit reached");
    next.push({ ...action.step, params: { ...action.step.params }, depends_on: [] });
  } else if (action.type === "remove") {
    if (index < 0 || next.length === 1) throw new TypeError("Invalid step removal");
    next = next.filter((step) => step.key !== action.key).map((step) => ({ ...step, depends_on: step.depends_on.filter((key) => key !== action.key) }));
  } else if (action.type === "move") {
    const destination = index + action.direction;
    if (index < 0 || ![-1, 1].includes(action.direction) || destination < 0 || destination >= next.length) throw new TypeError("Invalid step move");
    [next[index], next[destination]] = [next[destination], next[index]];
  } else throw new TypeError("Invalid step action");
  if (!next.every((step, i) => step.depends_on.every((key) => next.slice(0, i).some((prior) => prior.key === key)))) {
    throw new TypeError("请先解除依赖，再调整步骤顺序");
  }
  return next;
}

export function assistantPlanDraft(plan) {
  if (!saved(plan) || !Array.isArray(plan.steps) || plan.steps.length < 1 || plan.steps.length > 20
    || !plan.steps.every((step, index) => record(step) && step.seq === index + 1 && own(readSchemas, step.capability_id)
      && step.definition_version === 1 && record(step.params) && record(step.input_refs)
      && Object.keys(step.input_refs).length === 0 && Array.isArray(step.depends_on)
      && step.depends_on.every((seq) => Number.isInteger(seq) && seq >= 1 && seq < step.seq)
      && Object.entries(step.params).every(([key, value]) => step.capability_id !== "operations.capability_matrix.read"
        && ["page", "page_size"].includes(key) && Number.isInteger(value) && value >= 1 && value <= (key === "page" ? 1000000 : 100)))) {
    throw new TypeError("冻结清单含未支持的参数或字段引用，不能在此编辑");
  }
  return plan.steps.map((step) => ({ key: `saved-${step.seq}`, capability_id: step.capability_id,
    params: { ...step.params }, depends_on: step.depends_on.map((seq) => `saved-${seq}`) }));
}

function readableResult(step) {
  const ref = step.result_ref;
  const matches = Number.isInteger(step.seq) && step.seq >= 1 && step.seq <= 20
    && step.status === "succeeded" && typeof step.capability_id === "string" && own(readSchemas, step.capability_id)
    && record(ref) && ref.kind === "capability_read" && ref.capability_id === step.capability_id
    && ref.schema === readSchemas[step.capability_id] && record(ref.inline)
    && Array.isArray(ref.inline.items) && ref.inline.items.every(record);
  if (!matches) return null;
  const value = ref.inline;
  if (step.capability_id === "operations.capability_matrix.read") {
    return Array.isArray(value.instances) && Array.isArray(value.engines) && record(value.summary) ? value : null;
  }
  return value.schema === ref.schema && Number.isInteger(value.total) && value.total >= 0
    && Number.isInteger(value.page) && value.page > 0
    && Number.isInteger(value.page_size) && value.page_size > 0 ? value : null;
}

function genericReadableResult(step) {
  const ref = step.result_ref;
  return Number.isInteger(step.seq) && step.seq >= 1 && step.seq <= 20
    && step.status === "succeeded" && typeof step.capability_id === "string"
    && /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}$/.test(step.capability_id)
    && record(ref) && ref.kind === "capability_read" && ref.capability_id === step.capability_id
    && genericReadSchemas.has(ref.schema) && record(ref.inline)
    && (!own(ref.inline, "schema") || ref.inline.schema === ref.schema) ? ref.inline : null;
}

function storedReadResult(step) {
  return readableResult(step) || genericReadableResult(step);
}

// The saved adapter envelope is a locator, not a report or a claim about database health.
// Returns its validated inline fields plus step_status, or null. report.available is only
// the artifact-index state; source-report availability depends on published result rows.
export function inspectionPlanResult(plan, step) {
  if (!saved(plan) || plan.status !== "settled" || !executedOutcomes.has(plan.outcome)
    || !Number.isSafeInteger(plan.instance_id) || plan.instance_id <= 0 || !record(step)
    || !Number.isInteger(step.seq) || step.seq < 1 || step.seq > 20
    || step.capability_id !== "inspection.run.execute" || step.definition_version !== 2) return null;
  const ref = step.result_ref;
  if (!record(ref) || ref.kind !== "inspection_job" || ref.capability_id !== step.capability_id
    || ref.schema !== "inspection.plan_result/v1" || !record(ref.inline)) return null;
  const value = ref.inline, run = value.run;
  if (value.schema !== ref.schema || typeof value.job_id !== "string" || value.job_id.length !== 30
    || !/^[a-f0-9]{30}$/.test(value.job_id) || value.instance_id !== plan.instance_id || !record(run)
    || run.job_id !== value.job_id || run.instance_id !== value.instance_id
    || typeof plan.db_type !== "string" || !plan.db_type || run.db_type !== plan.db_type
    || run.status !== value.status || run.outcome !== value.outcome || run.terminal !== true
    || !Number.isSafeInteger(run.result_count) || run.result_count < 0
    || typeof run.results_published !== "boolean"
    || run.results_published !== (run.stage === "completed" && run.result_count > 0)
    || typeof run.finished_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(run.finished_at)
    || !Number.isFinite(Date.parse(run.finished_at))
    || !(value.error_code === null || (typeof value.error_code === "string" && /^[a-z][a-z0-9_]{0,127}$/.test(value.error_code)))
    || !record(value.report) || typeof value.report.available !== "boolean") return null;
  const terminalOutcome = { success: "success", partial: "partial", failed: "failed", cancelled: "cancelled",
    expired: "expired", timed_out: "unknown", interrupted: "unknown" };
  if (!own(terminalOutcome, value.status) || value.outcome !== terminalOutcome[value.status]
    || (value.status === "failed" ? !["failed", "completed"].includes(run.stage)
      : run.stage !== (["success", "partial"].includes(value.status) ? "completed" : value.status))) return null;
  const forced = value.status === "cancelled" && value.error_code === "cancelled_force_closed";
  const expected = forced ? "unknown" : value.outcome === "success" ? "succeeded" : value.outcome;
  if (step.status !== expected || (forced && run.outcome_kind !== value.error_code)) return null;
  if (["success", "partial"].includes(value.outcome)
    && (run.stage !== "completed" || run.result_count === 0 || !run.results_published)) return null;
  if (value.status === "cancelled" && (run.stage !== "cancelled" || run.result_count !== 0 || run.results_published)) return null;
  return { schema: value.schema, job_id: value.job_id, instance_id: value.instance_id,
    status: value.status, outcome: value.outcome, step_status: step.status,
    error_code: value.error_code, run, report: value.report };
}

export function rootCausePlanResult(plan, step) {
  if (!saved(plan) || plan.status !== "settled" || !executedOutcomes.has(plan.outcome)
    || !Number.isSafeInteger(plan.instance_id) || plan.instance_id <= 0 || !record(step)
    || !Number.isInteger(step.seq) || step.seq < 1 || step.seq > 20
    || step.capability_id !== "diagnosis.root_cause.execute" || step.definition_version !== 1) return null;
  const ref = step.result_ref;
  if (!record(ref) || ref.kind !== "capability_effect" || ref.capability_id !== step.capability_id
    || ref.schema !== "diagnosis.root_cause_result/v1" || !record(ref.inline)) return null;
  const value = ref.inline, sampling = value.sampling;
  const expectedStatus = { success: "succeeded", partial: "partial", failed: "failed" };
  if (value.schema !== ref.schema || !own(expectedStatus, value.outcome)
    || step.status !== expectedStatus[value.outcome] || value.instance_id !== plan.instance_id
    || !record(sampling) || typeof sampling.requested_time_window !== "string"
    || typeof sampling.impact_scope !== "string" || sampling.collection_mode !== "current_snapshot"
    || sampling.historical_replay_performed !== false) return null;
  if (!Number.isSafeInteger(value.incident_id) || value.incident_id <= 0
      || typeof value.incident_no !== "string" || !value.incident_no || value.incident_no.length > 40
      || typeof value.status !== "string" || !value.status || value.status.length > 32
      || typeof value.severity !== "string" || value.severity.length > 32
      || !(value.root_cause_summary === null || (typeof value.root_cause_summary === "string" && value.root_cause_summary.length <= 4000))
      || !(value.primary_layer === null || (typeof value.primary_layer === "string" && value.primary_layer.length <= 64))
      || !Number.isSafeInteger(value.evidence_count) || value.evidence_count < 0
      || !Number.isSafeInteger(value.root_cause_count) || value.root_cause_count < 0
      || !["execution_status", "data_quality", "conclusion_status"].every((field) => typeof value[field] === "string" && value[field].length <= 64)
      || (value.outcome === "partial" && !["collection_incomplete", "no_supported_root_cause"].includes(value.reason_code))
      || (value.outcome === "failed" && value.error_code !== "diagnosis_failed")) return null;
  return { schema: value.schema, outcome: value.outcome, step_status: step.status,
    instance_id: value.instance_id, incident_id: value.incident_id ?? null, incident_no: text(value.incident_no, 40) || null,
    status: text(value.status, 32) || null, severity: text(value.severity, 32) || null,
    root_cause_summary: text(value.root_cause_summary, 4000) || null, primary_layer: text(value.primary_layer, 64) || null,
    evidence_count: value.evidence_count ?? 0, root_cause_count: value.root_cause_count ?? 0,
    execution_status: text(value.execution_status, 64) || null, data_quality: text(value.data_quality, 64) || null,
    conclusion_status: text(value.conclusion_status, 64) || null,
    sampling: { requested_time_window: text(sampling.requested_time_window, 200),
      impact_scope: text(sampling.impact_scope, 200), collection_mode: sampling.collection_mode,
      historical_replay_performed: false } };
}

export function reportDocumentPlanResult(plan, step) {
  if (!saved(plan) || plan.status !== "settled" || !["succeeded", "partial"].includes(plan.outcome)
    || !Number.isSafeInteger(plan.instance_id) || plan.instance_id <= 0 || !record(step)
    || !Number.isInteger(step.seq) || step.seq < 1 || step.seq > 20
    || step.capability_id !== "report.document.generate" || step.definition_version !== 3) return null;
  const ref = step.result_ref;
  if (!record(ref) || ref.kind !== "capability_effect" || ref.capability_id !== step.capability_id
    || ref.schema !== "report.document_result/v1" || !record(ref.inline)) return null;
  const value = ref.inline;
  const params = record(step.params) ? step.params : {};
  const expectedReportType = params.report_type ?? "unified";
  const expectedFormat = params.format ?? "markdown";
  const expectedWindowHours = params.window_hours ?? 168;
  const expectedIncludeAi = params.include_ai ?? true;
  const expectedStatus = { success: "succeeded", partial: "partial" };
  if (value.schema !== ref.schema || !own(expectedStatus, value.outcome)
    || step.status !== expectedStatus[value.outcome] || value.instance_id !== plan.instance_id
    || !["unified", "after_sales"].includes(value.report_type) || value.report_type !== expectedReportType
    || !["markdown", "html"].includes(value.format) || value.format !== expectedFormat
    || !Number.isSafeInteger(value.window_hours) || value.window_hours < 1 || value.window_hours > 720
    || value.window_hours !== expectedWindowHours || typeof value.include_ai !== "boolean"
    || value.include_ai !== expectedIncludeAi
    || !Number.isSafeInteger(value.evidence_count) || value.evidence_count < 0) return null;
  if (value.outcome === "partial") {
    if (value.reason_code !== "no_evidence" || value.evidence_count !== 0
      || value.artifact_id !== null || value.artifact_uid !== null || value.sha256 !== null
      || value.download_url !== null) return null;
    return { schema: value.schema, outcome: value.outcome, step_status: step.status,
      instance_id: value.instance_id, report_type: value.report_type, format: value.format,
      window_hours: value.window_hours, include_ai: value.include_ai, evidence_count: 0,
      reason_code: value.reason_code, artifact_id: null, artifact_uid: null, sha256: null,
      analysis_source: null, model: null, generated_at: null, download_url: null };
  }
  if (!Number.isSafeInteger(value.artifact_id) || value.artifact_id <= 0
    || typeof value.artifact_uid !== "string" || !/^[a-f0-9]{32}$/.test(value.artifact_uid)
    || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)
    || !["rule_engine", "ai"].includes(value.analysis_source)
    || !(value.model === null || (typeof value.model === "string" && value.model.length <= 200))
    || typeof value.generated_at !== "string" || value.generated_at.length > 40
    || !Number.isFinite(Date.parse(value.generated_at))
    || value.download_url !== `/api/v1/reports/catalog/${value.artifact_id}/download`) return null;
  return { schema: value.schema, outcome: value.outcome, step_status: step.status,
    instance_id: value.instance_id, report_type: value.report_type, format: value.format,
    window_hours: value.window_hours, include_ai: value.include_ai, evidence_count: value.evidence_count,
    artifact_id: value.artifact_id, artifact_uid: value.artifact_uid, sha256: value.sha256,
    analysis_source: value.analysis_source, model: text(value.model, 200) || null,
    generated_at: value.generated_at, download_url: value.download_url };
}

function commonEffectPlanResult(plan, step) {
  if (!saved(plan) || plan.status !== "settled" || !executedOutcomes.has(plan.outcome)
    || !Number.isSafeInteger(plan.revision) || plan.revision <= 0 || !record(step)
    || !Number.isInteger(step.seq) || step.seq < 1 || step.seq > 20
    || !commonEffects.has(step.capability_id) || step.definition_version !== 3) return null;
  const requiresInstance = commonEffects.get(step.capability_id);
  if (requiresInstance ? (!Number.isSafeInteger(plan.instance_id) || plan.instance_id <= 0) : plan.instance_id !== null) return null;
  const ref = step.result_ref;
  const refKeys = ["kind", "capability_id", "schema", "inline"];
  const inlineKeys = ["schema", "capability_id", "definition_version", "instance_id", "execution_id", "outcome", "data"];
  if (!record(ref) || Object.keys(ref).length !== refKeys.length || !refKeys.every((key) => own(ref, key))
    || ref.kind !== "capability_effect" || ref.capability_id !== step.capability_id
    || ref.schema !== "capability.effect_result/v1" || !record(ref.inline)) return null;
  const value = ref.inline, data = value.data;
  const expectedStatus = { success: "succeeded", partial: "partial", failed: "failed", unknown: "unknown" };
  if (Object.keys(value).length !== inlineKeys.length || !inlineKeys.every((key) => own(value, key))
    || value.schema !== ref.schema || value.capability_id !== step.capability_id || value.definition_version !== 3
    || value.instance_id !== plan.instance_id || value.execution_id !== `${plan.plan_id}:${step.seq}:${plan.revision}`
    || !own(expectedStatus, value.outcome) || step.status !== expectedStatus[value.outcome]
    || !record(data) || data.outcome !== value.outcome) return null;
  const planOnly = planOnlyEffects.has(step.capability_id);
  if (planOnly && (value.outcome !== "success" || data.plan_only !== true
    || typeof data.script !== "string" || !data.script.trim() || data.execute === true || data.executed === true)) return null;
  if (step.capability_id === "operations.switchover.plan"
    && (data.status !== "plan_only" || data.execution_blocked !== true || typeof data.sql !== "string" || !data.sql.trim()
      || !Array.isArray(data.warnings) || data.warnings.length < 1)) return null;
  return { schema: value.schema, capability_id: value.capability_id, definition_version: 3,
    instance_id: value.instance_id, execution_id: value.execution_id, outcome: value.outcome,
    step_status: step.status, plan_only: planOnly, data };
}

const privateField = /password|passwd|pwd|secret|token|credential|authorization|cookie|(?:api|access|private)[_-]?key|connection[_-]?string|dsn/i;

function flattenResult(step, value) {
  const rows = [];
  const append = (field, item) => {
    if (rows.length >= 100 || !field || privateField.test(field)) return;
    if (typeof item === "number" && !Number.isFinite(item)) return;
    if (item !== null && !["string", "number", "boolean"].includes(typeof item)) return;
    rows.push({ key: `${step.seq}:${rows.length}`, seq: step.seq,
      capability_id: step.capability_id, field: field.slice(0, 200), value: typeof item === "string" ? item.slice(0, 500) : item });
  };
  const flatten = (item, prefix = "", depth = 0) => {
    if (rows.length >= 100 || depth > 3) return;
    if (Array.isArray(item)) {
      item.slice(0, 20).forEach((child, index) => flatten(child, `${prefix}[${index}]`, depth + 1));
    } else if (record(item)) {
      Object.keys(item).slice(0, 30).forEach((key) => {
        if (!["__proto__", "prototype", "constructor"].includes(key) && !privateField.test(key)) {
          flatten(item[key], prefix ? `${prefix}.${key}` : key, depth + 1);
        }
      });
    } else append(prefix, item);
  };
  flatten(value);
  return rows;
}

function emptyInvestigationContext(step, value) {
  const fields = ["items", "matches", "captures", "probe_leases", "kernel_capabilities"];
  return step.capability_id === "diagnosis.investigation.read" && value.total === 0
    && fields.every((field) => Array.isArray(value[field]) && value[field].length === 0);
}

function hasCoherentCompletedSteps(plan) {
  if (!saved(plan) || plan.status !== "settled" || !["succeeded", "partial"].includes(plan.outcome)
    || !Array.isArray(plan.steps) || plan.steps.length < 1 || plan.steps.length > 20
    || !plan.steps.every(record)) return false;
  const rawSteps = stepsOf(plan);
  const statusesAgree = plan.outcome === "succeeded"
    ? rawSteps.every((step) => step.status === "succeeded")
    : rawSteps.some((step) => ["succeeded", "partial"].includes(step.status))
      && rawSteps.every((step) => terminalSteps.has(step.status));
  if (!statusesAgree) return false;
  const inspectionPlan = plan.capability_id === "inspection.run.execute";
  const reportPlan = plan.capability_id === "report.document.generate";
  return rawSteps.every((step) => (step.capability_id !== "inspection.run.execute"
    || inspectionPlanResult(plan, step))
    && (step.capability_id !== "report.document.generate" || reportDocumentPlanResult(plan, step))
    && (!commonEffects.has(step.capability_id) || commonEffectPlanResult(plan, step)))
    && (!inspectionPlan || (plan.definition_version === 2 && rawSteps[0]?.capability_id === "inspection.run.execute"
      && inspectionPlanResult(plan, rawSteps[0])))
    && (!reportPlan || (plan.definition_version === 3 && rawSteps[0]?.capability_id === "report.document.generate"
      && reportDocumentPlanResult(plan, rawSteps[0])));
}

// Result rendering support is deliberately independent from the persisted execution status.
// It only projects inline JSON already returned by the plan API and never follows a reference.
export function assistantPlanStepResult(plan, step) {
  const reportDocument = reportDocumentPlanResult(plan, step);
  if (reportDocument) {
    if (!["succeeded", "partial"].includes(plan.outcome) || !hasCoherentCompletedSteps(plan)) {
      return { status: "not_applicable", kind: "none", message: "该步骤没有可展示的已完成结果。", rows: [] };
    }
    return { status: "available", kind: "report_document",
      message: reportDocument.outcome === "success" ? "报告已生成并保存。" : "时间范围内没有可用于生成报告的证据。",
      rows: [], value: reportDocument, artifactId: reportDocument.artifact_id };
  }
  const rootCause = rootCausePlanResult(plan, step);
  if (rootCause) {
    if (["succeeded", "partial"].includes(plan.outcome) && !hasCoherentCompletedSteps(plan)) {
      return { status: "not_applicable", kind: "none", message: "该步骤没有可展示的已完成结果。", rows: [] };
    }
    return { status: "available", kind: "root_cause", message: "已保存根因分析结果。", rows: [],
      value: rootCause, incidentId: rootCause.incident_id, instanceId: rootCause.instance_id };
  }
  const effect = commonEffectPlanResult(plan, step);
  if (effect) {
    if (["succeeded", "partial"].includes(plan.outcome) && !hasCoherentCompletedSteps(plan)) {
      return { status: "not_applicable", kind: "none", message: "该步骤没有可展示的已完成结果。", rows: [] };
    }
    const message = effect.plan_only ? "已生成操作方案，尚未执行变更。"
      : effect.outcome === "success" ? "实际执行结果已由服务端保存。"
        : effect.outcome === "partial" ? "任务部分完成，仅展示已保存的执行证据。"
          : effect.outcome === "failed" ? "任务执行失败，仅展示已保存的失败证据。" : "执行结果仍待核对。";
    return { status: "available", kind: "effect", message, rows: flattenResult(step, effect.data), value: effect };
  }
  if (!hasCoherentCompletedSteps(plan)
    || !record(step) || !["succeeded", "partial"].includes(step.status)) {
    return { status: "not_applicable", kind: "none", message: "该步骤没有可展示的已完成结果。", rows: [] };
  }
  if (inspectionPlanResult(plan, step)) {
    return { status: "available", kind: "inspection", message: "已保存巡检任务结果。", rows: [] };
  }
  const value = storedReadResult(step);
  if (!value) {
    return { status: "unsupported", kind: "unsupported",
      message: "服务端已记录执行状态；当前前端不支持该结果格式。", rows: [] };
  }
  const rows = flattenResult(step, value);
  if (emptyInvestigationContext(step, value)) {
    return { status: "empty", kind: "investigation_context",
      message: "读取成功，暂无上下文；尚未形成根因结论。", rows };
  }
  if (!rows.length) {
    return { status: "empty", kind: "object",
      message: "读取成功，但结果中没有可安全展示的字段。", rows };
  }
  return { status: "available", kind: "object", message: "已显示服务端保存的只读结果。", rows };
}

// This is a projection of persisted state; modal visibility and timers have no authority here.
export function planPresentation(plan) {
  const persisted = saved(plan);
  const settled = persisted && plan.status === "settled";
  let recognizedOutcome = settled && own(outcomes, plan.outcome);
  const active = persisted && own(lifecycle, plan.status) && plan.outcome == null;
  let status = recognizedOutcome ? plan.outcome : active ? plan.status : "unknown";
  const rawSteps = stepsOf(plan);
  const inspectionPlan = plan?.capability_id === "inspection.run.execute";
  if (recognizedOutcome && (own(plan, "steps") || inspectionPlan) && ["succeeded", "partial"].includes(status)) {
    if (!hasCoherentCompletedSteps(plan)) {
      status = "unknown";
      recognizedOutcome = false;
    }
  }
  let [label, color] = active ? lifecycle[status] : outcomes[status];
  const hasResults = recognizedOutcome && ["succeeded", "partial"].includes(status);
  const steps = rawSteps.map((step, index) => {
    let stepStatus = own(stepLabels, step.status) ? step.status : "unknown";
    if ((!active && !recognizedOutcome) || status === "unknown") stepStatus = "unknown";
    if (["succeeded", "partial"].includes(stepStatus) && step.capability_id === "inspection.run.execute"
      && !inspectionPlanResult(plan, step)) stepStatus = "unknown";
    if (["succeeded", "partial"].includes(stepStatus) && commonEffects.has(step.capability_id)
      && !commonEffectPlanResult(plan, step)) stepStatus = "unknown";
    const [baseLabel, stepColor] = stepLabels[stepStatus];
    const effect = commonEffectPlanResult(plan, step);
    const stepLabel = stepStatus === "succeeded" && step.capability_id === "inspection.run.execute" ? "巡检执行成功"
      : stepStatus === "succeeded" && step.capability_id === "diagnosis.root_cause.execute" ? "诊断完成"
        : stepStatus === "succeeded" && step.capability_id === "report.document.generate" ? "报告生成完成"
          : stepStatus === "succeeded" && effect?.plan_only ? "方案已生成"
            : stepStatus === "succeeded" && effect ? "执行完成" : baseLabel;
    const result = assistantPlanStepResult(plan, step);
    return {
      seq: Number.isInteger(step.seq) && step.seq > 0 ? step.seq : index + 1,
      title: text(step.title) || "Task", capability_id: text(step.capability_id, 64),
      status: stepStatus, label: stepLabel, color: stepColor,
      resultStatus: result.status, resultMessage: result.message, resultKind: result.kind,
    };
  });
  let percent = 0;
  if (recognizedOutcome && executedOutcomes.has(status)) percent = 100;
  if (active && status === "running" && rawSteps.length) {
    percent = Math.min(99, Math.floor(100 * rawSteps.filter((step) => terminalSteps.has(step.status)).length / rawSteps.length));
  }
  let summary = active
    ? status === "running" ? "计划仍在服务端运行；关闭窗口不会取消执行。" : "计划尚未执行；执行必须再次明确确认。"
    : outcomes[status][2];
  if (hasResults && rawSteps.some((step) => inspectionPlanResult(plan, step))) {
    summary = status === "succeeded" ? "计划已结束；巡检结果以保存的任务证据为准，不代表数据库健康。"
      : "计划部分完成；仅展示已保存的结果，未知步骤仍需核对，不能视为全部成功。";
  }
  if (hasResults && rawSteps.some((step) => rootCausePlanResult(plan, step))) {
    summary = status === "succeeded" ? "根因分析已结束；结论与证据以保存的诊断事件为准。"
      : "根因分析已部分完成；请在诊断事件中核对数据质量、证据与待验证结论。";
  }
  if (hasResults && rawSteps.some((step) => reportDocumentPlanResult(plan, step))) {
    summary = status === "succeeded" ? "报告已生成并保存，可查看结果并下载报告。"
      : "报告未生成：所选时间范围内没有可用证据，请调整范围或先完成数据采集。";
  }
  if (hasResults && rawSteps.some((step) => commonEffectPlanResult(plan, step)?.plan_only)) {
    label = "方案已生成";
    color = "success";
    summary = "操作方案已生成并保存，尚未执行任何变更；请先复核方案内容。";
  } else if (hasResults && rawSteps.some((step) => commonEffectPlanResult(plan, step))) {
    summary = status === "succeeded" ? "受控任务已结束；结果以服务端保存的执行证据为准。"
      : "受控任务仅部分完成；仅展示已保存的执行证据，其他步骤仍需核对。";
  }
  const renderable = steps.filter((step) => ["available", "empty", "unsupported"].includes(step.resultStatus));
  const unsupportedCount = renderable.filter((step) => step.resultStatus === "unsupported").length;
  const availableCount = renderable.filter((step) => step.resultStatus === "available").length;
  const empty = renderable.filter((step) => step.resultStatus === "empty");
  let resultStatus = "not_applicable", resultSummary = "当前状态没有已完成结果。";
  if (hasResults && !renderable.length) {
    resultStatus = "not_provided";
    resultSummary = "服务端未提供可展示的步骤结果。";
  } else if (unsupportedCount && unsupportedCount === renderable.length) {
    resultStatus = "unsupported";
    resultSummary = "服务端执行状态已确认；当前前端不支持结果格式。";
  } else if (unsupportedCount) {
    resultStatus = "partially_supported";
    resultSummary = "服务端执行状态已确认；部分步骤结果格式当前无法展示。";
  } else if (availableCount) {
    resultStatus = "available";
    resultSummary = "已提供可安全展示的持久结果。";
  } else if (empty.length) {
    resultStatus = "empty";
    resultSummary = empty.length === 1 ? empty[0].resultMessage : "读取成功，但没有可展示的数据。";
  }
  return { status, label, color, percent, summary, resultStatus, resultSummary, steps };
}
