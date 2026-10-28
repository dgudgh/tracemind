import { api, buildQuery } from "../api";
import { optionalEditionCall } from "./editionFallback";

export const getObservabilityBundle = () =>
  Promise.all([
    api.get("/observability/overview"),
    api.get("/observability/sources"),
    api.get("/observability/standard-metrics"),
    api.get("/observability/metric-mappings"),
    api.get("/observability/bindings"),
    api.get("/observability/policies"),
    api.get(`/observability/tasks${buildQuery({ limit: 100 })}`),
    optionalEditionCall(api.get(`/observability/logs${buildQuery({ limit: 100 })}`)),
    api.get(`/observability/alerts${buildQuery({ status: "all", limit: 100 })}`),
    api.get("/observability/exporters"),
    api.get(`/observability/metrics/current${buildQuery({ limit: 200 })}`),
    api.get(`/observability/metrics/history${buildQuery({ limit: 200 })}`),
    api.get("/observability/data-quality"),
  ]);

export const listObservabilitySources = () => api.get("/observability/sources");

export const saveObservabilitySource = (values) => api.post("/observability/sources", values);

export const testObservabilitySource = (sourceId) =>
  api.post(`/observability/sources/${sourceId}/test`, {});

export const runObservabilityTask = ({ sourceId, taskName, collectType }) =>
  api.post("/observability/tasks/run", {
    source_id: sourceId,
    task_name: taskName,
    collect_type: collectType,
  });

const ZABBIX_ACTION_PATH = {
  hosts: "sync-hosts",
  problems: "sync-problems",
  metrics: "sync-metrics",
};

export const runZabbixAction = (sourceId, action) =>
  api.post(`/observability/zabbix/${ZABBIX_ACTION_PATH[action]}${buildQuery({ source_id: sourceId })}`, {});

export const saveMetricMapping = (values) => api.post("/observability/metric-mappings", values);

export const saveCollectPolicy = (values) => api.post("/observability/policies", values);

export const saveObservabilityBinding = (values) => api.post("/observability/bindings", values);

export const autoBindObservability = () => api.post("/observability/bindings/auto", {});

export const getObservabilityLinkageHealth = ({ instanceId, probeSources = false, probeTimeoutSeconds = 2 }) =>
  api.get(`/observability/linkage-health${buildQuery({
    instance_id: instanceId,
    probe_sources: probeSources ? 1 : 0,
    probe_timeout_seconds: probeTimeoutSeconds,
  })}`);

export const syncPrometheusStandardMetrics = (sourceId) =>
  api.post(`/observability/prometheus/sync-standard-metrics${buildQuery({ source_id: sourceId })}`, {});

export const listObservabilityInstances = () => api.get(`/instances${buildQuery({ page: 1, page_size: 500 })}`);

export const queryObservabilityLogs = (values) => api.post("/observability/logs/query", values);

export const buildObservabilityFaultTimeline = (values) => api.post("/observability/fault-timeline", values);

export const listGrafanaTemplates = () => api.get("/observability/grafana/templates");

export const exportGrafanaTemplate = (values) => api.post("/observability/grafana/export", values);

export const queryApmTrace = (values) => api.post("/observability/apm/trace", values);

export const linkApmSqlEvidence = (values) => api.post("/observability/apm/sql-evidence", values);

export const getObservabilityDataQuality = (sourceId) =>
  api.get(`/observability/data-quality${buildQuery({ source_id: sourceId })}`);

export const evaluateObservabilityDataQuality = (sourceId) =>
  api.post("/observability/data-quality/evaluate", { source_id: sourceId || null, persist: true });

export const listObservabilityDataQualityPolicies = () =>
  api.get("/observability/data-quality/policies");

export const saveObservabilityDataQualityPolicy = (values) =>
  api.put("/observability/data-quality/policies", values);

export const listObservabilityDataQualityHistory = (sourceId, limit = 100) =>
  api.get(`/observability/data-quality/history${buildQuery({ source_id: sourceId, limit })}`);

export const runObservabilityRetention = ({ sourceId, dryRun = true, confirmation = null }) =>
  api.post("/observability/data-quality/retention", {
    source_id: sourceId || null,
    dry_run: dryRun,
    confirmation,
  });
