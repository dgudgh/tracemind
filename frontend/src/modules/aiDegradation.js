// One closed vocabulary for AI degradation on the client; mirrors backend ai_degradation.
const OK = "ok";

const MESSAGES = {
  not_configured: "未配置 AI 模型：以下内容仅为本地规则结果，不是模型结论。",
  model_failed: "AI 模型调用失败：以下内容仅为本地规则结果，不是模型结论。",
  output_rejected: "AI 输出未通过校验并被丢弃：以下内容仅为本地规则结果。",
  not_sent: "本次未向模型发送证据：以下内容仅为本地规则结果。",
};

// Mirrors backend ai_degradation.MANUAL_STEPS: the model-free path for each degraded entry.
const MANUAL_HINTS = {
  "diagnosis.evidence_review": "可按本地规则结果与原始证据人工核对，不依赖模型。",
  "awr.report_read": "可直接阅读原始 AWR 报告，不依赖模型。",
  "slow_sql.raw_list": "可回到慢 SQL 列表按原始记录判断，不依赖模型。",
  "operations.capability_matrix": "可查看引擎能力矩阵后手工选择操作，不依赖模型。",
  "workflow.manual_draft": "可在固定流程库手工填参，不依赖模型。",
};

export function manualHint(manual) {
  if (!manual || typeof manual !== "object" || manual.required !== true) return null;
  return MANUAL_HINTS[manual.step] ?? null;
}

export function degradationBanner(degradation, manual) {
  if (!degradation || typeof degradation !== "object") return null;
  const status = String(degradation.status || "");
  if (!status || status === OK) return null;
  const text = MESSAGES[status];
  if (!text) return null;
  return { tone: status === "not_configured" ? "info" : "warning", text, hint: manualHint(manual) };
}
