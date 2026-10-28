export function reloadPage(delayMs = 0) {
  window.setTimeout(() => window.location.reload(), delayMs);
}

export function currentLocationHref() {
  return window.location.href;
}

export function reloadToHash(hash, delayMs = 0) {
  window.setTimeout(() => {
    window.location.hash = hash;
    window.location.reload();
  }, delayMs);
}

export function reloadToLogin(delayMs = 0) {
  reloadToHash("#/login", delayMs);
}

export function openInNewTab(url) {
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened) opened.opener = null;
  return opened;
}

export function navigateCurrent(url) {
  window.location.assign(url);
}

export function consumeQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  const value = params.get(name);
  if (!value) return "";

  const url = new URL(window.location);
  url.searchParams.delete(name);
  window.history.replaceState({}, "", url);
  return value;
}
