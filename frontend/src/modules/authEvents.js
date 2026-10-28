const AUTH_EXPIRED_EVENT = "auth-expired";

export function emitAuthExpired() {
  window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
}

export function onAuthExpired(handler) {
  window.addEventListener(AUTH_EXPIRED_EVENT, handler);
  return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
}
