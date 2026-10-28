import { api, buildQuery } from "../api";

export const getCapacityAlertBundle = ({ status, instanceId, dateFrom, dateTo }) => {
  const alertQuery = buildQuery({
    limit: 200,
    status,
    instance_id: instanceId,
    date_from: dateFrom,
    date_to: dateTo,
  });
  const statsQuery = buildQuery({
    instance_id: instanceId,
    date_from: dateFrom,
    date_to: dateTo,
  });
  return Promise.all([
    api.get(`/capacity/alerts${alertQuery}`),
    api.get(`/capacity/alerts/stats${statsQuery}`),
  ]);
};

export const getCapacityAlertRuleBundle = ({ ruleType, level, enabled, keyword }) => {
  const query = buildQuery({
    rule_type: ruleType,
    level,
    enabled: enabled === "enabled" ? 1 : enabled === "disabled" ? 0 : undefined,
    keyword: keyword?.trim(),
  });
  return Promise.all([
    api.get(`/capacity/alert-rules${query}`),
    api.get("/capacity/alert-rules/stats"),
  ]);
};

export const closeCapacityAlert = (id) => api.put(`/capacity/alerts/${id}/close`);

export const closeCapacityAlerts = ({ instanceId, dateFrom, dateTo }) => {
  const query = buildQuery({
    instance_id: instanceId,
    date_from: dateFrom,
    date_to: dateTo,
  });
  return api.put(`/capacity/alerts/close-all${query}`);
};

export const isolateCapacityAlert = (id, days, reason) =>
  api.put(`/capacity/alerts/${id}/isolate${buildQuery({ days, reason })}`);

export const setCapacityAlertRuleEnabled = (id, enabled) =>
  api.put(`/capacity/alert-rules/${id}/enabled${buildQuery({ enabled: enabled ? 1 : 0 })}`);
