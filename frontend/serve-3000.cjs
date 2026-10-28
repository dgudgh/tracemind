const http = require("http");
const fs = require("fs");
const path = require("path");

const distDir = path.resolve(__dirname, "../build/frontend-dist");
const host = process.env.FRONTEND_HOST || "0.0.0.0";
const frontendPort = Number.parseInt(process.env.FRONTEND_PORT || "3300", 10);
const backendPort = Number.parseInt(process.env.BACKEND_PORT || "8800", 10);
const port = Number.isFinite(frontendPort) ? frontendPort : 3300;
const backend = {
  host: process.env.BACKEND_HOST || "127.0.0.1",
  port: Number.isFinite(backendPort) ? backendPort : 8800,
};
const configuredBackendProxyTimeoutMs = Number.parseInt(process.env.BACKEND_PROXY_TIMEOUT_MS || "30000", 10);
const backendProxyTimeoutMs =
  Number.isFinite(configuredBackendProxyTimeoutMs) && configuredBackendProxyTimeoutMs > 0
    ? configuredBackendProxyTimeoutMs
    : 30000;
const proxyClientIpHeader = "x-tracemind-proxy-client-ip";

const mimeTypes = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".ico": "image/x-icon",
};

function sendBackendUnavailable(res, message) {
  if (res.writableEnded || res.destroyed) return;
  if (res.headersSent) {
    res.end();
    return;
  }
  res.writeHead(502, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({ error: "backend_unavailable", message }));
}

function isInsideDirectory(filePath, directory) {
  const relative = path.relative(directory, filePath);
  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function requestPath(req) {
  try {
    return decodeURIComponent((req.url || "/").split("?")[0]);
  } catch {
    return null;
  }
}

const { backendRequestTimeout } = require("./backendProxyTimeout.cjs");

function proxyRequest(req, res) {
  const options = {
    hostname: backend.host,
    port: backend.port,
    path: req.url,
    method: req.method,
    headers: {
      ...req.headers,
      host: `${backend.host}:${backend.port}`,
      [proxyClientIpHeader]: req.socket.remoteAddress || "",
    },
  };

  const proxy = http.request(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
    proxyRes.pipe(res);
  });

  proxy.on("error", (error) => {
    sendBackendUnavailable(res, error.message);
  });

  proxy.setTimeout(backendRequestTimeout(req, backendProxyTimeoutMs), () => {
    proxy.destroy(new Error("backend proxy timeout"));
  });

  req.on("aborted", () => {
    proxy.destroy(new Error("client request aborted"));
  });

  req.pipe(proxy);
}

function serveStatic(req, res) {
  const urlPath = requestPath(req);
  if (!urlPath) {
    res.writeHead(400);
    res.end("Bad request");
    return;
  }
  const requested = urlPath === "/" ? "/index.html" : urlPath;
  const filePath = path.resolve(distDir, `.${requested}`);

  if (!isInsideDirectory(filePath, distDir)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  fs.stat(filePath, (statErr, stat) => {
    const finalPath = !statErr && stat.isFile() ? filePath : path.join(distDir, "index.html");
    fs.readFile(finalPath, (readErr, data) => {
      if (readErr) {
        res.writeHead(404);
        res.end("Not found");
        return;
      }
      const ext = path.extname(finalPath).toLowerCase();
      res.writeHead(200, {
        "content-type": mimeTypes[ext] || "application/octet-stream",
        "cache-control": "no-store, no-cache, must-revalidate, proxy-revalidate",
        "pragma": "no-cache",
        "expires": "0",
      });
      res.end(data);
    });
  });
}

http.createServer((req, res) => {
  if ((req.url || "").startsWith("/api/v1") || (req.url || "").startsWith("/health")) {
    proxyRequest(req, res);
    return;
  }
  serveStatic(req, res);
}).listen(port, host, () => {
  console.log(`TraceMind AI frontend listening on http://${host}:${port}`);
  if (host === "0.0.0.0") {
    console.log(`Local access: http://127.0.0.1:${port}`);
  }
  console.log(`Proxying API to http://${backend.host}:${backend.port}`);
});
