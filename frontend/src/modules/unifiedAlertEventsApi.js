import { api, buildQuery } from "../api";
import { isEditionLocked, optionalEditionCall } from "./editionFallback";

// 外部告警源（Zabbix / Prometheus / 推送日志）属「集成中心」，开源版被门禁拦下。
// 这三条读失败不能让整页崩掉：Promise.all 只要一条 reject，统一告警页就什么都显示不出来，
// 用户看到的是「当前为开源版，该模块需要企业版授权」——那是版本说明，不是业务错误。
// 因此逐条降级为「已锁定」，由页面就地提示，平台内告警事件照常展示。
export const getUnifiedAlertBundle = () =>
  Promise.all([
    api.get(`/alert-events${buildQuery({ limit: 200 })}`),
    api.get("/alert-events/stats"),
    api.get(`/alert-events/actions${buildQuery({ limit: 100 })}`),
    optionalEditionCall(api.get(`/zabbix/problems${buildQuery({ status: "firing", limit: 100 })}`)),
    optionalEditionCall(api.get(`/prometheus/alerts${buildQuery({ status: "all", limit: 100 })}`)),
    optionalEditionCall(api.get(`/zabbix-push/logs${buildQuery({ limit: 100 })}`)),
    api.get("/alert-events/maintenance-windows"),
  ]);

export const isAlertIntegrationLocked = (zbx, prom, logs) => isEditionLocked(zbx, prom, logs);

export const importCapacityAlerts = () => api.post("/alert-events/import/capacity", {});

export const importExternalAlerts = () => api.post("/alert-events/import/external", {});

export const createUnifiedAlertAction = (event, actionType) =>
  api.post(`/alert-events/${event.id}/actions`, {
    action_type: actionType,
    target: actionType === "ticket" ? "ITSM" : actionType === "zabbix_push" ? "Zabbix" : "Notify",
    request: { event_uid: event.event_uid, title: event.title, severity: event.severity },
    plan_only: true,
  });

export const getUnifiedAlertMetricTrends = (eventId) =>
  api.get(`/alert-events/${eventId}/metric-trends`);

export const getUnifiedAlertEvidenceChain = (eventId) =>
  api.get(`/alert-events/${eventId}/evidence-chain`);

export const diagnoseUnifiedAlertEvent = (eventId) =>
  api.post(`/alert-events/${eventId}/diagnose`, {});

export const getUnifiedAlertTicketDescription = (eventId) =>
  api.get(`/alert-events/${eventId}/ticket-description`);

export const getUnifiedAlertDetail = (eventId) =>
  api.get(`/alert-events/${eventId}`);

export const transitionUnifiedAlertEvent = (eventId, payload) =>
  api.post(`/alert-events/${eventId}/transition`, payload);

export const createMaintenanceWindow = (payload) =>
  api.post("/alert-events/maintenance-windows", payload);

export const updateMaintenanceWindow = (windowId, payload) =>
  api.put(`/alert-events/maintenance-windows/${windowId}`, payload);

export const deleteMaintenanceWindow = (windowId) =>
  api.delete(`/alert-events/maintenance-windows/${windowId}`);
