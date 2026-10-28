import { api } from "../api";

export const getSchedulerTasks = () => api.get("/scheduler/tasks");

export const getSchedulerStatus = () => api.get("/scheduler/status");

export const updateSchedulerTask = (taskType, instanceId, payload) =>
  api.put(`/scheduler/tasks/${taskType}/${instanceId}`, payload);

export const runSchedulerTaskNow = (taskType, instanceId) =>
  api.post(`/scheduler/tasks/${taskType}/${instanceId}/run-now`, {});
