import { api, buildQuery } from "../api";

export const getUnifiedReports = (reportType = "all", limit = 200) =>
  api.get(`/reports/unified${buildQuery({ report_type: reportType, limit })}`);

export const getUnifiedReportStats = () => api.get("/reports/unified/stats");

export const syncReportCatalog = (limit = 500) =>
  api.post(`/reports/catalog/sync${buildQuery({ limit })}`, {});

export const getReportCatalog = (params = {}) =>
  api.get(`/reports/catalog${buildQuery({
    report_type: params.reportType || "all",
    keyword: params.keyword,
    instance_id: params.instanceId,
    archive_state: params.archiveState || "all",
    date_from: params.dateFrom,
    date_to: params.dateTo,
    refresh_sources: params.refreshSources ?? false,
    limit: params.limit || 500,
  })}`);

export const getReportCatalogStats = () => api.get("/reports/catalog/stats");
export const getReportArtifact = (artifactId) => api.get(`/reports/catalog/${artifactId}`);
export const verifyReportArtifact = (artifactId) => api.get(`/reports/catalog/${artifactId}/verify`);
export const createReportSnapshot = (payload) => api.post("/reports/catalog/snapshots", payload);
export const archiveReportArtifact = (artifactId, payload = {}) => api.post(`/reports/catalog/${artifactId}/archive`, payload);
export const setReportArtifactLegalHold = (artifactId, payload) => api.put(`/reports/catalog/${artifactId}/legal-hold`, payload);
export const downloadReportArtifact = (artifactId, format, filename) =>
  api.downloadFile(`/reports/catalog/${artifactId}/download${buildQuery({ format })}`, filename);

export const getReportSchedules = () => api.get("/reports/schedules");
export const createReportSchedule = (payload) => api.post("/reports/schedules", payload);
export const updateReportSchedule = (scheduleId, payload) => api.put(`/reports/schedules/${scheduleId}`, payload);
export const deleteReportSchedule = (scheduleId) => api.delete(`/reports/schedules/${scheduleId}`);
export const runReportSchedule = (scheduleId) => api.post(`/reports/schedules/${scheduleId}/run`, {});
export const getReportScheduleRuns = (scheduleId, limit = 100) =>
  api.get(`/reports/schedules/${scheduleId}/runs${buildQuery({ limit })}`);

export const getReportSubscriptions = () => api.get("/reports/subscriptions");
export const createReportSubscription = (payload) => api.post("/reports/subscriptions", payload);
export const updateReportSubscription = (subscriptionId, payload) => api.put(`/reports/subscriptions/${subscriptionId}`, payload);
export const deleteReportSubscription = (subscriptionId) => api.delete(`/reports/subscriptions/${subscriptionId}`);

export const getInspectionReports = (dbType) =>
  api.get(`/reports/inspections${buildQuery({ db_type: dbType })}`);

export const getInspectionReportStructuredDetail = (taskId, params = {}) =>
  api.get(`/reports/${taskId}/detail${buildQuery(params)}`);

export const downloadInspectionReportWord = (instanceId, taskId) =>
  api.downloadFile(`/reports/inspections/${instanceId}/${taskId}/export-word`, `inspection_${taskId}.docx`);

export const downloadInspectionReportMarkdown = (taskId, params = {}) =>
  api.downloadFile(
    `/reports/${taskId}/export${buildQuery({ ...params, format: "markdown" })}`,
    `inspection_${taskId}.md`,
  );

export const downloadInspectionReportHtml = (taskId, params = {}) =>
  api.downloadFile(
    `/reports/${taskId}/export${buildQuery({ ...params, format: "html" })}`,
    `inspection_${taskId}.html`,
  );
