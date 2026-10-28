import { api, buildQuery } from "../api";

export const listStateDefinitions = (filters = {}) => api.get(`/states/definitions${buildQuery(filters)}`);
export const listStateTimeline = (filters = {}) => api.get(`/states/timeline${buildQuery(filters)}`);
export const listFaultPatternMatches = (filters = {}) => api.get(`/fault-patterns/matches${buildQuery(filters)}`);

export const listSceneCaptures = (filters = {}) => api.get(`/scene-captures${buildQuery(filters)}`);
export const createSceneCapture = (incidentId, captureType, values) => (
  api.post(`/incidents/${incidentId}/captures/${captureType === "thread_trace" ? "thread-trace" : captureType}`, values)
);
export const approveSceneCapture = (jobId, values) => api.post(`/scene-captures/${jobId}/approval`, values);
export const cancelSceneCapture = (jobId) => api.delete(`/scene-captures/${jobId}`);
export const generateSceneContext = (incidentId, values = {}) => api.post(`/incidents/${incidentId}/scene-context`, values);
export const listProbeTemplates = () => api.get("/probe-templates");
export const listProbeLeases = (filters = {}) => api.get(`/probe-leases${buildQuery(filters)}`);
export const createProbeLease = (incidentId, values) => api.post(`/incidents/${incidentId}/probes`, values);
export const removeProbeLease = (incidentId, leaseId) => api.delete(`/incidents/${incidentId}/probes/${leaseId}`);

export const listKernelCapabilities = (filters = {}) => api.get(`/kernel-observability/capabilities${buildQuery(filters)}`);
export const getKernelAgentHealth = (agentId) => api.get(`/kernel-observability/agents/${agentId}/health`);
export const listKernelSensors = () => api.get("/kernel-observability/sensors");
export const listKernelPolicies = () => api.get("/kernel-observability/policies");
export const testKernelPolicy = (policyId, values = {}) => api.post(`/kernel-observability/policies/${policyId}/test`, values);
export const setKernelAgentLevel = (agentId, values) => api.post(`/kernel-observability/agents/${agentId}/level`, values);
export const setKernelKillSwitch = (values) => api.post("/kernel-observability/kill-switch", values);
export const listKernelEvents = (snapshotId, filters = {}) => api.get(`/incident-snapshots/${snapshotId}/kernel-events${buildQuery(filters)}`);
export const listKernelStacks = (snapshotId, filters = {}) => api.get(`/incident-snapshots/${snapshotId}/stacks${buildQuery(filters)}`);
