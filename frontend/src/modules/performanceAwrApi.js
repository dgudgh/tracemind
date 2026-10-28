import { api, buildQuery } from "../api";

export const getAwrDbInfo = (instanceId) => api.get(`/oracle/${instanceId}/awr/db-info`);

export const getAwrSnapshots = (instanceId, days) =>
  api.get(`/oracle/${instanceId}/awr/snapshots${buildQuery({ days })}`);

export const getAwrReports = (instanceId) => api.get(`/oracle/${instanceId}/awr/reports`);

export const getSuccessfulAwrReports = async (instanceId) => {
  const result = await getAwrReports(instanceId);
  return (result.items || []).filter((item) => item.status === "success");
};

export const getAwrReportDetail = (instanceId, reportId) =>
  api.get(`/oracle/${instanceId}/awr/reports/${reportId}/detail`);

export const runLocalAwrAnalysis = (instanceId, reportId) =>
  api.post(`/oracle/${instanceId}/awr/reports/${reportId}/local-analysis`);

export const getAnalyzedAwrReportDetail = async (instanceId, reportId, options = {}) => {
  const detail = await getAwrReportDetail(instanceId, reportId);
  if (detail.analysis_results) return { detail, analyzedNow: false };
  options.onBeforeAnalyze?.();
  await runLocalAwrAnalysis(instanceId, reportId);
  return { detail: await getAwrReportDetail(instanceId, reportId), analyzedNow: true };
};

export const compareAwrReports = (instanceId, baseId, targetId) =>
  api.get(`/oracle/${instanceId}/awr/compare${buildQuery({ base_id: baseId, target_id: targetId })}`);

export const getAwrCollectPolicies = () => api.get("/oracle/awr/collect-policies");

export const updateAwrCollectPolicy = (instanceId, values) =>
  api.put(`/oracle/${instanceId}/awr/collect-policy`, values);

export const collectAwrNow = (instanceId) => api.post(`/oracle/${instanceId}/awr/collect-now`, {});

export const exportAwrReport = (instanceId, values) =>
  api.post(`/oracle/${instanceId}/awr/export`, values);

export const getAwrEnvironment = (instanceId, report) =>
  api.get(`/oracle/${instanceId}/awr/environment${buildQuery({
    begin_snap: report.begin_snap,
    end_snap: report.end_snap,
  })}`);

export const runAwrAiAnalysis = (instanceId, report) =>
  api.post(`/oracle/${instanceId}/awr/analyze`, {
    begin_snap: report.begin_snap,
    end_snap: report.end_snap,
  });

export const deleteAwrReport = (instanceId, reportId) =>
  api.delete(`/oracle/${instanceId}/awr/reports/${reportId}`);

export const getAwrDashboard = (instanceId) => api.get(`/oracle/${instanceId}/awr/dashboard`);

export const uploadAwrReport = (instanceId, file) => {
  const formData = new FormData();
  formData.append("file", file);
  return api.postForm(`/oracle/${instanceId}/awr/upload`, formData);
};

export const downloadAwrAnalysisExport = (instanceId, report, format) =>
  api.downloadFile(
    `/oracle/${instanceId}/awr/reports/${report.id}/export${buildQuery({ format })}`,
    `awr_${report.id}_analysis.${format === "doc" ? "docx" : format}`,
  );

export const downloadOriginalAwrReport = (instanceId, report) =>
  api.downloadFile(`/oracle/${instanceId}/awr/reports/${report.id}/download`, report.file_name || "awr.html");
