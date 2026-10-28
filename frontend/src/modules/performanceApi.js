import { api, buildQuery } from "../api";

export const getPerformanceOverview = () => api.get("/performance/overview");

export const getDatabaseProfile = (instanceId) =>
  api.get(`/performance/profile/${instanceId}`);

export const refreshPerformanceSuggestions = (instanceId) =>
  api.post(`/performance/suggestions/refresh${buildQuery({ instance_id: instanceId })}`, {});

export const getPerformanceSuggestions = (params = {}) =>
  api.get(`/performance/suggestions${buildQuery(params)}`);

export const getPerformanceSuggestionStats = (instanceId) =>
  api.get(`/performance/suggestions/stats${buildQuery({ instance_id: instanceId })}`);

export const updatePerformanceSuggestionStatus = (suggestionId, status) =>
  api.put(`/performance/suggestions/${suggestionId}`, { status });

export const createPerformanceSuggestionTicket = (suggestionId) =>
  api.post(`/performance/suggestions/${suggestionId}/to-ticket`, {});

export const getPerformanceSuggestionPlan = (suggestionId) =>
  api.get(`/performance/suggestions/${suggestionId}/plan`);

export const getTableProfile = (instanceId, params = {}) =>
  api.get(`/performance/table-profile/${instanceId}${buildQuery(params)}`);

export const collectTableProfile = (instanceId) =>
  api.post(`/performance/table-profile/${instanceId}/collect`, {});

export const getOracleTableUsage = (instanceId, endpoint, params = {}) =>
  api.get(`/oracle/${instanceId}/table-usage/${endpoint}${buildQuery(params)}`);

export const getPerformanceBaseline = (instanceId) =>
  api.get(`/performance/baseline/${instanceId}`);

export const collectPerformanceBaseline = (instanceId) =>
  api.post(`/performance/baseline/${instanceId}/collect`, {});

export const getProblemTrend = (params = {}) =>
  api.get(`/performance/problem-trend${buildQuery(params)}`);

export const getIndexFindings = (instanceId, params = {}) =>
  api.get(`/performance/index-findings/${instanceId}${buildQuery(params)}`);

export const collectIndexFindings = (instanceId) =>
  api.post(`/performance/index-findings/${instanceId}/collect`, {});

export const getSqlProfile = (instanceId, params = {}) =>
  api.get(`/performance/sql-profile/${instanceId}${buildQuery(params)}`);

export const collectSqlProfile = (instanceId) =>
  api.post(`/performance/sql-profile/${instanceId}/collect`, {});

export const getSqlProfileDetail = (sqlId, instanceId, profileId) =>
  api.get(`/sql-profiles/${encodeURIComponent(sqlId)}${buildQuery({ instance_id: instanceId, profile_id: profileId })}`);

export const compareSqlPlans = (sqlId, payload) =>
  api.post(`/sql-profiles/${encodeURIComponent(sqlId)}/compare`, payload);

export const getWorkloadReportCapabilities = () =>
  api.get("/performance/workload-reports/capabilities");

export const getWorkloadReports = (params = {}) =>
  api.get(`/performance/workload-reports${buildQuery(params)}`);

export const collectWorkloadReport = (instanceId, payload) =>
  api.post(`/performance/workload-reports/${instanceId}/collect`, payload);

export const getWorkloadReport = (reportId) =>
  api.get(`/performance/workload-reports/${reportId}`);

export const compareWorkloadReports = (leftReportId, rightReportId) =>
  api.post("/performance/workload-reports/compare", {
    left_report_id: leftReportId,
    right_report_id: rightReportId,
  });
