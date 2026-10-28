const API_PREFIX = "/api/v1";

function isAbsoluteHttpUrl(path) {
  return /^https?:\/\//i.test(path);
}

function currentHref() {
  return window.location.href;
}

export function toApiUrl(path) {
  if (isAbsoluteHttpUrl(path) || path === API_PREFIX || path.startsWith(`${API_PREFIX}/`)) {
    return path;
  }
  return `${API_PREFIX}${path}`;
}

export function toAuthPath(path) {
  if (isAbsoluteHttpUrl(path)) {
    try {
      const url = new URL(path, currentHref());
      return url.pathname.startsWith(`${API_PREFIX}/`) ? url.pathname.slice(API_PREFIX.length) : url.pathname;
    } catch {
      return path;
    }
  }
  return path.startsWith(`${API_PREFIX}/`) ? path.slice(API_PREFIX.length) : path;
}

export function shouldAttachAuth(path) {
  if (!isAbsoluteHttpUrl(path)) return true;
  try {
    const url = new URL(path, currentHref());
    const current = new URL(currentHref());
    const isApiPath = url.pathname === API_PREFIX || url.pathname.startsWith(`${API_PREFIX}/`);
    return url.hostname === current.hostname && isApiPath;
  } catch {
    return false;
  }
}

export function buildQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  });
  const text = query.toString();
  return text ? `?${text}` : "";
}
