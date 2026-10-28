import { api, buildQuery } from "../api";

export const getRuleDiagnosisHistory = (params = {}) =>
  api.get(`/diagnosis/results${buildQuery(params)}`);

export const getDiagnosisRules = () => api.get("/diagnosis/rules");

export const runRuleDiagnosis = (instanceId) =>
  api.post("/diagnosis/run", { instance_id: instanceId, persist: true });

export const generateDiagnosisAction = (resultId, action) =>
  api.post(`/diagnosis/results/${resultId}/${action}`, {});

export const updateDiagnosisRule = (ruleId, payload) =>
  api.put(`/diagnosis/rules/${ruleId}`, payload);
