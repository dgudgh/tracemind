import { api, buildQuery } from "../api";

export const getUsers = () => api.get("/users");

export const getModules = () => api.get(`/modules${buildQuery({ scope: "all" })}`);

export const getPasswordPolicy = () => api.get("/users/password-policy");

export const getUserPermissions = (userId) => api.get(`/users/${userId}/permissions`);

export const saveUserPermissions = (userId, moduleKeys) =>
  api.put(`/users/${userId}/permissions`, { module_keys: moduleKeys });

export const setModuleEnabled = (moduleKey, enabled) =>
  api.put(`/modules/${moduleKey}`, { enabled: enabled ? 1 : 0 });

export const saveModulesConfig = (modules) =>
  api.put("/modules", {
    modules: modules.map((m) => ({ module_key: m.module_key, enabled: m.enabled })),
  });

export const resetModulesConfig = () => api.post("/modules/reset");

export const getUserManagementBundle = () =>
  Promise.all([getUsers(), getModules(), getPasswordPolicy(), api.get("/auth/current-user")]);

export const createUser = (values) => api.post("/users", values);

export const updateUserStatus = (userId, status) => api.put(`/users/${userId}`, { status });

export const unlockUserAccount = (userId) => api.post(`/users/${userId}/unlock`);

export const deleteUserAccount = (userId) => api.delete(`/users/${userId}`);

export const resetUserPassword = (userId, { current_password, new_password }) =>
  api.put(`/users/${userId}/reset-password`, { current_password, new_password });

export const savePasswordPolicy = (values) => api.put("/users/password-policy", values);

export const getSecurityRoles = () => api.get("/security/roles");
export const getSecurityActions = () => api.get("/security/actions");
export const createSecurityRole = (values) => api.post("/security/roles", values);
export const updateSecurityRole = (roleId, values) => api.put(`/security/roles/${roleId}`, values);
export const deleteSecurityRole = (roleId) => api.delete(`/security/roles/${roleId}`);
export const getUserAccessRoles = (userId) => api.get(`/security/users/${userId}/roles`);
export const saveUserAccessRoles = (userId, roleIds) =>
  api.put(`/security/users/${userId}/roles`, { role_ids: roleIds });
export const getUserAssetScopes = (userId) => api.get(`/security/users/${userId}/scopes`);
export const saveUserAssetScopes = (userId, scopes) =>
  api.put(`/security/users/${userId}/scopes`, { scopes });
export const getScopeOptions = () => api.get("/security/scope-options");
export const getTenants = () => api.get("/security/tenants");
export const createTenant = (values) => api.post("/security/tenants", values);
export const updateTenant = (tenantId, values) => api.put(`/security/tenants/${tenantId}`, values);
export const deleteTenant = (tenantId) => api.delete(`/security/tenants/${tenantId}`);
export const getOrganizations = () => api.get("/security/organizations");
export const createOrganization = (values) => api.post("/security/organizations", values);
export const updateOrganization = (organizationId, values) => api.put(`/security/organizations/${organizationId}`, values);
export const deleteOrganization = (organizationId) => api.delete(`/security/organizations/${organizationId}`);
export const getUserOrganizations = (userId) => api.get(`/security/users/${userId}/organizations`);
export const saveUserOrganizations = (userId, organizationIds, primaryOrganizationId) =>
  api.put(`/security/users/${userId}/organizations`, {
    organization_ids: organizationIds,
    primary_organization_id: primaryOrganizationId || null,
  });
export const getSecurityAudit = () => api.get("/security/audit");
export const verifySecurityAudit = () => api.get("/security/audit/verify");
export const getMfaStatus = () => api.get("/auth/mfa/status");
export const enrollMfa = () => api.post("/auth/mfa/enroll", {});
export const confirmMfa = (code) => api.post("/auth/mfa/confirm", { code });
export const disableMfa = (code) => api.post("/auth/mfa/disable", { code });
export const getAuthSessions = () => api.get("/auth/sessions");
export const revokeAuthSession = (sessionId) => api.delete(`/auth/sessions/${sessionId}`);
