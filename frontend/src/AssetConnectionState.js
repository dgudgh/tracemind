const parsedTime = (value) => {
  const timestamp = Date.parse(value || "");
  return Number.isFinite(timestamp) ? timestamp : null;
};

export const connectionTestIsCurrent = (item) => {
  const attributes = item?.attributes || {};
  if (!["success", "failed"].includes(attributes.connection_test_status)) return false;
  const testedAt = parsedTime(attributes.last_connection_test_at);
  const discoveredAt = parsedTime(attributes.last_discovery_at || item?.last_seen_at);
  return testedAt !== null && (discoveredAt === null || testedAt >= discoveredAt);
};

export const successfulConnectionAfterDiscovery = (item) => {
  const attributes = item?.attributes || {};
  return ["failed", "partial"].includes(attributes.discovery_status)
    && attributes.connection_test_status === "success"
    && connectionTestIsCurrent(item);
};

export const failedConnectionAfterDiscovery = (item) => (
  item?.attributes?.connection_test_status === "failed"
  && connectionTestIsCurrent(item)
);

export const discoveryError = (item) => {
  const attributes = item?.attributes || {};
  if (failedConnectionAfterDiscovery(item)) {
    return attributes.connection_test_error || "最近一次数据库连接测试失败";
  }
  if (["failed", "partial"].includes(attributes.discovery_status) && !successfulConnectionAfterDiscovery(item)) {
    return attributes.discovery_error || "实时架构探测失败";
  }
  return "";
};

export const effectiveHealthStatus = (item) => {
  if (failedConnectionAfterDiscovery(item)) return "warning";
  if (successfulConnectionAfterDiscovery(item)) return "pending_confirmation";
  return item?.status || "unknown";
};
