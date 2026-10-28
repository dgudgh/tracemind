import { emitAuthExpired } from "./modules/authEvents";
import { clearAuthToken, getAuthSession, getAuthToken, isAuthSessionCurrent } from "./modules/authStorage";
import { buildQuery, shouldAttachAuth, toApiUrl, toAuthPath } from "./modules/apiUrl";
import { filenameFromContentDisposition, saveBlobFile } from "./modules/fileExport";

const SESSION_BLOCKING_LICENSE_REASONS = new Set([
  "license_not_found",
  "no_signature",
  "no_public_key",
  "signature_invalid",
  "machine_mismatch",
  "expired",
  "expire_date_invalid",
]);

function authHeaders(path, options = {}, token = getAuthToken()) {
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = { ...(isFormData ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) };
  const authPath = toAuthPath(path);
  if (token && shouldAttachAuth(path) && !["/auth/login", "/auth/external-login", "/auth/mes-login"].includes(authPath)) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

async function errorMessageFromResponse(response) {
  const rawText = await response.text();
  if (!rawText) return `HTTP ${response.status}`;
  try {
    const data = JSON.parse(rawText);
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    return data?.message || JSON.stringify(detail || data);
  } catch {
    return rawText;
  }
}

function httpError(status, message, responseData) {
  return Object.assign(new Error(message), { status }, responseData === undefined ? {} : { responseData });
}

export const api = {
  async request(path, options = {}) {
    const { preserveErrorData = false, authSession, ...fetchOptions } = options;
    const session = authSession === undefined ? getAuthSession() : authSession;
    const bound = authSession !== undefined;
    const rejectChangedSession = () => {
      if (bound && (!session || !isAuthSessionCurrent(session))) {
        throw Object.assign(new Error("Confirmed authentication session changed before dispatch"), { code: "auth_session_changed", status: 409 });
      }
    };
    rejectChangedSession();
    const url = toApiUrl(path);
    const requestOptions = { ...fetchOptions, headers: authHeaders(path, fetchOptions, session.token) };
    rejectChangedSession();
    const response = await fetch(url, requestOptions);
    const rawText = response.status === 204 ? "" : await response.text();
    let data = null;
    if (rawText) {
      try {
        data = JSON.parse(rawText);
      } catch {
        data = null;
      }
    }

    const detail = data?.detail;
    const errorText = typeof detail === "string"
      ? detail
      : detail?.message || data?.message || (data ? JSON.stringify(detail || data) : rawText);
    if (response.status === 401) {
      if (!toAuthPath(path).startsWith("/auth/")) {
        if (isAuthSessionCurrent(session)) {
          clearAuthToken();
          emitAuthExpired();
        }
        throw httpError(response.status, errorText || "登录已过期，请重新登录");
      }
      throw httpError(response.status, errorText || "用户名或密码错误");
    }
    if (response.status === 403) {
      if (data?.reason) {
        if (SESSION_BLOCKING_LICENSE_REASONS.has(data.reason) && isAuthSessionCurrent(session)) {
          clearAuthToken();
          emitAuthExpired();
        }
        throw { licenseBlocked: true, ...data, status: response.status };
      }
      // 版本能力限制（开源版访问企业版接口）需要与普通 403 区分：
      // 页面应就地降级并提示"需企业版"，而不是把 403 文案当业务错误弹出来。
      const editionCode = typeof detail?.code === "string" && detail.code.startsWith("edition_") ? detail.code : null;
      throw Object.assign(httpError(response.status, errorText || "授权未激活"), {
        code: editionCode,
        editionLocked: Boolean(editionCode),
      });
    }
    if (!response.ok) {
      throw httpError(response.status, errorText || `HTTP ${response.status}`, preserveErrorData ? data : undefined);
    }
    if (response.status === 204 || !rawText) return null;
    return data ?? rawText;
  },
  get(path, options = {}) {
    return this.request(path, options === true ? {} : options);
  },
  post(path, body) {
    return this.request(path, { method: "POST", body: JSON.stringify(body || {}) });
  },
  postForm(path, formData) {
    return this.request(path, { method: "POST", body: formData });
  },
  put(path, body) {
    return this.request(path, { method: "PUT", body: JSON.stringify(body || {}) });
  },
  delete(path) {
    return this.request(path, { method: "DELETE" });
  },
  raw(path, options = {}) {
    return fetch(toApiUrl(path), { ...options, headers: authHeaders(path, { ...options, body: options.body }) });
  },
  async download(path, options = {}) {
    const response = await this.raw(path, options);
    if (!response.ok) {
      throw httpError(response.status, await errorMessageFromResponse(response));
    }
    return response.blob();
  },
  async downloadFile(path, filename, options = {}) {
    const response = await this.raw(path, options);
    if (!response.ok) {
      throw httpError(response.status, await errorMessageFromResponse(response));
    }
    const blob = await response.blob();
    const resolvedFilename = filename || filenameFromContentDisposition(response.headers.get("Content-Disposition")) || "download";
    saveBlobFile(blob, resolvedFilename);
    return { blob, filename: resolvedFilename };
  },
};

export async function fetchFrontendHtml(version = Date.now()) {
  const response = await fetch(`/?__frontend_version=${version}`, {
    cache: "no-store",
    headers: { "Cache-Control": "no-cache" },
  });
  return response.ok ? response.text() : "";
}

export { buildQuery };
