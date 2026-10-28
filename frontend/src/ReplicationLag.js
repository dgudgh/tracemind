const metricValue = (item, key) => (
  item?.[key] ?? item?.attributes?.[key] ?? null
);

const finiteSeconds = (value) => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : null;
};

export const replicationLagMetric = (item, key) => metricValue(item, key);

export const replicationLagSeconds = (item) => {
  const combined = finiteSeconds(metricValue(item, "replication_delay_seconds"));
  if (combined !== null) return combined;
  const values = [
    finiteSeconds(metricValue(item, "transport_lag_seconds")),
    finiteSeconds(metricValue(item, "apply_lag_seconds")),
  ].filter((value) => value !== null);
  return values.length ? Math.max(...values) : null;
};

export const hasReplicationLagData = (item) => (
  replicationLagSeconds(item) !== null
  || ["transport_lag", "apply_lag", "apply_finish_time"].some(
    (key) => ![null, undefined, ""].includes(metricValue(item, key)),
  )
);

export const formatReplicationLagSeconds = (value, emptyText = "未采集") => {
  const seconds = finiteSeconds(value);
  if (seconds === null) return emptyText;
  if (seconds < 60) {
    const precision = seconds > 0 && seconds < 1 ? 2 : seconds % 1 ? 1 : 0;
    return `${seconds.toFixed(precision)} 秒`;
  }
  if (seconds < 3600) {
    const minutes = seconds / 60;
    return `${minutes.toFixed(minutes % 1 ? 1 : 0)} 分钟`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
};

export const replicationLagTone = (item) => {
  const seconds = replicationLagSeconds(item);
  if (seconds === null) return "default";
  if (seconds <= 5) return "green";
  if (seconds <= 30) return "orange";
  return "red";
};
