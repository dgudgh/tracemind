import { api, buildQuery } from "./api";

export const authApi = {
  login(username, password) {
    return api.post("/auth/login", { username, password });
  },
  verifyMfa(challengeToken, code) {
    return api.post("/auth/mfa/verify", { challenge_token: challengeToken, code });
  },
  externalLogin(username, password) {
    return api.post("/auth/external-login", { username, password });
  },
  oidcProviders() {
    return api.get("/auth/oidc/providers");
  },
  oidcStart(providerId) {
    return api.get(`/auth/oidc/${providerId}/start`);
  },
  oidcExchange(code) {
    return api.post("/auth/oidc/exchange", { code });
  },
  mesLogin(token) {
    return api.post("/auth/mes-login", { token });
  },
  logout(token) {
    return api.post("/auth/logout", { token });
  },
  currentUser() {
    return api.get("/auth/current-user");
  },
};

export const systemApi = {
  initStatus() {
    return api.get("/system/init/status");
  },
  config() {
    return api.get("/system/config");
  },
  dashboardStats() {
    return api.get("/system/dashboard-stats");
  },
  apiModules() {
    return api.get("/system/api-modules");
  },
};

export const moduleApi = {
  list(scope) {
    return api.get(`/modules${buildQuery({ scope })}`);
  },
};

export const instanceApi = {
  list(params = { page: 1, page_size: 100 }) {
    return api.get(`/instances${buildQuery(params)}`);
  },
  get(id) {
    return api.get(`/instances/${id}`);
  },
  create(payload) {
    return api.post("/instances", payload);
  },
  update(id, payload) {
    return api.put(`/instances/${id}`, payload);
  },
  remove(id) {
    return api.delete(`/instances/${id}`);
  },
  test(id) {
    return api.post(`/instances/${id}/test`);
  },
  testConfig(payload) {
    return api.post("/instances/connection-test", payload);
  },
  rotateCredential(id, payload) {
    return api.post(`/instances/${id}/credentials/rotate`, payload);
  },
  credentialAudits(id, limit = 100) {
    return api.get(`/instances/${id}/credential-audits${buildQuery({ limit })}`);
  },
  hostOverview(id) {
    return api.get(`/instances/${id}/host/overview`);
  },
};

export const connectionSecurityApi = {
  certificates() {
    return api.get("/connection-security/certificates");
  },
  createCertificate(payload) {
    return api.post("/connection-security/certificates", payload);
  },
  updateCertificate(id, payload) {
    return api.put(`/connection-security/certificates/${id}`, payload);
  },
  deleteCertificate(id) {
    return api.delete(`/connection-security/certificates/${id}`);
  },
  testCertificate(id) {
    return api.post(`/connection-security/certificates/${id}/test`);
  },
  certificateAudits(id, limit = 100) {
    return api.get(`/connection-security/certificates/${id}/audits${buildQuery({ limit })}`);
  },
  minimumPrivilegeTemplates(dbType) {
    return api.get(`/connection-security/minimum-privilege-templates${buildQuery({ db_type: dbType })}`);
  },
  compatibilityMatrix() {
    return api.get("/connection-security/compatibility-matrix");
  },
};

export const sshApi = {
  test(instanceId) {
    return api.post(`/ssh/${instanceId}/test`);
  },
  diagnose(instanceId) {
    return api.post(`/ssh/${instanceId}/diagnose`);
  },
  systemInfo(instanceId) {
    return api.get(`/ssh/${instanceId}/system-info`);
  },
  execute(instanceId, payload) {
    return api.post(`/ssh/${instanceId}/execute`, payload);
  },
};

export const capacityApi = {
  policy(instanceId) {
    return api.get(`/capacity/policies/${instanceId}`);
  },
  summary(instanceId) {
    return api.get(`/capacity/${instanceId}/summary`);
  },
  analysis(instanceId, params = { days: 30 }) {
    return api.get(`/capacity/${instanceId}/analysis${buildQuery(params)}`);
  },
  snapshots(instanceId, params = { days: 30 }) {
    return api.get(`/capacity/${instanceId}/snapshots${buildQuery(params)}`);
  },
  collect(instanceId) {
    return api.post(`/capacity/${instanceId}/collect`);
  },
  alerts(params = {}) {
    return api.get(`/capacity/alerts${buildQuery(params)}`);
  },
  closeAll() {
    return api.put("/capacity/alerts/close-all");
  },
};

export const alertReportApi = {
  events(params = { limit: 200 }) {
    return api.get(`/alert-events${buildQuery(params)}`);
  },
  stats() {
    return api.get("/alert-events/stats");
  },
  actions(params = { limit: 100 }) {
    return api.get(`/alert-events/actions${buildQuery(params)}`);
  },
  importCapacity() {
    return api.post("/alert-events/import/capacity", {});
  },
  importExternal() {
    return api.post("/alert-events/import/external", {});
  },
  close(id, reason = "frontend close") {
    return api.post(`/alert-events/${id}/close`, { status: "closed", reason });
  },
  action(id, payload) {
    return api.post(`/alert-events/${id}/actions`, payload);
  },
};

export const settingsApi = {
  list() {
    return api.get("/settings");
  },
  update(updates) {
    return api.put("/settings", { updates });
  },
};

export const aiConfigApi = {
  list() {
    return api.get("/ai-config");
  },
  create(payload) {
    return api.post("/ai-config", payload);
  },
  update(id, payload) {
    return api.put(`/ai-config/${id}`, payload);
  },
  test(payload) {
    return api.post("/ai-config/test", payload);
  },
  remove(id) {
    return api.delete(`/ai-config/${id}`);
  },
};

export const mysqlOpsApi = {
  section(instanceId, endpoint) {
    const spec = typeof endpoint === "string" ? { path: endpoint } : endpoint;
    return api.get(`/mysql-ops/${instanceId}${spec.path}${buildQuery(spec.params)}`);
  },
  lockTransactions(instanceId) {
    return api.get(`/mysql-ops/${instanceId}/locks/transactions`);
  },
  killSessionPlan(instanceId, { threadId, action }) {
    return api.post(`/mysql-ops/${instanceId}/plans/kill-session${buildQuery({ thread_id: threadId, action })}`);
  },
  slowSqlTop(instanceId, params = { time_range: "24h", limit: 200 }) {
    return api.get(`/mysql-ops/${instanceId}/slow-sql/top${buildQuery(params)}`);
  },
  capacityTables(instanceId, params = { limit: 200 }) {
    return api.get(`/mysql-ops/${instanceId}/capacity/tables${buildQuery(params)}`);
  },
  binlogPurgePlan(instanceId, params = {}) {
    return api.post(`/mysql-ops/${instanceId}/binlog/purge-plan${buildQuery(params)}`);
  },
  userGrants(instanceId, { user, host }) {
    return api.get(`/mysql-ops/${instanceId}/users/${encodeURIComponent(user)}/grants${buildQuery({ host })}`);
  },
  tableIndexPlan(instanceId, path, payload) {
    return api.post(`/mysql-ops/${instanceId}/tables-indexes/${path}`, payload);
  },
  tableStatisticsPlan(instanceId, path, payload) {
    return api.post(`/operations/mysql/${instanceId}/table-statistics/${path}`, payload);
  },
};

export const oracleOpsApi = {
  basicInfo(instanceId) {
    return api.get(`/oracle/${instanceId}/basic-info`);
  },
  dbStatus(instanceId) {
    return api.get(`/oracle/${instanceId}/db-status`);
  },
  startupScript(instanceId, mode) {
    return api.get(`/oracle/${instanceId}/startup-script${buildQuery({ mode })}`);
  },
  shutdownScript(instanceId, mode) {
    return api.get(`/oracle/${instanceId}/shutdown-script${buildQuery({ mode })}`);
  },
  bootPrerequisites(instanceId) {
    return api.get(`/oracle/${instanceId}/boot-prerequisites`);
  },
  bootScript(instanceId, mode) {
    return api.get(`/oracle/${instanceId}/boot-script${buildQuery({ mode })}`);
  },
  parameters(instanceId, params = {}) {
    return api.get(`/oracle/${instanceId}/parameters${buildQuery(params)}`);
  },
  parameterSection(instanceId, section) {
    return api.get(`/oracle/${instanceId}/${section}`);
  },
  alterSystemScript(instanceId, params = {}) {
    return api.get(`/oracle/${instanceId}/alter-system-script${buildQuery(params)}`);
  },
  osKillScript(instanceId, params = {}) {
    return api.get(`/oracle/${instanceId}/os-kill-script${buildQuery(params)}`);
  },
  tablespaces(instanceId) {
    return api.get(`/operations/oracle/${instanceId}/tablespaces`);
  },
  archive(instanceId) {
    return api.get(`/operations/oracle/${instanceId}/archive`);
  },
  archiveConfig(instanceId) {
    return api.get(`/oracle/${instanceId}/archive-config`);
  },
  flashbackStatus(instanceId) {
    return api.get(`/oracle/${instanceId}/flashback-status`);
  },
  flashbackPlan(instanceId, action = "enable") {
    return api.get(`/oracle/${instanceId}/flashback-plan${buildQuery({ action })}`);
  },
  archiveSpaceDetail(instanceId) {
    return api.get(`/oracle/${instanceId}/archive-space-detail`);
  },
  archiveEnableScript(instanceId) {
    return api.get(`/oracle/${instanceId}/archive-enable-script`);
  },
  archiveDisableScript(instanceId) {
    return api.get(`/oracle/${instanceId}/archive-disable-script`);
  },
  archivePathScript(instanceId, params = {}) {
    return api.get(`/oracle/${instanceId}/archive-path-script${buildQuery(params)}`);
  },
  archiveEmergencyScript(instanceId, mode) {
    return api.get(`/oracle/${instanceId}/archive-emergency-script${buildQuery({ mode })}`);
  },
  tablespaceExpansionPlan(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/tablespaces/expansion-plan`, payload);
  },
  deployAutoextend(instanceId, params = {}) {
    return api.post(`/oracle/${instanceId}/tablespaces/deploy-autoextend${buildQuery(params)}`);
  },
  deployArchiveCleanup(instanceId, params = {}) {
    return api.post(`/oracle/${instanceId}/archive/deploy-cleanup${buildQuery(params)}`);
  },
  archiveCleanupPlan(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/archive/cleanup-plan`, payload);
  },
  flashbackQuery(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/flashback/query`, payload);
  },
  recyclebin(instanceId, params = {}) {
    return api.get(`/operations/oracle/${instanceId}/recyclebin${buildQuery(params)}`);
  },
  recyclebinRestorePlan(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/recyclebin/restore-plan`, payload);
  },
  tableRestorePlan(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/table/restore-plan`, payload);
  },
  rmanRestorePlan(instanceId, payload) {
    return api.post(`/operations/oracle/${instanceId}/rman/restore-plan`, payload);
  },
  undoOverview(instanceId) {
    return api.get(`/oracle/${instanceId}/undo/overview`);
  },
  undoExtents(instanceId) {
    return api.get(`/oracle/${instanceId}/undo/extents`);
  },
  undoTransactions(instanceId) {
    return api.get(`/oracle/${instanceId}/undo/transactions`);
  },
  undoHistory(instanceId) {
    return api.get(`/oracle/${instanceId}/undo/history`);
  },
  undoRetentionPlan(instanceId, payload) {
    return api.post(`/oracle/${instanceId}/undo/retention-plan`, payload);
  },
  dataGuardOverview(instanceId, standbyId) {
    return api.get(`/oracle/${instanceId}/dataguard/overview${buildQuery({ standby_instance_id: standbyId })}`);
  },
  dataGuardSwitchoverCheckPlan(instanceId) {
    return api.post(`/oracle/${instanceId}/dataguard/switchover-check-plan`, {});
  },
  redoLog(instanceId) {
    return api.get(`/oracle/${instanceId}/redo-log`);
  },
  redoLogSwitchFrequency(instanceId, thread) {
    return api.get(`/oracle/${instanceId}/redo-log/switch-frequency${buildQuery({ thread })}`);
  },
  redoLogAddGroupScript(instanceId, params = { size_mb: 1024 }) {
    return api.get(`/oracle/${instanceId}/redo-log/add-group-script${buildQuery(params)}`);
  },
  redoLogSwitchScript(instanceId) {
    return api.get(`/oracle/${instanceId}/redo-log/switch-script`);
  },
  redoLogDropGroupScript(instanceId, groupNum) {
    return api.get(`/oracle/${instanceId}/redo-log/drop-group-script${buildQuery({ group_num: groupNum })}`);
  },
  tempOverview(instanceId) {
    return api.get(`/oracle/${instanceId}/temp/overview`);
  },
  tempFiles(instanceId) {
    return api.get(`/oracle/${instanceId}/temp/files`);
  },
  tempSqlUsage(instanceId) {
    return api.get(`/oracle/${instanceId}/temp/sql-usage`);
  },
  tempSessionUsage(instanceId) {
    return api.get(`/oracle/${instanceId}/temp/session-usage`);
  },
  tempExtendPlan(instanceId, params = {}) {
    return api.post(`/oracle/${instanceId}/temp/extend-plan${buildQuery(params)}`);
  },
  lockedObjects(instanceId) {
    return api.get(`/oracle/${instanceId}/locked-objects`);
  },
  blockingTree(instanceId) {
    return api.get(`/oracle/${instanceId}/blocking-tree`);
  },
  users(instanceId) {
    return api.get(`/oracle/${instanceId}/users`);
  },
  roles(instanceId) {
    return api.get(`/oracle/${instanceId}/roles`);
  },
  userPrivileges(instanceId, username) {
    return api.get(`/oracle/${instanceId}/users/${encodeURIComponent(username)}/privileges`);
  },
  ransomwareCheck(instanceId) {
    return api.post(`/oracle/${instanceId}/ransomware-check`);
  },
};

export const genericOpsApi = {
  capabilityMatrix(params = {}, options = {}) {
    return api.get(`/operations/capability-matrix${buildQuery(params)}`, options);
  },
  section(engine, instanceId, section) {
    return api.get(`/operations/${engine}/${instanceId}/${section}`);
  },
  plan(engine, instanceId, planType) {
    return api.post(`/operations/${engine}/${instanceId}/plans/${planType}`, {});
  },
};

export const setupApi = {
  license() {
    return api.get("/setup/license");
  },
  uploadLicenseContent(content) {
    return api.post("/setup/license/upload", { content });
  },
  initStatus() {
    return api.get("/system/init/status");
  },
  setupAdmin(payload) {
    return api.post("/system/init/setup", payload);
  },
  testConnection(payload) {
    return api.post("/setup/test-connection", payload);
  },
  initDatabase(payload) {
    return api.post("/setup/init-database", payload);
  },
  restart() {
    return api.post("/setup/restart", {});
  },
};


export async function readEngineCatalog(session) {
  const value = await api.request("/engine-catalog", { authSession: session });
  if (value?.schema !== "engine.catalog_view/v1" || !Number.isSafeInteger(value.catalog_generation)
      || value.catalog_generation < 1 || !/^[a-f0-9]{32}$/.test(value.catalog_boot_id) || !Array.isArray(value.engines)
      || !value.engines.every((entry) => typeof entry?.engine_id === "string"
        && typeof entry.available === "boolean" && (entry.reason === null || typeof entry.reason === "string"))) {
    throw new Error("引擎目录响应无效");
  }
  return value;
}

// 只读：反映本机 Python 环境能否导入驱动，不代表引擎包已安装或服务端已认证。
export async function readEngineDriverStatus(session) {
  const value = await api.request("/engine-catalog/drivers", { authSession: session });
  if (value?.schema !== "engine.driver_status/v1" || !Array.isArray(value.drivers)
      || !value.drivers.every((row) => typeof row?.engine_id === "string"
        && typeof row.installed === "boolean" && Array.isArray(row.available_via))) {
    throw new Error("引擎驱动状态响应无效");
  }
  return value;
}
