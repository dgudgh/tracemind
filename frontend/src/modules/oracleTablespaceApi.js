import { api, buildQuery } from "../api";

export const getTablespaceOverview = (instanceId) =>
  api.get(`/oracle/${instanceId}/tablespace-overview`);

export const getTablespaceDatafiles = (instanceId, tablespaceName) =>
  api.get(`/oracle/${instanceId}/tablespace/${tablespaceName}/datafiles`);

export const buildTablespaceExpandPlan = (instanceId, values) => {
  const params = {
    tablespace_name: values.tablespace_name,
    datafile_path: values.datafile_path,
    file_count: values.file_count || 1,
    size_mb: values.size_mb,
    autoextend: !!values.autoextend,
    next_mb: values.autoextend ? values.next_mb : undefined,
    maxsize_mb: values.autoextend ? values.maxsize_mb : undefined,
  };
  return api.post(`/oracle/${instanceId}/tablespace-expand-script${buildQuery(params)}`);
};

export const getTablespaceCleanupSummary = (instanceId, cleanupType) => {
  const path = cleanupType === "audit" ? "system-audit" : "sysaux-awr";
  return api.get(`/oracle/${instanceId}/tablespaces/${path}/summary`);
};

export const buildAuditCleanupPlan = (instanceId, values) =>
  api.post(`/oracle/${instanceId}/tablespaces/system-audit/cleanup-plan`, {
    retention_days: values.retention_days,
    include_fga: !!values.include_fga,
    include_location_plan: !!values.include_location_plan,
    audit_trail_location: values.audit_trail_location || "SYSAUX",
  });

export const buildAwrRetentionPlan = (instanceId, values) =>
  api.post(`/oracle/${instanceId}/tablespaces/sysaux-awr/retention-plan`, {
    retention_days: values.retention_days,
    interval_minutes: values.interval_minutes || 60,
  });

export const buildAwrCleanupPlan = (instanceId, values) =>
  api.post(`/oracle/${instanceId}/tablespaces/sysaux-awr/cleanup-plan`, {
    retention_days: values.retention_days,
  });
