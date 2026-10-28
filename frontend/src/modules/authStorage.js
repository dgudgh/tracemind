const AUTH_TOKEN_KEY = "mesguard_token";
const AUTH_LOGIN_EPOCH_KEY = "mesguard.loginEpoch.v1";
const ADMIN_SHELL_COLLAPSED_KEY = "mesguard.adminShell.collapsed";

let fallbackToken = "";
let fallbackOverride = false;
let fallbackLoginEpoch = null;
let fallbackAdminShellCollapsed = false;
// A new login can reuse the same token, so token equality alone is insufficient.
let authSessionGeneration = 0;
let loginEpochUnavailable = false;

function rotateLoginEpoch() {
  try {
    // Invalidate the previous login before any write can fail or a page can reload.
    try { localStorage.removeItem(AUTH_LOGIN_EPOCH_KEY); }
    catch {
      localStorage.setItem(AUTH_LOGIN_EPOCH_KEY, "invalidated");
      if (localStorage.getItem(AUTH_LOGIN_EPOCH_KEY) !== "invalidated") throw new Error("Login epoch was not invalidated");
    }
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    const epoch = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    localStorage.setItem(AUTH_LOGIN_EPOCH_KEY, epoch);
    if (localStorage.getItem(AUTH_LOGIN_EPOCH_KEY) !== epoch) throw new Error("Login epoch was not persisted");
    loginEpochUnavailable = false;
    return epoch;
  } catch {
    loginEpochUnavailable = true;
    return null;
  }
}

function currentLoginEpoch() {
  if (loginEpochUnavailable) return null;
  try {
    const epoch = localStorage.getItem(AUTH_LOGIN_EPOCH_KEY);
    if (typeof epoch === "string" && /^[a-f0-9]{32}$/.test(epoch)) return epoch;
    return rotateLoginEpoch();
  } catch {
    loginEpochUnavailable = true;
    return null;
  }
}

export function getAuthToken() {
  try {
    if (fallbackOverride) {
      const persistedEpoch = localStorage.getItem(AUTH_LOGIN_EPOCH_KEY);
      // A failed write must not switch a new login back to the old persisted user.
      if (fallbackLoginEpoch === null) return "";
      if (persistedEpoch === fallbackLoginEpoch) return fallbackToken;
      fallbackOverride = false;
      fallbackToken = "";
    }
    // A successful cross-tab logout is authoritative, not a reason to revive RAM.
    return localStorage.getItem(AUTH_TOKEN_KEY) || "";
  } catch {
    return fallbackToken;
  }
}

export function saveAuthToken(token) {
  authSessionGeneration += 1;
  fallbackToken = token || "";
  fallbackLoginEpoch = rotateLoginEpoch();
  fallbackOverride = true;
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, fallbackToken);
    if (localStorage.getItem(AUTH_TOKEN_KEY) !== fallbackToken) throw new Error("Token was not persisted");
    fallbackOverride = false;
  } catch {
    fallbackToken = token || "";
    loginEpochUnavailable = true;
  }
}

export function clearAuthToken() {
  authSessionGeneration += 1;
  fallbackToken = "";
  fallbackLoginEpoch = rotateLoginEpoch();
  fallbackOverride = true;
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    if (localStorage.getItem(AUTH_TOKEN_KEY) !== null) throw new Error("Token was not removed");
    fallbackOverride = false;
  } catch {
    fallbackToken = "";
    loginEpochUnavailable = true;
  }
}

export function getAuthSession() {
  return { token: getAuthToken(), generation: authSessionGeneration, loginEpoch: currentLoginEpoch() };
}

export function isAuthSessionCurrent(session) {
  return session.generation === authSessionGeneration && session.token === getAuthToken()
    && session.loginEpoch === currentLoginEpoch();
}

export function getAdminShellCollapsed() {
  try {
    return localStorage.getItem(ADMIN_SHELL_COLLAPSED_KEY) === "1";
  } catch {
    return fallbackAdminShellCollapsed;
  }
}

export function saveAdminShellCollapsed(collapsed) {
  fallbackAdminShellCollapsed = Boolean(collapsed);
  try {
    localStorage.setItem(ADMIN_SHELL_COLLAPSED_KEY, fallbackAdminShellCollapsed ? "1" : "0");
  } catch {
    fallbackAdminShellCollapsed = Boolean(collapsed);
  }
}
