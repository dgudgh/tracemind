const normalize = (value) => String(value || "").trim().toLowerCase();

export const INSPECTION_STATUS_TEXT = {
  success: "执行成功", failed: "采集失败", partial: "部分完成", empty: "无有效结果",
  unknown: "未确认", running: "运行中", pending: "待执行", skipped: "未执行",
  canceled: "已取消", plan_only: "仅生成方案",
};

function executionStatus(value) {
  const status = normalize(value);
  if (["error", "failure", "collection_failed", "connection_failed"].includes(status)) return "failed";
  if (["ok", "completed", "succeeded"].includes(status)) return "success";
  if (["not_executed", "not_supported", "not_applicable"].includes(status)) return "skipped";
  if (status === "cancelled") return "canceled";
  return status;
}

export function inspectionOutcome(input = {}) {
  const data = input || {};
  const task = data.task || data;
  const sources = [task, data.report, data.summary, data].filter(Boolean);
  const statuses = sources.flatMap((source) => [source.task_status, source.execution_status, source.status])
    .map(executionStatus).filter(Boolean);
  const error = sources.map((source) => source.error_message || source.error).find(Boolean);
  const reason = error || sources.map((source) => source.failure_reason).find(Boolean) || "";
  // A successful HTTP response or a score is not proof that collection completed.
  let status = error || statuses.includes("failed") ? "failed"
    : ["partial", "canceled", "skipped", "plan_only", "running", "pending", "empty", "unknown"]
      .find((value) => statuses.includes(value)) || (statuses.includes("success") ? "success" : "unknown");
  const findings = data.all_findings ?? data.findings;
  const totals = [task.total_count, data.summary?.total].filter((value) => value != null && value !== "");
  if (status === "success" && (totals.some((value) => Number(value) === 0) || (Array.isArray(findings) && !findings.length))) {
    status = "empty";
  }
  if (["success", "unknown"].includes(status) && Array.isArray(findings) && findings.length) {
    const evidenceStatuses = findings.map((item) => evidenceExecutionStatus(item,
      item.evidence || item.raw_data?.raw_data || item.raw_data || {}));
    if (evidenceStatuses.every((value) => value === "skipped")) status = "empty";
    else if (evidenceStatuses.includes("partial")) status = "partial";
    else if (evidenceStatuses.includes("failed")) {
      status = evidenceStatuses.every((value) => ["failed", "skipped"].includes(value)) ? "failed" : "partial";
    }
  }
  const rawScore = task.overall_score ?? task.score ?? data.summary?.score;
  const score = status === "success" && rawScore != null && String(rawScore).trim() !== ""
    && Number.isFinite(Number(rawScore)) && Number(rawScore) >= 0 && Number(rawScore) <= 100
    ? Number(rawScore) : null;
  return {
    status, score, reason: String(reason),
    label: INSPECTION_STATUS_TEXT[status] || INSPECTION_STATUS_TEXT.unknown,
    complete: status === "success",
  };
}

export function inspectionNotice(input, templateName = "自动匹配模板") {
  const outcome = inspectionOutcome(input);
  return {
    type: outcome.complete ? "success" : outcome.status === "failed" ? "error" : "warning",
    message: outcome.complete ? `巡检完成：${templateName}`
      : `巡检${outcome.label}：${outcome.reason || "未获得完整有效的检查结果，请查看报告"}`,
  };
}

export function evidenceSqlList(payload = {}) {
  return (Array.isArray(payload.sql) ? payload.sql : [payload.sql])
    .filter((sql) => typeof sql === "string" && sql.trim()).map((sql) => sql.trim());
}

export function evidenceExecutionStatus(item, payload = {}) {
  const state = executionStatus(payload.state);
  const execution = executionStatus(payload.execution_status);
  const status = executionStatus(payload.execution_status || payload.status);
  if ([state, execution, executionStatus(payload.status)].includes("failed") || payload.error || payload.error_message) return "failed";
  if (payload.executed === false || ["skipped", "pending", "plan_only", "canceled"].includes(state)) return "skipped";
  if (["skipped", "pending", "plan_only", "canceled"].includes(status)) return "skipped";
  if (state === "partial" || status === "partial") return "partial";
  if (["unknown", "running"].includes(execution)) return execution;
  if (payload.evidence_kind === "execution_summary") return "summary";
  if (normalize(payload.evidence_source).startsWith("legacy_")) return "unknown";
  if (state === "collected" || execution === "success") return "success";
  if (["unknown", "running"].includes(state)) return state;
  // Risk severity describes the observation, never whether its SQL ran.
  if (evidenceSqlList(payload).length && ["normal", "success"].includes(status)) return "success";
  if (["critical", "warning"].includes(status)) return status;
  return "unknown";
}

export function evidenceEmptyText(status, payload, item, hasCommand) {
  const detail = payload.message || payload.error_message || item?.finding || "未提供原因";
  if (status === "skipped") return `本项未执行：${detail}`;
  if (status === "partial") return `未取得完整查询结果：${detail}`;
  if (["failed", "error", "critical", "warning"].includes(status)) return `未取得查询结果：${detail}`;
  if (status === "summary") return "暂无执行汇总明细";
  if (status === "success" && hasCommand) {
    if (Number(payload.row_count) > 0) return `查询返回 ${payload.row_count} 行，未保存可展示明细`;
    if (payload.row_count === 0 || (Array.isArray(payload.rows) && payload.rows.length === 0)
      || (Array.isArray(payload.items) && payload.items.length === 0)) return "查询完成，返回 0 行";
  }
  return "暂无采集明细";
}
