import { api, buildQuery } from "../api";

export const getGovernanceBundle = (instanceId) => {
  requireArchiveId(instanceId);
  const suffix = buildQuery({ instance_id: instanceId });
  return Promise.all([
    api.get(`/data-governance/policies${suffix}`),
    api.get(`/data-governance/candidates${suffix}`),
    api.get(`/data-governance/jobs${suffix}`),
  ]);
};

export const createGovernancePolicy = (payload) => api.post("/data-governance/policies", payload);

export const scanGovernanceCandidates = (instanceId, minRows) =>
  api.post("/data-governance/candidates/scan", { instance_id: instanceId, min_rows: minRows });

export const createGovernancePlanJob = (payload) => api.post("/data-governance/jobs/plan", payload);

export const createArchiveFilePlan = (instanceId, endpoint, payload) =>
  api.post(`/oracle/${instanceId}/archive-file/${endpoint}`, payload);

export const createArchiveExportPlan = (payload) =>
  api.post("/data-governance/export/plan", payload);

export const createOracleArchiveDbPlan = (endpoint, payload) =>
  api.post(`/oracle/archive-db/${endpoint}`, payload);

export const createMigrationPlan = (payload) =>
  api.post("/data-governance/migration/plan", payload);

export const createPartitionPlan = (payload) =>
  api.post("/data-governance/partition/plan", payload);

export const getArchiveExecutionCapabilities = () =>
  api.get("/data-governance/execution/capabilities");

export const listArchiveExecutions = (jobId) =>
  api.get(`/data-governance/executions${buildQuery({ job_id: jobId })}`);

export const precheckArchiveJob = (jobId, payload) =>
  api.post(`/data-governance/jobs/${jobId}/precheck`, payload);

export const setArchiveJobStatus = (jobId, status, precheckExecutionId) =>
  api.post(`/data-governance/jobs/${jobId}/status`, {
    status,
    ...(precheckExecutionId == null ? {} : { precheck_execution_id: precheckExecutionId }),
  });

export const executeArchiveJob = (jobId, payload) =>
  api.post(`/data-governance/jobs/${jobId}/execute`, payload);

export const listArchiveRecoveryTests = (artifactId) =>
  api.get(`/data-governance/recovery-tests${buildQuery({ artifact_id: artifactId })}`);

export const runArchiveRecoveryTest = (artifactId, testMode = "sample") =>
  api.post(`/data-governance/object-storage/artifacts/${artifactId}/recovery-test`, { test_mode: testMode });

export const listArchiveLegalHolds = (artifactId) =>
  api.get(`/data-governance/legal-holds${buildQuery({ artifact_id: artifactId })}`);

export const createArchiveLegalHold = (artifactId, payload) =>
  api.post(`/data-governance/object-storage/artifacts/${artifactId}/legal-holds`, payload);

export const releaseArchiveLegalHold = (holdId, reason) =>
  api.post(`/data-governance/legal-holds/${holdId}/release`, { reason });

function requireArchiveId(value) {
  if (!Number.isSafeInteger(Number(value)) || Number(value) <= 0) throw new Error("缺少有效的归档资源编号");
}

// Optional globals are not empty successes when access is denied. The shared
// transport retains error.status; authentication/license handling stays there.
async function optionalArchiveRead(path) {
  try {
    const data = await api.get(path);
    if (!data || typeof data !== "object" || (path.endsWith("/configs") && !Array.isArray(data.items))) {
      throw new Error("归档接口返回了无效数据");
    }
    return { status: "ready", data };
  } catch (error) {
    if (error.status === 403 && !error.licenseBlocked) return { status: "forbidden", error: error.message || "无权查看全局归档数据" };
    return { status: "error", error: error.message || "归档数据加载失败" };
  }
}

export const getOptionalArchiveExecutionSummary = () => optionalArchiveRead("/data-governance/execution/summary");
export const getOptionalArchiveStorageConfigs = () => optionalArchiveRead("/data-governance/object-storage/configs");

async function artifactCollection(ids, read) {
  try {
    const pages = await Promise.all(ids.map((id) => read(id)));
    if (pages.some((page) => !Array.isArray(page?.items))) throw new Error("制品记录返回了无效数据");
    return { status: "ready", items: pages.flatMap((page) => page.items) };
  } catch (error) {
    return { status: "error", error: error.message || "制品记录加载失败" };
  }
}

export async function getArchiveJobEvidence(jobId) {
  requireArchiveId(jobId);
  const result = await listArchiveExecutions(jobId);
  if (!Array.isArray(result?.items)) throw new Error("执行记录返回了无效数据");
  const executions = result.items.filter((row) => Number(row.job_id) === Number(jobId))
    .sort((left, right) => Number(right.id) - Number(left.id));
  const artifactIds = [...new Set(executions.map((row) => row.artifact_id).filter((id) => Number(id) > 0))];
  const [recoveries, holds] = await Promise.all([
    artifactCollection(artifactIds, listArchiveRecoveryTests),
    artifactCollection(artifactIds, listArchiveLegalHolds),
  ]);
  return { jobId, executions, recoveries, holds };
}

export function archivePrecheckState(job, executions, rowLimit) {
  const latestPrecheck = executions.filter((row) => Number(row.job_id) === Number(job?.id) && row.execution_kind === "precheck")
    .sort((left, right) => Number(right.id) - Number(left.id))[0] || null;
  const manifest = latestPrecheck?.manifest;
  const matches = Boolean(job && job.status !== "cancelled" && latestPrecheck?.status === "passed"
    && Number.isSafeInteger(rowLimit) && rowLimit > 0
    && Number(latestPrecheck.instance_id) === Number(job.instance_id)
    && Number(manifest?.row_limit) === rowLimit);
  return { latestPrecheck, matches };
}

export function archivePrecheckApproved(job, precheck, rowLimit) {
  if (job?.status !== "approved" || !archivePrecheckState(job, precheck ? [precheck] : [], rowLimit).matches) return false;
  const approval = precheck.manifest.approval;
  if (approval?.precheck_execution_id !== precheck.id || !approval?.approval_id
    || !approval?.approved_by || !approval?.approved_at || !approval?.evidence_sha256) return false;
  return true; // Backend verifies the protected evidence digest at execution time.
}
