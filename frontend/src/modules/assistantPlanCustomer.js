const CAPABILITY_COPY = {
  "inspection.run.query": ["读取巡检记录", "查看已保存的巡检任务与状态"],
  "inspection.template.read": ["读取巡检策略", "查看可用于当前任务的巡检策略"],
  "operations.capability_matrix.read": ["核对可用能力", "核对目标环境当前可用的运维能力"],
  "diagnosis.investigation.read": ["读取已有诊断上下文", "读取与现场问题相关的已有诊断上下文"],
  "diagnosis.root_cause.execute": ["执行根因分析", "定位问题根因并形成可核验的诊断结论"],
  "inspection.run.execute": ["执行数据库巡检", "执行已确认的巡检任务并保存结果"],
  "performance.lock.read": ["查询锁等待", "查询目标实例的锁等待与阻塞情况"],
  "performance.slow_sql.read": ["读取慢 SQL 证据", "查看目标实例的慢 SQL 证据"],
  "report.catalog.read": ["查询报告记录", "查看已保存的报告记录"],
  "report.document.generate": ["生成分析报告", "按已确认的报告类型和时间范围生成报告"],
  "monitor.alert.sync": ["导入告警证据", "导入已授权范围内的容量与外部告警证据"],
  "monitor.capacity.execute": ["采集容量快照", "采集目标实例的容量快照并保存证据"],
  "monitor.observability.bind": ["绑定可观测数据源", "自动匹配并保存可观测数据源绑定"],
  "performance.workload.collect": ["采集工作负载报告", "采集目标实例的工作负载证据"],
  "performance.sql_profile.collect": ["采集 SQL 画像", "采集目标实例的 SQL 画像证据"],
  "performance.index.collect": ["采集索引发现", "采集目标实例的索引发现证据"],
  "performance.table_profile.collect": ["采集表画像", "采集目标实例的表画像证据"],
  "performance.baseline.collect": ["采集性能基线", "采集目标实例的性能基线证据"],
  "performance.capacity.collect": ["采集容量证据", "采集目标实例的容量证据"],
  "operations.startup.plan": ["生成启动方案", "生成 Oracle 启动操作方案供复核"],
  "operations.shutdown.plan": ["生成停库方案", "生成 Oracle 停库操作方案供复核"],
  "operations.switchover.plan": ["生成切换方案", "生成 Data Guard 切换检查与操作方案供复核"],
  "governance.candidate.scan": ["扫描治理候选", "扫描目标实例的治理候选并保存结果"],
  "protection.backup.plan": ["生成备份方案", "生成备份操作方案供复核"],
  "protection.restore.plan": ["生成恢复演练方案", "生成恢复演练操作方案供复核"],
  "integration.zabbix.sync": ["同步 Zabbix 证据", "同步 Zabbix 主机与问题记录"],
  "integration.prometheus.sync": ["同步 Prometheus 指标", "同步 Prometheus 标准指标记录"],
  "asset.inventory.read": ["查询资产清单", "查看授权范围内的资产与实例"],
  "protection.backup_center.read": ["查询备份状态", "查看备份状态与恢复就绪记录"],
};

const cleanText = (value, limit) => typeof value === "string" ? value.trim().slice(0, limit) : "";
export function customerCapabilityTitle(capabilityId) {
  return CAPABILITY_COPY[capabilityId]?.[0] || "通用任务";
}
const QUEUE_STATUS = {
  queued: ["排队中", "processing"],
  running: ["执行中", "processing"],
  reconciliation_required: ["结果待核对", "warning"],
  paused: ["已暂停", "warning"],
  blocked: ["已阻断", "warning"],
  cancelled: ["已取消", "default"],
  expired: ["已过期", "warning"],
  stopped: ["已停止", "default"],
};

// Conservative client presentation/guard; the server remains authoritative at execution time.
export function assistantPlanApprovalExpired(plan, now = Date.now()) {
  if (plan?.status !== "approved" || plan.approval_state !== "approved"
    || typeof plan.approval_expires_at !== "string") return false;
  const deadline = Date.parse(plan.approval_expires_at);
  return Number.isFinite(deadline) && deadline <= now;
}

function stage(key, label, state, detail) {
  return { key, label, state, detail };
}

function approvalStage(plan) {
  if (plan.approval_state === "not_required") return stage("approval", "审批", "muted", "本任务无需审批");
  if (assistantPlanApprovalExpired(plan)) return stage("approval", "审批", "warning", "审批已过期，需先修订任务");
  if (plan.approval_state === "expired") return stage("approval", "审批", "warning", "审批已过期");
  if (plan.approval_state === "revoked") return stage("approval", "审批", "warning", "审批已撤销，需先修订任务");
  if (plan.approval_state === "rejected") return stage("approval", "审批", "warning", "审批未通过");
  if (plan.approval_state === "approved") return stage("approval", "审批", "done", "审批已通过");
  if (plan.status === "awaiting_approval" && plan.approval_state === "pending") return stage("approval", "审批", "current", "等待授权用户审批");
  if (plan.status === "validated" && plan.approval_state === "pending") return stage("approval", "审批", "current", "等待请求人发起审批");
  if (plan.status === "draft" && plan.approval_state === "pending") return stage("approval", "审批", "pending", "保存任务后可发起审批");
  return stage("approval", "审批", "pending", "尚未进入审批");
}

function executionStage(plan) {
  const queueState = plan.execution_queue_state;
  if (queueState === "queued") return stage("execution", "执行", "current", "已进入持久队列");
  if (queueState === "running") return stage("execution", "执行", "current", "工作节点正在执行");
  if (queueState === "reconciliation_required") return stage("execution", "执行", "warning", "队列结果需要人工核对");
  if (queueState === "paused") return stage("execution", "执行", "warning", "队列执行已暂停");
  if (["blocked", "cancelled", "expired", "stopped"].includes(queueState)) {
    return stage("execution", "执行", "warning", "队列未继续执行");
  }
  if (queueState === "archived" && plan.status !== "settled") return stage("execution", "执行", "warning", "队列已归档，计划状态待核对");
  if (plan.status === "settled") {
    const state = plan.outcome === "failed" ? "error"
      : plan.outcome === "succeeded" ? "done" : "warning";
    return stage("execution", "执行", state, "服务端已记录终态");
  }
  if (queueState) return stage("execution", "执行", "warning", "队列状态需要核对");
  if (plan.status === "running") return stage("execution", "执行", "current", "工作节点正在执行");
  return stage("execution", "执行", "pending", "尚未开始执行");
}

function resultStage(plan, presentation) {
  if (plan.status !== "settled") return stage("result", "结果", "pending", "等待执行结果");
  const state = presentation.status === "succeeded" ? "done"
    : presentation.status === "failed" ? "error"
      : ["partial", "unknown", "blocked", "expired", "needs_input"].includes(presentation.status) ? "warning" : "muted";
  return stage("result", "结果", state, presentation.label || "结果待核对");
}

function nextAction(plan, presentation, uncertain) {
  if (uncertain) return "请求结果尚未确认，请刷新当前清单核对；系统不会自动重复提交。";
  const queueState = plan.execution_queue_state;
  if (queueState === "queued") return "任务已进入队列，请等待工作节点处理；无需重复提交。";
  if (queueState === "running") return "任务正在执行，页面会继续读取真实状态。";
  if (queueState === "reconciliation_required") return "队列结果需要人工核对，请刷新当前清单并按执行批次确认。";
  if (queueState === "paused") return "队列执行已暂停，请核对暂停原因和后续处理。";
  if (["blocked", "cancelled", "expired", "stopped"].includes(queueState)) return "队列没有继续执行，请刷新当前清单并核对执行批次状态。";
  if (queueState === "archived" && plan.status !== "settled") return "队列已归档，但计划尚无终态，请刷新当前清单核对。";
  if (queueState === "archived" && plan.status === "settled") return presentation.summary || "任务已结束，请查看保存的结果。";
  if (queueState) return "队列状态需要核对，请刷新当前清单确认。";
  if (plan.status === "running") return "任务正在执行，页面会继续读取真实状态。";
  if (assistantPlanApprovalExpired(plan)) return "审批已过期，请先修订任务，再申请审批。";
  if (plan.status === "awaiting_approval" && plan.approval_state === "pending") return "等待授权用户审批当前任务。";
  if (plan.status === "validated" && plan.approval_state === "pending") return "请由请求人发起审批，审批通过后才能执行。";
  if (plan.status === "draft" && plan.approval_state === "pending") return "请先完善并保存任务，再发起审批。";
  if (plan.approval_state === "expired") return "审批已过期，请由请求人重新发起审批。";
  if (plan.approval_state === "revoked") return "审批已撤销，请先修订任务，再申请审批。";
  if (plan.approval_state === "rejected") return "审批未通过，请由请求人核对后重新发起。";
  if (plan.status === "settled") return presentation.summary || "任务已结束，请核对保存的结果。";
  if (plan.status === "approved") return "审批已通过，等待请求人确认执行。";
  if (plan.status === "validated") return "任务已识别，等待请求人复核并确认执行。";
  if (plan.status === "draft") return "任务仍是草稿，请完善并保存后继续。";
  return "当前状态需要核对，请刷新任务状态。";
}

export function customerPlanPresentation(plan, presentation, {
  requestPlanId = "", requirementText = "", targetLabel = "", uncertain = false,
} = {}) {
  if (!plan || typeof plan !== "object" || !presentation || typeof presentation !== "object") return null;
  const copy = CAPABILITY_COPY[plan.capability_id];
  const matchedRequirement = typeof requestPlanId === "string" && requestPlanId === plan.plan_id;
  const viewSteps = Array.isArray(presentation.steps) ? presentation.steps : [];
  const rawSteps = Array.isArray(plan.steps) ? plan.steps : [];
  const queueStatus = QUEUE_STATUS[plan.execution_queue_state]
    || (plan.execution_queue_state && plan.execution_queue_state !== "archived" ? ["状态待核对", "warning"] : null)
    || (plan.execution_queue_state === "archived" && plan.status !== "settled" ? ["状态待核对", "warning"] : null);
  return {
    requirement: matchedRequirement ? cleanText(requirementText, 2000) : "",
    requirementMatched: matchedRequirement,
    goal: copy?.[1] || "完成已识别的任务清单并保存真实结果",
    target: cleanText(targetLabel, 200) || (plan.instance_id == null ? "全局范围" : `实例 ${plan.instance_id}`),
    statusLabel: queueStatus?.[0] || cleanText(presentation.label, 80) || "状态待核对",
    statusColor: queueStatus?.[1] || cleanText(presentation.color, 24) || "default",
    nextAction: nextAction(plan, presentation, uncertain),
    stages: [stage("identified", "任务识别", "done", "已生成任务清单"), approvalStage(plan),
      executionStage(plan), resultStage(plan, presentation)],
    steps: rawSteps.slice(0, 20).map((item, index) => {
      const state = viewSteps.find((candidate) => candidate.seq === item.seq) || viewSteps[index] || {};
      return {
        seq: Number.isInteger(item.seq) && item.seq > 0 ? item.seq : index + 1,
        title: item.capability_id === "report.document.generate"
          ? (item.params?.report_type === "after_sales" ? "生成售后分析报告" : "生成统一运维报告")
          : CAPABILITY_COPY[item.capability_id]?.[0] || `任务步骤 ${index + 1}`,
        statusLabel: cleanText(state.label, 80) || "状态待核对",
        statusColor: cleanText(state.color, 24) || "default",
        resultStatus: cleanText(state.resultStatus, 40) || "not_applicable",
      };
    }),
  };
}
