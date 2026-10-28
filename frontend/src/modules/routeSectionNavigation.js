const PREFIX = "tracemind:route-section:";

export function consumeRouteSection(routeKey, fallback) {
  if (!routeKey || typeof window === "undefined") return fallback;
  const key = `${PREFIX}${routeKey}`;
  const section = window.sessionStorage.getItem(key);
  window.sessionStorage.removeItem(key);
  return section || fallback;
}
