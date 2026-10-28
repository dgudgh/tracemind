import { api, buildQuery } from "../api";

const base = "/operations/config-governance";

export const configGovernanceApi = {
  summary() {
    return api.get(`${base}/summary`);
  },
  baselines(params = {}) {
    return api.get(`${base}/baselines${buildQuery(params)}`);
  },
  baseline(id) {
    return api.get(`${base}/baselines/${id}`);
  },
  createBaseline(payload) {
    return api.post(`${base}/baselines`, payload);
  },
  updateBaseline(id, payload) {
    return api.put(`${base}/baselines/${id}`, payload);
  },
  deleteBaseline(id) {
    return api.delete(`${base}/baselines/${id}`);
  },
  evaluateBaseline(id, payload) {
    return api.post(`${base}/baselines/${id}/evaluate`, payload);
  },
  snapshots(params = {}) {
    return api.get(`${base}/snapshots${buildQuery(params)}`);
  },
  drifts(params = {}) {
    return api.get(`${base}/drifts${buildQuery(params)}`);
  },
  acceptDrift(id, reason) {
    return api.post(`${base}/drifts/${id}/accept`, { reason });
  },
  remediations(params = {}) {
    return api.get(`${base}/remediations${buildQuery(params)}`);
  },
  createRemediation(payload) {
    return api.post(`${base}/remediations`, payload);
  },
  submitRemediation(id, payload = {}) {
    return api.post(`${base}/remediations/${id}/submit`, payload);
  },
  applyRemediation(id) {
    return api.post(`${base}/remediations/${id}/apply`, { confirm: true });
  },
  verifyRemediation(id, payload = {}) {
    return api.post(`${base}/remediations/${id}/verify`, payload);
  },
  audits(params = {}) {
    return api.get(`${base}/audits${buildQuery(params)}`);
  },
};
