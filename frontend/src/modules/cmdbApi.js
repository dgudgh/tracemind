import { api, buildQuery } from "../api";

export const listCmdbSources = () => api.get("/cmdb/sources");
export const createCmdbSource = (values) => api.post("/cmdb/sources", values);
export const syncCmdbAssets = (values) => api.post("/cmdb/sync", values);

export const getAssetOverview = () => api.get("/assets/overview");
export const getAssetTree = () => api.get("/assets/tree");
export const listAssetClusters = () => api.get("/assets/clusters");
export const getAssetCluster = (clusterId) => api.get(`/assets/clusters/${clusterId}`);
export const getClusterHADiagnostics = (clusterId) => api.get(`/assets/clusters/${clusterId}/ha-diagnostics`);
export const refreshClusterHADiagnostics = (clusterId) => api.post(`/assets/clusters/${clusterId}/ha-diagnostics/refresh`, {});
export const listClusterHADiagnosticHistory = (clusterId, filters = {}) => api.get(`/assets/clusters/${clusterId}/ha-diagnostics/history${buildQuery(filters)}`);
export const generateClusterHAPlan = (endpoint) => api.post(endpoint, {});
export const listAssetInstances = () => api.get("/assets/instances");
export const getNormalizedAssetInstance = (instanceId) => api.get(`/assets/instances/${instanceId}`);
export const listAssetHosts = () => api.get("/assets/hosts");
export const getAssetHost = (hostId) => api.get(`/assets/hosts/${hostId}`);
export const listAssetDiscoveryBatches = (filters = {}) => api.get(`/assets/discovery-batches${buildQuery(filters)}`);
export const listAssetCorrections = (filters = {}) => api.get(`/assets/corrections${buildQuery(filters)}`);
export const createAssetCorrection = (values) => api.post("/assets/corrections", values);
export const previewAssetDiscovery = () => api.post("/assets/discovery/preview");
export const applyAssetDiscovery = (options = {}) => api.post("/assets/discovery/apply", options);
