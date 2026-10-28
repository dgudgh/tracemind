import { Tag } from "antd";

export function dataOpsStatusTag(value) {
  const normalized = String(value || "info").toLowerCase();
  const color = {
    critical: "red",
    high: "volcano",
    warning: "orange",
    warn: "orange",
    medium: "gold",
    low: "blue",
    normal: "green",
    success: "green",
    healthy: "green",
    open: "red",
    closed: "green",
    error: "red",
    failed: "red",
  }[normalized] || "default";

  return <Tag color={color}>{value || "-"}</Tag>;
}

export function dataOpsUsageColor(pct) {
  return pct >= 95 ? "#cf1322" : pct >= 85 ? "#fa8c16" : pct >= 70 ? "#fadb14" : "#3f8600";
}

export function dataOpsLevelTag(level) {
  return <Tag color={level === "critical" ? "red" : level === "warning" ? "gold" : "green"}>{(level || "normal").toUpperCase()}</Tag>;
}

export function mbToGb(value) {
  return parseFloat((Number(value || 0) / 1024).toFixed(2));
}

export function partitionIntervalDefault(unit) {
  return unit === "DAY" ? 7 : 1;
}
