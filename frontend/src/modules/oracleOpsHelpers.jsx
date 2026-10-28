import { Tag } from "antd";

export function normalizeDgMessage(text) {
  if (!text) return text;
  const value = String(text);
  if (value.includes("检测到主库 Data Guard 归档传输目标") || value.includes("至少一个备库归档传输目标状态为 VALID")) {
    return "DG 已启动并应用";
  }
  return text;
}

export function oracleDgRiskColor(risk) {
  if (risk === "critical") return "#cf1322";
  if (risk === "warning") return "#d46b08";
  return "#3f8600";
}

export function oracleDgStatusTag(value) {
  const text = value || "-";
  const upper = String(text).toUpperCase();
  const color = upper === "VALID" || upper === "OPEN" || upper === "NORMAL" || upper === "APPLYING_LOG" || upper === "WAIT_FOR_LOG" ? "green"
    : upper.includes("ERROR") || upper.includes("FAIL") ? "red"
    : upper === "-" ? "default" : "orange";
  return <Tag color={color}>{text}</Tag>;
}
