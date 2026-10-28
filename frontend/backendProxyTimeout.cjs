function backendRequestTimeout(req, defaultMs) {
  const pathname = new URL(req.url || "/", "http://localhost").pathname;
  const modelAnalysis = req.method === "POST" && /^\/api\/v1\/slow-sql\/(?:oracle\/\d+|\d+)\/analyze$/.test(pathname);
  return modelAnalysis ? Math.max(defaultMs, 190000) : defaultMs;
}

module.exports = { backendRequestTimeout };
