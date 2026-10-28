import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Empty, Input, InputNumber, message, Modal, Progress, Select, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, EditOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, SafetyCertificateOutlined, SaveOutlined, StopOutlined } from "@ant-design/icons";
import { listAssistantCapabilities, getAssistantCapability, createAssistantPlan, listAssistantPlans, getAssistantPlan, getAssistantPlanValidation, executeAssistantPlan, getAssistantPlanApproval, reviseAssistantPlan, requestAssistantPlanApproval, approveAssistantPlan, revokeAssistantPlanApproval } from "./modules/assistantApi";
import { assistantPlanStepResult, planPresentation, assistantPlanDraft, changeAssistantSteps } from "./modules/assistantPlanState";
import { assistantPlanApprovalExpired, customerCapabilityTitle, customerPlanPresentation } from "./modules/assistantPlanCustomer";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { onAuthExpired } from "./modules/authEvents";
import { authApi } from "./apiClients";
import { AssistantBatchWorkspace } from "./AssistantBatchWorkspace";
import { AssistantWorkflowWorkspace } from "./AssistantWorkflowWorkspace";
import { AssistantInspectionResult } from "./AssistantInspectionResult";
import { AssistantGenericCapabilityPanel } from "./AssistantGenericCapabilityPanel";
import { downloadReportArtifact } from "./modules/reportApi";
import "./assistantPlan.css";

const READERS = new Set(["inspection.run.query", "inspection.template.read", "operations.capability_matrix.read"]);
const GOVERNED_EFFECTS = {
  "inspection.run.execute": { version: 2, kind: "execute", outputSchema: "inspection.plan_result/v1", requiresInstance: true },
  "diagnosis.root_cause.execute": { version: 1, kind: "collect", outputSchema: "diagnosis.root_cause_result/v1", requiresInstance: true },
  "report.document.generate": { version: 3, kind: "collect", outputSchema: "report.document_result/v1", requiresInstance: true },
  "monitor.alert.sync": { version: 3, kind: "sync", outputSchema: "capability.effect_result/v1", requiresInstance: false },
  "monitor.capacity.execute": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "monitor.observability.bind": { version: 3, kind: "bind", outputSchema: "capability.effect_result/v1", requiresInstance: false },
  "performance.workload.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "performance.sql_profile.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "performance.index.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "performance.table_profile.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "performance.baseline.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "performance.capacity.collect": { version: 3, kind: "collect", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "operations.startup.plan": { version: 3, kind: "plan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "operations.shutdown.plan": { version: 3, kind: "plan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "operations.switchover.plan": { version: 3, kind: "plan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "governance.candidate.scan": { version: 3, kind: "scan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "protection.backup.plan": { version: 3, kind: "plan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "protection.restore.plan": { version: 3, kind: "plan", outputSchema: "capability.effect_result/v1", requiresInstance: true },
  "integration.zabbix.sync": { version: 3, kind: "sync", outputSchema: "capability.effect_result/v1", requiresInstance: false },
  "integration.prometheus.sync": { version: 3, kind: "sync", outputSchema: "capability.effect_result/v1", requiresInstance: false },
};
const ACTIVE_EXECUTION_QUEUES = new Set(["queued", "running"]);
const NAMES = { "inspection.run.query": "巡检任务查询", "inspection.template.read": "巡检模板查询", "operations.capability_matrix.read": "运维能力矩阵",
  "diagnosis.investigation.read": "诊断上下文读取", "diagnosis.root_cause.execute": "端到端根因分析",
  "report.document.generate": "生成分析报告", "monitor.alert.sync": "导入容量与外部告警",
  "monitor.capacity.execute": "采集容量快照", "monitor.observability.bind": "自动绑定可观测数据源",
  "performance.workload.collect": "采集工作负载报告", "performance.sql_profile.collect": "采集 SQL 画像",
  "performance.index.collect": "采集索引发现", "performance.table_profile.collect": "采集表画像",
  "performance.baseline.collect": "采集性能基线", "performance.capacity.collect": "采集容量证据",
  "operations.startup.plan": "生成 Oracle 启动方案", "operations.shutdown.plan": "生成 Oracle 停库方案",
  "operations.switchover.plan": "生成 Data Guard 切换方案", "governance.candidate.scan": "扫描治理候选",
  "protection.backup.plan": "生成备份方案", "protection.restore.plan": "生成恢复演练方案",
  "integration.zabbix.sync": "同步 Zabbix 主机与问题", "integration.prometheus.sync": "同步 Prometheus 标准指标" };
const RCA_DISPLAY_LABELS = {
  status: { root_caused: "已形成根因假设", diagnosing: "分析中", failed: "失败", resolved: "已恢复", reviewed: "已复核", closed: "已关闭" },
  dataQuality: { good: "数据完整", sufficient: "证据充分", partial: "数据不完整", missing: "缺少数据", unknown: "待确认" },
  conclusion: { hypotheses_require_validation: "根因假设待验证", confirmed: "根因已确认", not_available: "尚无根因结论", unknown: "待确认" },
  severity: { critical: "严重", high: "高", warning: "警告", warn: "警告", info: "提示", normal: "正常", unknown: "待确认" },
  layer: { business: "业务", application: "应用", app: "应用", network: "网络", os: "操作系统",
    operating_system: "操作系统", storage: "存储", database: "数据库", db: "数据库", sql: "SQL" },
};
const PAGE_SIZE = 10;
const governedSteps = (plan) => Array.isArray(plan?.steps) && plan.steps.length > 0
  && plan.steps.every((step) => (READERS.has(step.capability_id) && step.definition_version === 1)
    || GOVERNED_EFFECTS[step.capability_id]?.version === step.definition_version);
const hasDurableExecution = (plan) => typeof plan?.execution_batch_id === "string"
  && typeof plan?.execution_queue_state === "string";
const executionInProgress = (plan) => hasDurableExecution(plan)
  && ACTIVE_EXECUTION_QUEUES.has(plan.execution_queue_state);
const shouldPollPlan = (plan) => hasDurableExecution(plan) ? executionInProgress(plan) : plan?.status === "running";
const executionQueueNotice = (plan) => ({
  queued: ["info", "已进入持久执行队列", "任务正在等待工作节点处理；页面会持续读取原清单状态。"],
  running: ["info", "持久执行队列正在运行", "任务已由工作节点接管；页面会持续读取原清单状态。"],
  reconciliation_required: ["warning", "队列执行结果待核对", "服务端无法确认完整终态，请刷新清单并按执行批次核对。"],
  blocked: ["warning", "队列执行已阻止", "任务没有继续执行，请按执行批次核对阻断原因。"],
  cancelled: ["warning", "队列投递已取消", "任务未在当前队列继续执行。"],
  expired: ["warning", "队列投递已过期", "任务未在有效期内完成投递。"],
  paused: ["warning", "队列执行已暂停", "任务已暂停，请按执行批次核对后续控制状态。"],
  stopped: ["warning", "队列执行已停止", "任务已停止后续下发，请按执行批次核对最终状态。"],
}[plan?.execution_queue_state] || null);

const capabilityVersion = (item) => item?.definition_version ?? item?.version;
const capabilityName = (item) => NAMES[item?.capability_id] || item?.title || item?.name || item?.capability_id;

function isServerRead(item) {
  return Boolean(item && item.execution === "server" && ["read", "query"].includes(item.kind)
    && item.plan_executable && capabilityVersion(item) >= 1);
}

function simpleReader(item) {
  const required = item?.params_schema?.required;
  return !Array.isArray(required) || required.every((key) => ["page", "page_size"].includes(key));
}

function knownReader(item, instance) {
  return Boolean(isServerRead(item) && simpleReader(item) && item.availability === "available"
    && (!item.requires_instance || instance)
    // Global readers may still be opened with an instance selected; only instance-bound
    // capabilities need the engine allow-list to contain that instance's engine.
    && (!instance || !item.requires_instance || item.authorized_engines?.includes(instance.db_type)));
}

function knownGovernedEffect(item, definitionVersion, plan, instances) {
  const contract = GOVERNED_EFFECTS[item?.capability_id];
  if (!contract || item.execution !== "server" || item.kind !== contract.kind || item.handler_status !== "wired"
    || item.plan_executable !== true || item.availability !== "available"
    || capabilityVersion(item) !== contract.version || item.output_schema !== contract.outputSchema
    || definitionVersion !== contract.version || item.requires_instance !== contract.requiresInstance) return false;
  if (!contract.requiresInstance) return plan.instance_id === null;
  if (!Number.isSafeInteger(plan.instance_id) || plan.instance_id <= 0) return false;
  const target = instances.find((candidate) => candidate.id === plan.instance_id);
  return Boolean(target && plan.db_type === target.db_type && item.authorized_engines?.includes(target.db_type));
}

function knownPlan(plan, capabilities, instances = []) {
  if (!plan || !Array.isArray(plan.steps)) return false;
  const primary = capabilities.find((item) => item.capability_id === plan.capability_id);
  if (!(isServerRead(primary) && plan.definition_version === capabilityVersion(primary))
    && !knownGovernedEffect(primary, plan.definition_version, plan, instances)) return false;
  return plan.steps.every((step) => {
    const item = capabilities.find((candidate) => candidate.capability_id === step.capability_id);
    return (isServerRead(item) && step.definition_version === capabilityVersion(item))
      || (knownGovernedEffect(item, step.definition_version, plan, instances)
        && GOVERNED_EFFECTS[step.capability_id]?.version === step.definition_version);
  });
}

function composeSteps(steps, capabilities) {
  if (!steps.length || steps.length > 20) throw new Error("清单必须包含 1 至 20 个步骤");
  return steps.map((step, index) => {
    const capability = capabilities.find((item) => item.capability_id === step.capability_id);
    if (!capability || !isServerRead(capability)
      || !Array.isArray(step.depends_on) || new Set(step.depends_on).size !== step.depends_on.length
      || !step.depends_on.every((key) => steps.slice(0, index).some((prior) => prior.key === key))) throw new Error("请核对步骤能力与前置依赖");
    const params = step.capability_id === "operations.capability_matrix.read" ? {} : step.params;
    if (Object.keys(params).some((key) => !["page", "page_size"].includes(key))
      || Object.entries(params).some(([key, value]) => !Number.isInteger(value) || value < 1 || value > (key === "page" ? 1000000 : 100))) throw new Error("请填写有效的分页参数");
    return { seq: index + 1, title: NAMES[step.capability_id], capability_id: step.capability_id,
      definition_version: capability.definition_version, params: { ...params },
      depends_on: step.depends_on.map((key) => steps.findIndex((prior) => prior.key === key) + 1), input_refs: {}, fanout: 1, retries: 0 };
  });
}

function checkValidity(seconds) {
  if (!Number.isInteger(seconds) || seconds < 60 || seconds > 86400) throw new Error("清单有效期必须为 60 至 86400 秒");
  return seconds;
}

function editablePlan(value) {
  return Boolean(value && Number.isSafeInteger(value.revision) && value.revision >= 1
    && !value.started_at && !value.result_ref && value.status !== "running"
    && ["draft", "validated", "awaiting_approval", "approved", "settled"].includes(value.status)
    && (value.outcome == null || ["needs_input", "blocked", "unsupported", "plan_only"].includes(value.outcome)));
}

function boundedWait(promise) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("等待响应超时，请刷新清单核对；未确认服务端已停止。")), 90000);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function display(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "object") return String(value);
  return JSON.stringify(value);
}

function displayTime(value) {
  return typeof value === "string" ? value.slice(0, 19).replace("T", " ") + " UTC" : display(value);
}

function rcaDisplay(group, value, fallback = "待确认") {
  const raw = typeof value === "string" ? value.trim() : "";
  return RCA_DISPLAY_LABELS[group]?.[raw.toLowerCase()] || raw || fallback;
}

function InlineResult({ plan, step, isActive, onIncidentReady }) {
  if (step?.capability_id === "inspection.run.execute" && step.definition_version === 2) {
    return <AssistantInspectionResult key={`${plan.plan_id}:${plan.revision}:${step.seq}`} plan={plan} step={step} isActive={isActive} />;
  }
  const result = assistantPlanStepResult(plan, step);
  if (result.kind === "root_cause") {
    const value = result.value;
    return <div className="assistant-plan-result">
      <Alert showIcon type={value.outcome === "success" ? "success" : value.outcome === "failed" ? "error" : "warning"}
        message={value.root_cause_summary || (value.root_cause_count > 0 ? "已形成根因候选" : "尚未形成可验证的根因结论")}
        description={`数据质量：${rcaDisplay("dataQuality", value.data_quality)} · 结论状态：${rcaDisplay("conclusion", value.conclusion_status)}`} />
      <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
        { key: "incident", label: "诊断事件", children: value.incident_no },
        { key: "status", label: "事件状态", children: rcaDisplay("status", value.status) },
        { key: "severity", label: "严重度", children: rcaDisplay("severity", value.severity) },
        { key: "layer", label: "主要层级", children: rcaDisplay("layer", value.primary_layer) },
        { key: "evidence", label: "证据数", children: value.evidence_count },
        { key: "causes", label: "根因候选数", children: value.root_cause_count },
        { key: "window", label: "用户报告时间窗", children: value.sampling.requested_time_window },
        { key: "scope", label: "影响范围", children: value.sampling.impact_scope },
      ]} />
      <Alert showIcon type="info" message="本次采集方式：当前快照"
        description="用户报告的时间窗仅作为分析上下文；本次没有执行历史回放。" />
      {typeof onIncidentReady === "function" && <Button type="primary"
        onClick={() => onIncidentReady({ incidentId: result.incidentId, instanceId: result.instanceId })}>查看完整诊断</Button>}
    </div>;
  }
  if (result.kind === "report_document") {
    const value = result.value;
    const reportName = value.report_type === "after_sales" ? "售后分析报告" : "统一运维报告";
    if (value.outcome === "partial") {
      return <Alert showIcon type="warning" message={`${reportName}未生成`}
        description={`最近 ${value.window_hours} 小时内没有可用于生成报告的巡检、告警或容量证据。`} />;
    }
    const suffix = value.format === "html" ? "html" : "md";
    return <div className="assistant-plan-result">
      <Alert showIcon type="success" message={`${reportName}已生成`}
        description={`已基于 ${value.evidence_count} 条保存证据生成报告。`} />
      <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
        { key: "window", label: "统计范围", children: `最近 ${value.window_hours} 小时` },
        { key: "format", label: "报告格式", children: value.format === "html" ? "HTML" : "Markdown" },
        { key: "source", label: "分析来源", children: value.analysis_source === "ai" ? `AI${value.model ? `（${value.model}）` : ""}` : "规则引擎" },
        { key: "evidence", label: "证据数", children: value.evidence_count },
        { key: "generated", label: "生成时间", children: displayTime(value.generated_at) },
      ]} />
      <Button type="primary" onClick={async () => {
        if (!isActive()) return;
        try {
          await downloadReportArtifact(value.artifact_id, value.format, `${reportName}.${suffix}`);
        } catch (error) {
          if (isActive()) message.error(error.message || "报告下载失败");
        }
      }}>下载报告</Button>
    </div>;
  }
  if (result.kind === "effect") {
    const value = result.value;
    const tone = value.outcome === "success" ? (value.plan_only ? "info" : "success")
      : value.outcome === "failed" ? "error" : "warning";
    const title = value.plan_only ? "已生成操作方案，尚未执行变更"
      : value.outcome === "success" ? "任务执行完成，结果已保存"
        : value.outcome === "partial" ? "任务部分完成，结果已保存"
          : value.outcome === "failed" ? "任务执行失败，失败证据已保存" : "任务结果仍待核对";
    return <div className="assistant-plan-result">
      <Alert showIcon type={tone} message={title}
        description={value.plan_only ? "当前保存内容仅供复核；没有执行启动、停库、切换、备份或恢复变更。" : result.message} />
      {result.rows.length > 0 && <Table size="small" pagination={{ pageSize: 20 }} scroll={{ x: 700 }} rowKey="key"
        dataSource={result.rows} columns={[
          { title: "结果字段", dataIndex: "field", width: 320, ellipsis: true },
          { title: "保存的值", dataIndex: "value", render: display },
        ]} />}
    </div>;
  }
  const rows = result.rows;
  if (!rows.length || result.status === "empty") {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={result.message} />;
  }
  return <div className="assistant-plan-result">
    <Table size="small" pagination={{ pageSize: 20 }} scroll={{ x: 700 }} rowKey="key" dataSource={rows} columns={[
      { title: "结果字段", dataIndex: "field", width: 320, ellipsis: true },
      { title: "保存的值", dataIndex: "value", render: display },
    ]} />
  </div>;
}

export function AssistantPlanWorkspace({ open, onClose, instances = [], instanceId, request, onIncidentReady, customerMode = false }) {
  const authSession = getAuthSession();
  const dataSession = useRef(authSession);
  const [, setAuthTick] = useState(0);
  const dataCurrent = isAuthSessionCurrent(dataSession.current);
  const [tab, setTab] = useState("new");
  const [batchOpen, setBatchOpen] = useState(false);
  const [workflowOpen, setWorkflowOpen] = useState(false);
  const [genericOpen, setGenericOpen] = useState(false);
  const [genericInitial, setGenericInitial] = useState(null);
  const [batchInitial, setBatchInitial] = useState(null);
  const [workflowSource, setWorkflowSource] = useState(null);
  const [workflowAction, setWorkflowAction] = useState("prepare");
  const [storedCapabilities, setCapabilities] = useState([]);
  const capabilities = dataCurrent ? storedCapabilities : [];
  const [capabilityId, setCapabilityId] = useState();
  const [target, setTarget] = useState(instanceId || undefined);
  const [pageSize, setPageSize] = useState(20);
  const [page, setPage] = useState(1);
  const [firstKey, setFirstKey] = useState("primary");
  const [draftSteps, setDraftSteps] = useState([]);
  const [validForSeconds, setValidForSeconds] = useState();
  const [requestApproval, setRequestApproval] = useState(false);
  const [revisionBase, setRevisionBase] = useState(null);
  const [draftKey, setDraftKey] = useState(null);
  const [approvalId, setApprovalId] = useState("");
  const [approvalMode, setApprovalMode] = useState(false);
  const [approval, setApproval] = useState(null);
  const [approvalSeconds, setApprovalSeconds] = useState(900);
  const [storedPlan, setPlan] = useState(null);
  const plan = dataCurrent ? storedPlan : null;
  const [storedHistory, setHistory] = useState({ items: [], total: 0, page: 1 });
  const history = dataCurrent ? storedHistory : { items: [], total: 0, page: 1 };
  const [catalogError, setCatalogError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [actionError, setActionError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [storedResultStep, setResultStep] = useState(null);
  const resultStep = dataCurrent ? storedResultStep : null;
  const [monitorNotice, setMonitorNotice] = useState("");
  const [queuedRequest, setQueuedRequest] = useState(false);
  const [actorId, setActorId] = useState(null);
  const [validation, setValidation] = useState(null);
  const alive = useRef(true);
  const visible = useRef(open);
  visible.current = open;
  const busyRef = useRef(false);
  const busyOperation = useRef(null);
  const validationBusyRef = useRef(false);
  const validationEpoch = useRef(0);
  const catalogEpoch = useRef(0);
  const historyEpoch = useRef(0);
  const detailEpoch = useRef(0);
  const activePlanId = useRef(null);
  const pollBudget = useRef({ id: null, count: 0 });
  const appliedRequest = useRef(null);
  const stepSequence = useRef(0);
  const actionEpoch = useRef(0);

  useEffect(() => {
    alive.current = true;
    const notify = () => setAuthTick((value) => value + 1);
    const removeExpired = onAuthExpired(notify);
    globalThis.addEventListener?.("storage", notify);
    return () => { alive.current = false; actionEpoch.current += 1; catalogEpoch.current += 1; historyEpoch.current += 1; detailEpoch.current += 1;
      removeExpired(); globalThis.removeEventListener?.("storage", notify); };
  }, []);

  const current = (session) => alive.current && visible.current && isAuthSessionCurrent(session);
  const invalidateValidation = () => {
    validationEpoch.current += 1;
    validationBusyRef.current = false;
    setValidation(null);
  };
  const closeWorkspace = () => {
    visible.current = false;
    actionEpoch.current += 1;
    detailEpoch.current += 1;
    invalidateValidation();
    setBatchOpen(false);
    setWorkflowOpen(false);
    setGenericOpen(false);
    setResultStep(null);
    onClose();
  };
  const publish = (value) => {
    invalidateValidation();
    activePlanId.current = value.plan_id;
    setPlan(value);
    setApproval(null);
    setResultStep(null);
  };

  useEffect(() => {
    actionEpoch.current += 1;
    detailEpoch.current += 1;
    if (!isAuthSessionCurrent(dataSession.current)) {
      busyOperation.current = null;
      busyRef.current = false;
      setBusy(false);
    }
    setApproval(null);
    invalidateValidation();
    setBatchOpen(false);
    setWorkflowOpen(false);
    if (!isAuthSessionCurrent(dataSession.current)) {
      dataSession.current = authSession;
      catalogEpoch.current += 1;
      historyEpoch.current += 1;
      activePlanId.current = null;
      pollBudget.current = { id: null, count: 0 };
      appliedRequest.current = null;
      setPlan(null); setHistory({ items: [], total: 0, page: 1 }); setCapabilities([]); setResultStep(null);
      setWorkflowSource(null); setApprovalMode(false); setApprovalId(""); setActorId(null);
      setRevisionBase(null); setDraftKey(null); setDraftSteps([]); setCapabilityId(undefined);
      setTarget(instanceId || undefined); setPage(1); setPageSize(20); setFirstKey("primary");
      setValidForSeconds(undefined); setRequestApproval(false); setUncertain(false);
      setCatalogError(""); setHistoryError(""); setActionError(""); setMonitorNotice("");
      setCatalogLoading(false); setHistoryLoading(false); setQueuedRequest(false); setTab("new");
    }
    return () => { validationEpoch.current += 1; validationBusyRef.current = false; };
  }, [open, request, instanceId, authSession.token, authSession.generation, authSession.loginEpoch]);

  const loadCatalog = async () => {
    const epoch = ++catalogEpoch.current;
    const session = getAuthSession();
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const value = await boundedWait(listAssistantCapabilities());
      if (current(session) && epoch === catalogEpoch.current) setCapabilities(value.items);
    } catch (error) {
      if (current(session) && epoch === catalogEpoch.current) {
        setCapabilities([]);
        setCatalogError(error.message || "能力目录加载失败，未执行任何操作");
      }
    } finally {
      if (current(session) && epoch === catalogEpoch.current) setCatalogLoading(false);
    }
  };

  const loadHistory = async (page = 1) => {
    const epoch = ++historyEpoch.current;
    const session = getAuthSession();
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const value = await boundedWait(listAssistantPlans({ page, page_size: PAGE_SIZE }));
      if (current(session) && epoch === historyEpoch.current) setHistory(value);
    } catch (error) {
      if (current(session) && epoch === historyEpoch.current) setHistoryError(error.message || "历史清单加载失败");
    } finally {
      if (current(session) && epoch === historyEpoch.current) setHistoryLoading(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    loadCatalog();
    loadHistory();
    detailEpoch.current += 1;
    return () => { detailEpoch.current += 1; };
  }, [open, request, authSession.token, authSession.generation, authSession.loginEpoch]);

  useEffect(() => {
    if (!open) return;
    if (request?.capability && request !== appliedRequest.current && busy) {
      setQueuedRequest(true);
      return;
    }
    if (request?.batchId && request !== appliedRequest.current) {
      appliedRequest.current = request;
      setBatchInitial(request.batchId);
      setBatchOpen(true);
      return;
    }
    if (request?.planId && request !== appliedRequest.current) {
      appliedRequest.current = request;
      setQueuedRequest(false);
      setTab("result");
      readPlan(request.planId);
      return;
    }
    if (request?.initialTab === "history" && !request.capability && !request.planId && !request.batchId
      && request !== appliedRequest.current) {
      appliedRequest.current = request;
      setQueuedRequest(false);
      setTab("history");
      return;
    }
    if (request?.capability && request !== appliedRequest.current) {
      setCapabilityId(request.capability.capability_id);
      setTarget(request.instanceId || undefined);
      setDraftSteps([]);
      setFirstKey("primary");
      setPage(1);
      setRevisionBase(null);
      setDraftKey(null);
      setRequestApproval(false);
      setActionError("");
      setTab("new");
      if (request.capability.execution === "server") {
        setGenericInitial({ capabilityId: request.capability.capability_id,
          instanceId: request.instanceId || undefined, params: request.params || {} });
        setGenericOpen(true);
      }
    }
    appliedRequest.current = request;
    setQueuedRequest(false);
  }, [open, request, busy, authSession.token, authSession.generation, authSession.loginEpoch]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const session = getAuthSession();
    setActorId(null);
    boundedWait(authApi.currentUser()).then((value) => {
      if (!cancelled && current(session) && Number.isInteger(value?.id)) setActorId(value.id);
    }).catch(() => { if (!cancelled && current(session)) setActorId(null); });
    return () => { cancelled = true; };
  }, [open, authSession.token, authSession.generation, authSession.loginEpoch]);

  const readPlan = async (id, { polling = false, forApproval = false, expectedRevision, throwOnError = false } = {}) => {
    invalidateValidation();
    setApproval(null);
    const epoch = ++detailEpoch.current;
    const session = getAuthSession();
    if (!polling) {
      pollBudget.current = { id, count: 0 };
      setMonitorNotice("");
    }
    try {
      const envelope = await boundedWait(forApproval ? getAssistantPlanApproval(id) : getAssistantPlan(id));
      const value = forApproval ? envelope.plan : envelope;
      if (expectedRevision !== undefined && value.revision !== expectedRevision) throw new Error("修订已变化，请重新读取并核对冻结摘要");
      if (!current(session) || epoch !== detailEpoch.current) return;
      publish(value);
      setApprovalMode(forApproval);
      if (forApproval) setApproval(envelope);
      setActionError("");
      setUncertain(false);
      setTab("result");
      return value;
    } catch (error) {
      if (current(session) && epoch === detailEpoch.current) {
        setActionError(error.message || "清单详情读取失败");
        if (polling) setMonitorNotice("自动刷新失败，请手动刷新状态；尚未确认服务端停止。");
      }
      if (throwOnError) throw error;
    }
  };

  // Validation identity restarts polling even when loading/completion are batched.
  useEffect(() => {
    if (!open || tab !== "result" || !shouldPollPlan(plan) || busy || validationBusyRef.current) return;
    if (pollBudget.current.id !== plan.plan_id) pollBudget.current = { id: plan.plan_id, count: 0 };
    if (pollBudget.current.count >= 40) {
      setMonitorNotice("已达到自动刷新上限，请手动刷新状态；服务端任务不会因此取消。");
      return;
    }
    const id = plan.plan_id;
    const timer = setTimeout(() => {
      if (activePlanId.current !== id || busyRef.current || validationBusyRef.current) return;
      pollBudget.current.count += 1;
      readPlan(id, { polling: true, forApproval: approvalMode });
    }, 1500);
    return () => clearTimeout(timer);
  }, [open, tab, plan, busy, validation, approvalMode]);

  const selected = capabilities.find((item) => item.capability_id === capabilityId);
  const selectedInstance = instances.find((item) => item.id === target);
  const editorSteps = [{ key: firstKey, capability_id: capabilityId,
    params: capabilityId === "operations.capability_matrix.read" ? {} : { page, page_size: pageSize }, depends_on: [] }, ...draftSteps];
  const ready = (!target || selectedInstance) && editorSteps.every((step) => knownReader(capabilities.find((item) => item.capability_id === step.capability_id), selectedInstance));
  const locked = busy || uncertain || (plan && (["running", "unknown"].includes(planPresentation(plan).status)
    || (plan.status !== "settled" && hasDurableExecution(plan))));
  const applySteps = (steps) => {
    setFirstKey(steps[0].key);
    setCapabilityId(steps[0].capability_id);
    setPage(steps[0].params.page ?? 1);
    setPageSize(steps[0].params.page_size ?? 20);
    setDraftSteps(steps.slice(1));
  };
  const changeSteps = (action) => {
    if (locked || busyRef.current) return;
    try { applySteps(changeAssistantSteps(editorSteps, action)); setActionError(""); }
    catch (error) { setActionError(error.message); }
  };
  const beginEdit = (revise) => {
    if (locked || busyRef.current || approvalMode || actorId !== plan?.requested_by
      || (revise ? !editablePlan(plan) : plan?.status !== "settled")) return;
    if (plan.workflow_binding) {
      setWorkflowSource(plan);
      setWorkflowAction(revise ? "revise" : "rerun");
      setWorkflowOpen(true);
      return;
    }
    try {
      applySteps(assistantPlanDraft(plan));
      setTarget(plan.instance_id ?? undefined);
      setRevisionBase(revise ? { id: plan.plan_id, revision: plan.revision, binding: plan.binding_hash } : null);
      setDraftKey(revise ? null : `manual:${globalThis.crypto.randomUUID()}`);
      setValidForSeconds(3600);
      setRequestApproval(plan.approval_state !== "not_required");
      setActionError("");
      setTab("new");
    } catch (error) { setActionError(error.message); }
  };

  const saveDraft = async () => {
    if (!current(authSession)) return;
    if (!open || busyRef.current || validationBusyRef.current || locked || !ready) return;
    if (revisionBase && (plan?.plan_id !== revisionBase.id || plan?.revision !== revisionBase.revision
      || plan?.binding_hash !== revisionBase.binding || actorId !== plan.requested_by || !editablePlan(plan))) {
      setActionError("修订上下文已变化，请重新读取并编辑"); return;
    }
    busyRef.current = true;
    setBusy(true);
    invalidateValidation();
    setActionError("");
    const epoch = ++actionEpoch.current;
    const operationOwner = {};
    busyOperation.current = operationOwner;
    detailEpoch.current += 1;
    const session = authSession;
    const valid = () => current(session) && actionEpoch.current === epoch;
    let submitted = false, committed = false;
    try {
      const steps = composeSteps(editorSteps, capabilities);
      const ttl = checkValidity(validForSeconds ?? 3600);
      for (const step of steps) {
        const cached = capabilities.find((item) => item.capability_id === step.capability_id);
        const fresh = await boundedWait(getAssistantCapability(step.capability_id));
        if (!valid()) return;
        if (!knownReader(fresh, selectedInstance) || fresh.definition_hash !== cached.definition_hash) throw new Error("能力定义或授权已变化，请刷新目录并重新确认");
      }
      const payload = { capability_id: steps[0].capability_id, definition_version: steps[0].definition_version,
        ...(target ? { instance_id: target } : {}), steps, valid_for_seconds: ttl, request_approval: requestApproval };
      submitted = true;
      setUncertain(true);
      const value = await boundedWait(revisionBase
        ? reviseAssistantPlan(revisionBase.id, { ...payload, expected_revision: revisionBase.revision }, { authSession: session })
        : createAssistantPlan({ ...payload, trigger_type: request?.capability ? "assistant" : "manual" }, { ...(draftKey ? { idempotencyKey: draftKey } : {}), authSession: session }));
      if (!valid()) return;
      committed = true;
      publish(value);
      setApprovalMode(false);
      setUncertain(true);
      setTab("result");
      await readPlan(value.plan_id, { expectedRevision: value.revision, throwOnError: true });
      if (!valid()) return;
      setRevisionBase(null);
      loadHistory();
    } catch (error) {
      if (valid()) {
        setUncertain(committed || (submitted && (!error.status || error.status >= 500 || error.code === "assistant_response_invalid")));
        setActionError(error.message || "保存结果待核对，请刷新清单");
      }
    } finally {
      if (busyOperation.current === operationOwner) {
        busyOperation.current = null;
        busyRef.current = false;
        if (alive.current) setBusy(false);
      }
    }
  };

  const govern = async (kind) => {
    if (!current(authSession)) return;
    if (!open || !plan || busyRef.current || validationBusyRef.current || uncertain
      || planPresentation(plan).status === "unknown" || !Number.isSafeInteger(plan.revision)) return;
    if (kind !== "revoke" && (plan.status === "running" || (plan.status !== "settled" && hasDurableExecution(plan)))) return;
    if (!plan.approval_available || !governedSteps(plan) || !knownPlan(plan, capabilities, instances)) return;
    if (kind === "request" && (approvalMode || actorId !== plan.requested_by || plan.approval_state === "revoked")) return;
    if (kind === "approve" && (!approvalMode || !approval || approval.plan.binding_hash !== plan.binding_hash || actorId == null || actorId === plan.requested_by)) return;
    if (kind === "approve" && (!Number.isInteger(approvalSeconds) || approvalSeconds < 60 || approvalSeconds > 3600)) return;
    busyRef.current = true;
    setBusy(true);
    invalidateValidation();
    const session = authSession;
    const id = plan.plan_id;
    const revision = plan.revision;
    const epoch = ++actionEpoch.current;
    const operationOwner = {};
    busyOperation.current = operationOwner;
    detailEpoch.current += 1;
    const valid = () => current(session) && epoch === actionEpoch.current && activePlanId.current === id;
    setActionError("");
    let submitted = false, committed = false;
    try {
      const payload = { expected_revision: revision };
      submitted = true;
      setUncertain(true);
      const value = await boundedWait(kind === "approve"
        ? approveAssistantPlan(id, { ...payload, expected_binding_hash: plan.binding_hash, valid_for_seconds: approvalSeconds }, { authSession: session })
        : kind === "request" ? requestAssistantPlanApproval(id, payload, { authSession: session }) : revokeAssistantPlanApproval(id, payload, { authSession: session }));
      if (!valid()) return;
      committed = true;
      if (value.revision !== revision) throw new Error("修订已变化，请重新读取");
      publish(value);
      setUncertain(true);
      await readPlan(id, { forApproval: approvalMode, expectedRevision: revision, throwOnError: true });
      if (valid()) loadHistory();
    } catch (error) {
      if (valid()) {
        setUncertain(committed || (submitted && (!error.status || error.status >= 500 || error.code === "assistant_response_invalid")));
        setActionError(error.message || "审批请求未完成，请刷新核对");
      }
    } finally {
      if (busyOperation.current === operationOwner) {
        busyOperation.current = null;
        busyRef.current = false;
        if (alive.current) setBusy(false);
      }
    }
  };

  const executeSaved = async (saved, session, epoch = actionEpoch.current) => {
    if (!current(session)) return;
    if (approvalMode || actorId !== saved.requested_by || uncertain || hasDurableExecution(saved) || saved.outcome !== null
      || assistantPlanApprovalExpired(saved)
      || !((saved.status === "validated" && saved.approval_state === "not_required")
        || (saved.status === "approved" && saved.approval_state === "approved" && saved.approval_available === true && saved.binding_hash && saved.expires_at))) return;
    const done = await boundedWait(executeAssistantPlan(saved.plan_id, { authSession: session }));
    if (current(session) && actionEpoch.current === epoch && activePlanId.current === saved.plan_id) publish(done);
  };

  const validatePlan = async () => {
    if (!open || !plan || approvalMode || busyRef.current || validationBusyRef.current) return;
    detailEpoch.current += 1;
    const id = plan.plan_id;
    const session = getAuthSession();
    const epoch = ++validationEpoch.current;
    validationBusyRef.current = true;
    setValidation({ phase: "loading", planId: id, session });
    const valid = () => current(session) && epoch === validationEpoch.current && activePlanId.current === id;
    try {
      const value = await boundedWait(getAssistantPlanValidation(id));
      if (valid()) setValidation({ phase: "ready", planId: id, session, value });
    } catch {
      if (valid()) setValidation({ phase: "error", planId: id, session });
    } finally {
      if (epoch === validationEpoch.current) {
        validationBusyRef.current = false;
        if (!current(session)) setValidation(null);
      }
    }
  };

  const run = async (existing = null) => {
    if (!current(authSession)) return;
    if (busyRef.current || validationBusyRef.current || uncertain || (!existing && (!ready || locked || requestApproval || revisionBase))) return;
    if (existing && (approvalMode || actorId !== existing.requested_by || hasDurableExecution(existing) || existing.outcome !== null
      || assistantPlanApprovalExpired(existing)
      || !knownPlan(existing, capabilities, instances)
      || !((existing.status === "validated" && existing.approval_state === "not_required")
        || (existing.status === "approved" && existing.approval_state === "approved" && existing.approval_available === true && existing.binding_hash && existing.expires_at)))) return;
    busyRef.current = true;
    invalidateValidation();
    setBusy(true);
    setActionError("");
    setUncertain(false);
    detailEpoch.current += 1;
    if (!existing) {
      activePlanId.current = null;
      setPlan(null);
      setResultStep(null);
    }
    const session = authSession;
    const epoch = actionEpoch.current;
    const operationOwner = {};
    busyOperation.current = operationOwner;
    let submitted = false;
    try {
      let saved = existing;
      if (!saved) {
        const steps = composeSteps(editorSteps, capabilities);
        if (validForSeconds !== undefined) checkValidity(validForSeconds);
        const fresh = await boundedWait(getAssistantCapability(selected.capability_id));
        if (!current(session) || epoch !== actionEpoch.current) return;
        if (!fresh.plan_executable || fresh.availability !== "available"
          || fresh.definition_version !== selected.definition_version || fresh.definition_hash !== selected.definition_hash
          || (selectedInstance && !fresh.authorized_engines?.includes(selectedInstance.db_type))) {
          throw Object.assign(new Error("能力定义或授权已变化，请刷新目录并重新确认。"), { status: 409 });
        }
        const payload = {
          capability_id: selected.capability_id,
          definition_version: selected.definition_version,
          ...(target ? { instance_id: target } : {}),
          ...(draftSteps.length ? { steps } : { params: steps[0].params }),
          ...(validForSeconds !== undefined ? { valid_for_seconds: validForSeconds } : {}),
          trigger_type: request?.capability ? "assistant" : "manual",
        };
        for (const step of draftSteps) {
          const cached = capabilities.find((item) => item.capability_id === step.capability_id);
          const definition = await boundedWait(getAssistantCapability(step.capability_id));
          if (!current(session) || epoch !== actionEpoch.current) return;
          if (!knownReader(definition, selectedInstance) || definition.definition_hash !== cached?.definition_hash) throw new Error("步骤定义或授权已变化，请刷新目录");
        }
        submitted = true;
        setUncertain(true);
        saved = await boundedWait(createAssistantPlan(payload, { ...(draftKey ? { idempotencyKey: draftKey } : {}), authSession: session }));
        if (!current(session) || epoch !== actionEpoch.current) {
          if (current(session)) setUncertain(true);
          return;
        }
        publish(saved);
        setApprovalMode(false);
      }
      if (!current(session)) return;
      setTab("result");
      submitted = true;
      setUncertain(true);
      await executeSaved(saved, session, epoch);
      if (current(session) && epoch === actionEpoch.current) setUncertain(false);
      if (current(session)) loadHistory();
    } catch (error) {
      if (current(session)) {
        setUncertain(submitted && (!error.status || error.status >= 500));
        setActionError(error.message || "请求未确认，请刷新清单核对");
      }
    } finally {
      if (busyOperation.current === operationOwner) {
        busyOperation.current = null;
        busyRef.current = false;
        if (alive.current) setBusy(false);
      }
    }
  };

  const view = plan ? uncertain ? { ...planPresentation(plan), status: "unknown", color: "warning", label: "结果待核对",
    percent: 0, summary: "提交后的服务端结果尚未确认，请刷新状态核对；未自动重试。" } : planPresentation(plan) : null;
  const terminalPlan = plan?.status === "settled";
  const queueNotice = executionQueueNotice(plan);
  const statusSummary = plan?.execution_queue_state === "queued"
    ? "执行已接收，等待工作节点执行，无需重复提交。"
    : plan?.execution_queue_state === "running"
      ? "工作节点正在执行已接收的任务，无需重复提交。" : view?.summary;
  const validationView = open && plan && validation?.planId === plan.plan_id && current(validation.session) ? validation : null;
  const validating = validationView?.phase === "loading";
  const historyColumns = [
    { title: customerMode ? "任务编号" : "清单号", dataIndex: "plan_id", width: 245, ellipsis: true },
    { title: customerMode ? "任务" : "能力", dataIndex: "capability_id", width: 180,
      render: (value) => customerMode ? customerCapabilityTitle(value) : NAMES[value] || value },
    { title: "目标实例", dataIndex: "instance_id", width: 140,
      render: (value) => customerMode ? instances.find((item) => item.id === value)?.instance_name || (value == null ? "全局" : `实例 ${value}`) : value ?? "全局" },
    { title: "状态", key: "status", width: 110, render: (_, row) => { const item = planPresentation(row); return <Tag color={item.color}>{item.label}</Tag>; } },
    { title: "创建时间", dataIndex: "created_at", width: 185, render: displayTime },
    { title: "操作", key: "actions", width: 90, render: (_, row) => <Button type="link" disabled={busy} onClick={() => readPlan(row.plan_id)}>查看</Button> },
  ];

  return <>
    <Modal open={open} centered title={customerMode ? "任务进展" : "任务清单"} onCancel={closeWorkspace} footer={<Button onClick={closeWorkspace}>关闭</Button>}
      width={1060} className="assistant-plan-modal">
      <div className="assistant-plan-workspace">
        <Space wrap>
          <Button icon={<PlusOutlined />} disabled={busy || validating} onClick={() => setBatchOpen(true)}>批次任务</Button>
          <Button icon={<PlusOutlined />} disabled={busy || validating} onClick={() => { setWorkflowSource(null); setWorkflowAction("prepare"); setWorkflowOpen(true); }}>固定流程</Button>
          <Button icon={<PlusOutlined />} disabled={busy || validating} onClick={() => setGenericOpen(true)}>通用能力</Button>
        </Space>
        {queuedRequest && <Alert showIcon type="info" message="当前清单仍在执行，新请求将在结束后等待确认" />}
        {actionError && <Alert showIcon type={uncertain ? "warning" : "error"} message={uncertain ? "请求结果待核对" : "请求未完成"} description={actionError} />}
        <Tabs activeKey={tab} onChange={(key) => { if (!busy) setTab(key); }} items={[
          { key: "new", label: customerMode ? "新建任务" : "新建清单", children: <div className="assistant-plan-new">
            {catalogError && <Alert showIcon type="error" message={catalogError} />}
            <Space wrap>
              <Select aria-label="清单能力" placeholder="选择能力" value={capabilityId} disabled={locked || catalogLoading}
                onChange={setCapabilityId} style={{ width: 320, maxWidth: "100%" }} showSearch optionFilterProp="label"
                options={capabilities.map((item) => ({ value: item.capability_id,
                  label: `${capabilityName(item)} · v${capabilityVersion(item)}${isServerRead(item) ? "" : " · 不可在此执行"}`,
                  disabled: !isServerRead(item) || item.availability !== "available" }))} />
              <Button icon={<ReloadOutlined />} loading={catalogLoading} disabled={busy} onClick={loadCatalog}>刷新目录</Button>
            </Space>
            <div className="assistant-plan-fields">
              <label>目标实例<Select aria-label="清单目标实例" placeholder={selected?.requires_instance ? "必须选择实例" : "全局读取（可选实例）"}
                value={target} allowClear={!selected?.requires_instance} disabled={locked} onChange={setTarget}
                options={instances.map((item) => ({ value: item.id, label: `${item.instance_name || item.name} · ${item.db_type} · ${item.host}:${item.port}` }))} /></label>
              {selected && selected.capability_id !== "operations.capability_matrix.read" && <label>返回上限<InputNumber aria-label="清单返回上限" min={1} max={100} precision={0} value={pageSize}
                disabled={locked} onChange={setPageSize} /></label>}
            </div>
            <div className="assistant-plan-steps">
              {editorSteps.map((step, index) => <div key={step.key} className="assistant-plan-editor-step">
                <Space wrap>
                  <Typography.Text strong>步骤 {index + 1}{index === 0 ? " · 主能力" : ""}</Typography.Text>
                  {index > 0 && <Select aria-label={`步骤 ${index + 1} 能力`} value={step.capability_id} disabled={locked}
                    className="assistant-plan-step-capability" options={capabilities.filter((item) => knownReader(item, selectedInstance)).map((item) => ({ value: item.capability_id, label: NAMES[item.capability_id] }))}
                    onChange={(value) => applySteps(editorSteps.map((item) => item.key === step.key ? { ...item, capability_id: value, params: value === "operations.capability_matrix.read" ? {} : { page: 1, page_size: 20 } } : item))} />}
                  <Tooltip title="上移步骤"><Button aria-label={`上移步骤 ${index + 1}`} icon={<ArrowUpOutlined />} disabled={locked || index === 0} onClick={() => changeSteps({ type: "move", key: step.key, direction: -1 })} /></Tooltip>
                  <Tooltip title="下移步骤"><Button aria-label={`下移步骤 ${index + 1}`} icon={<ArrowDownOutlined />} disabled={locked || index === editorSteps.length - 1} onClick={() => changeSteps({ type: "move", key: step.key, direction: 1 })} /></Tooltip>
                  <Tooltip title="删除步骤"><Button aria-label={`删除步骤 ${index + 1}`} icon={<DeleteOutlined />} disabled={locked || editorSteps.length === 1} onClick={() => changeSteps({ type: "remove", key: step.key })} /></Tooltip>
                </Space>
                <div className="assistant-plan-fields">
                  {step.capability_id !== "operations.capability_matrix.read" && <label>页码<InputNumber aria-label={`步骤 ${index + 1} 页码`} min={1} max={1000000} precision={0} value={step.params.page} disabled={locked}
                    onChange={(value) => index === 0 ? setPage(value) : applySteps(editorSteps.map((item) => item.key === step.key ? { ...item, params: { ...item.params, page: value } } : item))} /></label>}
                  {index > 0 && step.capability_id !== "operations.capability_matrix.read" && <label>返回上限<InputNumber aria-label={`步骤 ${index + 1} 返回上限`} min={1} max={100} precision={0} value={step.params.page_size} disabled={locked}
                    onChange={(value) => applySteps(editorSteps.map((item) => item.key === step.key ? { ...item, params: { ...item.params, page_size: value } } : item))} /></label>}
                  {index > 0 && <label>前置步骤<Select mode="multiple" aria-label={`步骤 ${index + 1} 依赖`} value={step.depends_on} disabled={locked}
                    options={editorSteps.slice(0, index).map((prior, priorIndex) => ({ value: prior.key, label: `${priorIndex + 1} · ${NAMES[prior.capability_id] || "未选能力"}` }))}
                    onChange={(value) => applySteps(editorSteps.map((item) => item.key === step.key ? { ...item, depends_on: value } : item))} /></label>}
                </div>
              </div>)}
              <Button icon={<PlusOutlined />} disabled={locked || !ready || editorSteps.length >= 20} onClick={() => changeSteps({ type: "add", step: { key: `new-${++stepSequence.current}`, capability_id: capabilityId, params: capabilityId === "operations.capability_matrix.read" ? {} : { page: 1, page_size: 20 } } })}>添加步骤 ({editorSteps.length}/20)</Button>
            </div>
            <div className="assistant-plan-fields">
              <label>清单有效期（秒）<InputNumber aria-label="清单有效期秒数" min={60} max={86400} precision={0} value={validForSeconds ?? 3600} disabled={locked} onChange={setValidForSeconds} /></label>
              <Checkbox checked={requestApproval} disabled={locked} onChange={(event) => setRequestApproval(event.target.checked)}>此修订需要独立审批</Checkbox>
            </div>
            {revisionBase && <Alert type="warning" showIcon message={`正在修订 ${revisionBase.id} · r${revisionBase.revision}`} description="保存将产生新修订，旧批准失效；不会自动执行。" />}
            {selected && <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
              { key: "capability", label: "能力 ID", children: selected.capability_id },
              { key: "version", label: "定义版本", children: selected.definition_version },
              { key: "target", label: "执行目标", children: target ? (selectedInstance?.instance_name || target) : "全局读取" },
              { key: "effect", label: "动作", children: "读取已有数据" },
            ]} />}
            {selected && !ready && <Alert type="warning" showIcon message="当前清单不可执行" description={selected.unavailable_reason || "请核对目标实例与能力授权范围"} />}
            <Space wrap>
              <Button type="primary" icon={<SaveOutlined />} loading={busy} disabled={locked || validating || !ready || catalogLoading || Boolean(catalogError)} onClick={saveDraft}>{revisionBase ? "保存新修订" : "保存冻结清单"}</Button>
              {!revisionBase && !requestApproval && <Button icon={<PlayCircleOutlined />} loading={busy} disabled={locked || validating || !ready || catalogLoading || Boolean(catalogError)} onClick={() => run()}>确认并执行读取</Button>}
            </Space>
          </div> },
          { key: "approval", label: customerMode ? "审批任务" : "审批读取", children: <div className="assistant-plan-detail">
            <Space.Compact className="assistant-plan-approval-open">
              <Input aria-label="审批清单号" placeholder="pl + 26 位小写十六进制字符" maxLength={28} value={approvalId} disabled={busy}
                onChange={(event) => { detailEpoch.current += 1; setApprovalId(event.target.value); setApproval(null); }} />
              <Button icon={<SafetyCertificateOutlined />} disabled={busy || approvalId.length !== 28 || !/^pl[a-f0-9]{26}$/.test(approvalId)} onClick={() => readPlan(approvalId, { forApproval: true })}>读取审批清单</Button>
            </Space.Compact>
          </div> },
          { key: "history", label: customerMode ? "任务记录" : "历史清单", children: <div className="assistant-plan-history">
            <Button icon={<ReloadOutlined />} loading={historyLoading} disabled={busy} onClick={() => loadHistory(history.page)}>刷新历史</Button>
            {historyError && <Alert type="error" showIcon message="历史清单读取失败" description={historyError} />}
            <Table size="small" rowKey="plan_id" loading={historyLoading} dataSource={history.items} columns={historyColumns} scroll={{ x: 900 }}
              pagination={{ current: history.page, pageSize: PAGE_SIZE, total: history.total, showSizeChanger: false, onChange: loadHistory }} />
          </div> },
          { key: "result", label: customerMode ? "任务进展" : "执行结果", children: plan ? <div className="assistant-plan-detail">
            {monitorNotice && <Alert showIcon type="warning" message={monitorNotice} />}
	            {queueNotice && <Alert showIcon type={queueNotice[0]} message={queueNotice[1]} description={queueNotice[2]} />}
            <Space wrap>{!customerMode && <><Tag color={view.color}>{view.label}</Tag><Typography.Text code>{plan.plan_id}</Typography.Text></>}
	              {executionInProgress(plan) && <Tag color="processing">{plan.execution_queue_state === "queued" ? "已入队" : "队列运行中"}</Tag>}
              {plan.reused && <Tag>复用已有清单</Tag>}
              <Button icon={<ReloadOutlined />} disabled={busy} onClick={() => readPlan(plan.plan_id, { forApproval: approvalMode })}>刷新状态</Button>
              {!terminalPlan && !hasDurableExecution(plan) && <Button icon={<SafetyCertificateOutlined />} loading={validating} disabled={busy || approvalMode} onClick={validatePlan}>执行前复核</Button>}
              {!uncertain && !approvalMode && actorId === plan.requested_by
	                && !hasDurableExecution(plan) && !assistantPlanApprovalExpired(plan)
                && ((plan.status === "validated" && plan.approval_state === "not_required")
                  || (plan.status === "approved" && plan.approval_state === "approved" && plan.approval_available === true && plan.binding_hash && plan.expires_at))
                && knownPlan(plan, capabilities, instances)
                && <Button type="primary" icon={<PlayCircleOutlined />} loading={busy} disabled={validating} onClick={() => run(plan)}>{customerMode ? "确认执行任务" : "执行已冻结清单"}</Button>}
              <Button icon={<EditOutlined />} disabled={locked || approvalMode || actorId !== plan.requested_by || !editablePlan(plan)} onClick={() => beginEdit(true)}>{customerMode ? "修改任务" : "修订清单"}</Button>
              <Button icon={<PlusOutlined />} disabled={locked || approvalMode || actorId !== plan.requested_by || plan.status !== "settled"} onClick={() => beginEdit(false)}>{customerMode ? "重新创建任务" : "重跑为新清单"}</Button>
              <Button icon={<PlusOutlined />} disabled={locked} onClick={() => { setActionError(""); setRevisionBase(null); setDraftKey(null); setTab("new"); }}>新建</Button>
            </Space>
            <Space wrap>
              {!approvalMode && actorId === plan.requested_by && plan.approval_available && plan.status === "validated" && plan.approval_state !== "revoked" && <Button icon={<SafetyCertificateOutlined />}
                disabled={locked || validating || !governedSteps(plan) || !knownPlan(plan, capabilities, instances)} onClick={() => govern("request")}>申请审批</Button>}
              {approvalMode && approval && plan.status === "awaiting_approval" && <>
                <label>批准有效期（秒） <InputNumber aria-label="批准有效期秒数" min={60} max={3600} precision={0} value={approvalSeconds} disabled={busy} onChange={setApprovalSeconds} /></label>
                <Button icon={<SafetyCertificateOutlined />} disabled={locked || !plan.approval_available || !governedSteps(plan)
                  || !knownPlan(plan, capabilities, instances) || actorId == null || actorId === plan.requested_by
                  || !Number.isInteger(approvalSeconds) || approvalSeconds < 60 || approvalSeconds > 3600} onClick={() => govern("approve")}>批准当前冻结修订</Button>
              </>}
              {plan.approval_available && ["pending", "approved"].includes(plan.approval_state) && <Button danger icon={<StopOutlined />} disabled={busy || uncertain || validating || plan.status === "settled"} onClick={() => govern("revoke")}>撤销审批</Button>}
            </Space>
            {approvalMode && <Alert type="info" showIcon message="审批读取，不提供代执行" description="审批身份与权限由服务端核验；按钮可见不代表有审批权限。" />}
            {approval && <Descriptions size="small" bordered column={1} items={[
              { key: "required", label: "所需批准数", children: approval.required_approvals },
              { key: "decisions", label: "当前修订审批记录", children: approval.decisions.length ? approval.decisions.map((decision) => <div key={decision.actor_id}>用户 {decision.actor_id} · {displayTime(decision.approved_at)} · 到期 {displayTime(decision.expires_at)}</div>) : "尚无批准" },
            ]} />}
            {customerMode && (() => {
              const targetInstance = instances.find((item) => item.id === plan.instance_id);
              const customer = customerPlanPresentation(plan, view, {
                requestPlanId: request?.planId,
                requirementText: request?.requirementText,
                targetLabel: plan.instance_id == null ? "全局范围"
                  : targetInstance ? `${targetInstance.instance_name || targetInstance.name || `实例 ${plan.instance_id}`} · ${targetInstance.db_type}`
                    : `实例 ${plan.instance_id}`,
                uncertain,
              });
              return <section className="assistant-plan-customer" aria-label="客户任务进展">
                <div className="assistant-plan-customer-brief">
                  <div>
                    <Typography.Text type="secondary">原始需求</Typography.Text>
                    <Typography.Paragraph className="assistant-plan-customer-requirement">
                      {customer.requirement || "此历史清单未关联当前入口的原始需求。"}
                    </Typography.Paragraph>
                  </div>
                  <div className="assistant-plan-customer-facts">
                    <div><Typography.Text type="secondary">任务目标</Typography.Text><strong>{customer.goal}</strong></div>
                    <div><Typography.Text type="secondary">目标范围</Typography.Text><strong>{customer.target}</strong></div>
                  </div>
                </div>
                <ol className="assistant-plan-customer-flow" aria-label="任务阶段">
                  {customer.stages.map((stage) => <li key={stage.key} data-state={stage.state}>
                    <span className="assistant-plan-customer-dot" aria-hidden="true" />
                    <div><strong>{stage.label}</strong><Typography.Text type="secondary">{stage.detail}</Typography.Text></div>
                  </li>)}
                </ol>
                <Alert showIcon type={uncertain ? "warning" : plan.status === "settled" && view.status === "failed" ? "error" : "info"}
                  message="当前状态与下一步" description={<span><Tag color={customer.statusColor}>{customer.statusLabel}</Tag>{customer.nextAction}</span>} />
                <div>
                  <Typography.Title level={5}>已识别的任务步骤</Typography.Title>
                  <Table className="assistant-plan-customer-steps" size="small" tableLayout="fixed" rowKey="seq" pagination={false}
                    dataSource={plan.steps || []} columns={[
                      { title: "步骤", dataIndex: "seq", width: 64 },
                      { title: "任务", key: "task", render: (_, row) => customer.steps.find((item) => item.seq === row.seq)?.title || `任务步骤 ${row.seq}` },
                      { title: "状态", key: "status", width: 120, render: (_, row) => { const item = customer.steps.find((entry) => entry.seq === row.seq); return <Tag color={item?.statusColor}>{item?.statusLabel || "状态待核对"}</Tag>; } },
                      { title: "结果", key: "result", width: 100, render: (_, row) => <Button type="link" disabled={approvalMode || !row.result_ref} onClick={() => setResultStep(row)}>查看结果</Button> },
                    ]} />
                </div>
              </section>;
            })()}
            {!terminalPlan && !hasDurableExecution(plan) && <Alert showIcon
              type={validationView?.phase === "ready" ? (validationView.value.can_execute ? "success" : "warning") : "info"}
              message={validationView?.phase === "ready" ? (validationView.value.can_execute ? "复核结论：当前可执行" : "复核结论：阻断")
                : validating ? "正在复核" : validationView?.phase === "error" ? "复核失败：待核对" : "执行前复核：待核对"}
              description={<Space direction="vertical" size={4}>
                {validationView?.phase === "ready" && <>
                  <Typography.Text>复核时间：{displayTime(validationView.value.checked_at)} · 清单状态：{validationView.value.plan_status}</Typography.Text>
                  {validationView.value.issues.length > 0 && <Space wrap>{validationView.value.issues.map((issue, index) => <Tag key={`${index}:${issue.code}`}>{issue.code}</Tag>)}</Space>}
                </>}
                <Typography.Text>复核不代表执行成功或执行授权；实际执行仍需服务端重查。</Typography.Text>
              </Space>} />}
            {!customerMode && <Progress percent={view.percent} status={view.status === "succeeded" ? "success" : ["failed", "unknown"].includes(view.status) ? "exception" : "normal"} showInfo={false} />}
            {!customerMode && <Alert type={view.status === "succeeded" ? "success" : "info"} showIcon message={statusSummary} description={plan.outcome_reason || undefined} />}
            {approvalMode
              ? <Alert type="info" showIcon message="审批视图不展示执行结果，请由请求人在历史清单查看。" />
              : ["empty", "unsupported", "partially_supported"].includes(view.resultStatus)
                && <Alert type={view.resultStatus === "empty" ? "info" : "warning"} showIcon message={view.resultSummary} />}
            {customerMode ? <details className="assistant-plan-technical-details">
              <summary>技术详情</summary>
              <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
                { key: "plan", label: "清单号", children: <Typography.Text code>{plan.plan_id}</Typography.Text> },
                { key: "capability", label: "能力 ID / 版本", children: `${plan.capability_id} · v${plan.definition_version}` },
                { key: "revision", label: "修订", children: plan.revision ?? "历史记录未提供" },
                { key: "expires", label: "有效期至（TTL）", children: displayTime(plan.expires_at) },
                { key: "binding", label: "冻结摘要", children: <Typography.Text code className="assistant-plan-binding">{plan.binding_hash || "历史记录未提供"}</Typography.Text> },
                { key: "approval", label: "审批状态", children: plan.approval_state },
                { key: "outcome", label: "业务结果", children: plan.outcome || "尚无终态结论" },
                ...(hasDurableExecution(plan) ? [
                  { key: "executionBatch", label: "执行批次", children: <Typography.Text code>{plan.execution_batch_id}</Typography.Text> },
                  { key: "executionQueue", label: "队列状态", children: plan.execution_queue_state },
                ] : []),
              ]} />
            </details> : <>
            <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
              { key: "capability", label: "能力", children: `${NAMES[plan.capability_id] || plan.capability_id} · v${plan.definition_version}` },
              { key: "target", label: "目标实例", children: plan.instance_id ?? "全局" },
              { key: "requester", label: "请求人", children: plan.requested_by },
              { key: "engine", label: "目标引擎", children: plan.db_type || "全局" },
              { key: "revision", label: "当前修订", children: plan.revision ?? "历史记录未提供" },
              { key: "expires", label: "清单有效期至（TTL）", children: displayTime(plan.expires_at) },
              { key: "binding", label: "完整冻结摘要", children: <Typography.Text code className="assistant-plan-binding">{plan.binding_hash || "历史记录未提供"}</Typography.Text> },
              { key: "approval", label: "审批状态", children: plan.approval_state },
              { key: "approver", label: "最后批准人", children: display(plan.approved_by) },
              { key: "approvalExpiry", label: "批准有效期至", children: displayTime(plan.approval_expires_at) },
              { key: "policy", label: "失败策略", children: plan.failure_policy || "未提供" },
              { key: "created", label: "创建时间", children: displayTime(plan.created_at) },
              { key: "outcome", label: "业务结果", children: plan.outcome || "尚无终态结论" },
	              ...(hasDurableExecution(plan) ? [
	                { key: "executionBatch", label: "执行批次", children: <Typography.Text code>{plan.execution_batch_id}</Typography.Text> },
	                { key: "executionQueue", label: "队列状态", children: plan.execution_queue_state },
	              ] : []),
            ]} />
            <Table size="small" tableLayout="fixed" rowKey="seq" pagination={false} scroll={{ x: 1180 }} dataSource={plan.steps || []} columns={[
              { title: "步骤", dataIndex: "seq", width: 60 }, { title: "任务", dataIndex: "title", width: 220, render: (value, row) => NAMES[row.capability_id] || value },
              { title: "状态", dataIndex: "status", width: 100, render: (_, row) => view.steps.find((item) => item.seq === row.seq)?.label || "未知" },
              { title: "结果展示", key: "resultSupport", width: 150, render: (_, row) => {
                if (approvalMode) return <Typography.Text type="secondary">审批视图不展示</Typography.Text>;
                const item = view.steps.find((entry) => entry.seq === row.seq);
                const labels = { available: "可查看", empty: "无上下文", unsupported: "格式暂不支持", not_applicable: "无结果" };
                return <Typography.Text type={item?.resultStatus === "unsupported" ? "warning" : "secondary"}>{labels[item?.resultStatus] || "待核对"}</Typography.Text>;
              } },
              { title: "冻结参数", dataIndex: "params", width: 200, render: display },
              { title: "能力 / 版本", key: "definition", width: 240, render: (_, row) => `${row.capability_id} · v${row.definition_version}` },
              { title: "前置依赖", dataIndex: "depends_on", width: 100, render: display },
              { title: "字段引用", dataIndex: "input_refs", width: 160, render: display },
              { title: "结果", key: "result", width: 100, render: (_, row) => <Button type="link" disabled={approvalMode || !row.result_ref} onClick={() => setResultStep(row)}>查看结果</Button> },
            ]} />
            </>}
          </div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未选择清单" /> },
        ]} />
      </div>
    </Modal>
    {open && resultStep && <Modal open centered title={resultStep.capability_id === "diagnosis.root_cause.execute"
      ? "根因分析" : resultStep.capability_id === "report.document.generate" ? "报告结果"
        : GOVERNED_EFFECTS[resultStep.capability_id] ? `${NAMES[resultStep.capability_id] || "任务"}结果` : `步骤结果 · ${resultStep.title || ""}`} width={1000} onCancel={() => setResultStep(null)}
      footer={<Button onClick={() => setResultStep(null)}>关闭</Button>} className="assistant-plan-modal">
      <InlineResult plan={plan} step={resultStep} isActive={() => visible.current && isAuthSessionCurrent(dataSession.current) && activePlanId.current === plan.plan_id}
        onIncidentReady={onIncidentReady} />
    </Modal>}
    <AssistantBatchWorkspace open={open && batchOpen} onClose={() => setBatchOpen(false)} instances={instances}
      initialBatchId={batchInitial}
      onOpenPlan={(id) => { setBatchOpen(false); setTab("result"); readPlan(id); }} />
    <AssistantWorkflowWorkspace open={open && workflowOpen} onClose={() => setWorkflowOpen(false)} instances={instances}
      initialPlan={workflowSource} initialAction={workflowAction}
      onOpenPlan={(id) => { setWorkflowOpen(false); setTab("result"); readPlan(id); }} />
    <AssistantGenericCapabilityPanel open={open && genericOpen} onClose={() => setGenericOpen(false)}
      capabilities={capabilities} instances={instances}
      initialCapabilityId={genericInitial?.capabilityId}
      initialInstanceId={genericInitial?.instanceId}
      initialParams={genericInitial?.params}
      onOpenPlan={(id) => { setGenericOpen(false); setTab("result"); readPlan(id); }} />
  </>;
}
