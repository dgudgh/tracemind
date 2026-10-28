const GENERATED_CLUSTER_SUFFIXES = [
  "SQL Server Always On 集群",
  "SQL Server Always On",
  "PostgreSQL 流复制集群",
  "PostgreSQL 流复制",
  "Oracle Data Guard 集群",
  "Oracle Data Guard",
  "Data Guard 集群",
  "Data Guard",
  "Oracle RAC 集群",
  "RAC 集群",
  "MySQL MGR 集群",
  "MGR 集群",
  "Always On 集群",
  "Always On",
  "主从复制集群",
  "数据库集群",
  "流复制集群",
  "分片集群",
  "复制拓扑",
  "单机集群",
  "主从复制",
  "流复制",
];

const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function formatClusterName(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";

  for (const suffix of GENERATED_CLUSTER_SUFFIXES) {
    const compact = raw
      .replace(new RegExp(`\\s*${escapeRegExp(suffix)}$`, "i"), "")
      .replace(/[\s·/_-]+$/, "")
      .trim();
    if (compact && compact !== raw) return compact;
  }
  return raw;
}

export function formatClusterOption(cluster, architectureLabel) {
  return [
    cluster?.db_type || "Unknown",
    formatClusterName(cluster?.name) || "未命名集群",
    architectureLabel || cluster?.architecture || "架构待确认",
  ].join(" · ");
}
