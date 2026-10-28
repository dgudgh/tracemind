import { api, buildQuery } from "../api";

export const listInspectionTasks = (params = {}) =>
  api.get(`/inspection/tasks${buildQuery(params)}`);

export const getInspectionTask = (taskId, params = {}) =>
  api.get(`/inspection/tasks/${taskId}${buildQuery(params)}`);

export const runInspectionTask = (engine, instanceId, policyKey = null) =>
  api.post(`/inspection/${engine}/${instanceId}/run`, policyKey ? { policy_key: policyKey } : {});

export const testInspectionCustomMetric = (payload) =>
  api.post("/inspection/custom-metrics/test", payload);

export const listInspectionPolicies = (params = {}) =>
  api.get(`/inspection/policies${buildQuery(params)}`);

export const createInspectionPolicy = (payload) =>
  api.post("/inspection/policies", payload);

export const updateInspectionPolicy = (policyId, payload) =>
  api.put(`/inspection/policies/${policyId}`, payload);

export const cloneInspectionPolicy = (policyId, payload) =>
  api.post(`/inspection/policies/${policyId}/clone`, payload);

export const deleteInspectionPolicy = (policyId) =>
  api.delete(`/inspection/policies/${policyId}`);

export const exportInspectionTemplate = (policyId) =>
  api.downloadFile(`/inspection/policies/${policyId}/export`);

export const exportInspectionTemplates = (policyIds) =>
  api.downloadFile("/inspection/policies/export", null, {
    method: "POST",
    body: JSON.stringify({ policy_ids: policyIds }),
  });

function inspectionTemplateForm(files, selectedIds = null) {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file, file.name));
  selectedIds?.forEach((selectionId) => formData.append("selected_ids", selectionId));
  return formData;
}

export const previewInspectionTemplateImport = (files) =>
  api.postForm("/inspection/policies/import/preview", inspectionTemplateForm(files));

export const importInspectionTemplates = (files, selectedIds) =>
  api.postForm("/inspection/policies/import", inspectionTemplateForm(files, selectedIds));

export const getOracleSlowSqlRealtime = (instanceId, params = {}, options = {}) =>
  api.get(`/oracle/${instanceId}/slow-sql-realtime${buildQuery(params)}`, options);

export const getOracleSqlDetail = (instanceId, sqlId, identity = {}, options = {}) =>
  api.get(`/oracle/${instanceId}/sql-detail${buildQuery({ sql_id: sqlId, child_number: identity.child_number, con_id: identity.con_id, plan_hash_value: identity.plan_hash_value })}`, options);

export const collectSlowSql = (instanceId, params = {}) =>
  api.post(`/slow-sql/collect/${instanceId}${buildQuery(params)}`);

export const getSlowSqlAiCapability = (options = {}) => api.get("/slow-sql/ai-capability", options);
export const listSlowSqlAiReports = (instanceId, sqlId, options = {}) =>
  api.get(`/slow-sql/analysis-reports${buildQuery({ instance_id: instanceId, sql_id: sqlId, limit: 10 })}`, options);
export const analyzeOracleSql = (instanceId, payload, options = {}) =>
  api.request(`/slow-sql/oracle/${instanceId}/analyze`, { ...options, method: "POST", body: JSON.stringify(payload) });
