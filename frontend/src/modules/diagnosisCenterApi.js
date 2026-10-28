import { api, buildQuery } from "../api";

export { degradationBanner } from "./aiDegradation";

export const loadDiagnosisCenterIncidents = (instanceId, pageSize = 50, page = 1) =>
  api.get(`/diagnosis-center/incidents${buildQuery({ instance_id: instanceId, page_size: pageSize, page })}`);

export const loadDiagnosisCenterContext = () =>
  Promise.all([
    api.get("/diagnosis-center/methodology"),
    api.get("/diagnosis-center/data-sources"),
  ]);

export const loadDiagnosisCenterInstanceContext = (instanceId) =>
  Promise.all([
    api.get(`/diagnosis-center/instances/${instanceId}/arch-profile`),
    api.get(`/diagnosis-center/instances/${instanceId}/auto-policy`),
  ]);

export const getDiagnosisCenterIncident = (incidentId) =>
  api.get(`/diagnosis-center/incidents/${incidentId}`);

export const runDiagnosisCenter = (payload) =>
  api.post("/diagnosis-center/diagnose", payload);

export const routeDiagnosisIntent = (payload) =>
  api.post("/diagnosis-center/route-intent", payload);

export const notifyDiagnosisCenterIncident = (incidentId) =>
  api.post(`/diagnosis-center/incidents/${incidentId}/notify`, {});
