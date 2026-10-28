const http = require("http");
const https = require("https");

function positiveNumber(value, fallback) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function nonNegativeInteger(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function parseArgs(argv) {
  const args = {
    baseUrl: process.env.FRONTEND_SMOKE_URL || "http://127.0.0.1:3300",
    timeout: positiveNumber(process.env.FRONTEND_SMOKE_TIMEOUT, 10),
    retries: nonNegativeInteger(process.env.FRONTEND_SMOKE_RETRIES, 1),
    checkHealth: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--base-url") {
      args.baseUrl = argv[index + 1];
      index += 1;
    } else if (item === "--timeout") {
      args.timeout = positiveNumber(argv[index + 1], args.timeout);
      index += 1;
    } else if (item === "--retries") {
      args.retries = nonNegativeInteger(argv[index + 1], args.retries);
      index += 1;
    } else if (item === "--check-health") {
      args.checkHealth = true;
    } else if (item === "-h" || item === "--help") {
      console.log("Usage: node scripts/check_frontend_smoke.cjs [--base-url URL] [--timeout SEC] [--retries N] [--check-health]");
      process.exit(0);
    }
  }
  return args;
}

function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function requestTextOnce(url, timeoutSeconds) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const client = parsed.protocol === "https:" ? https : http;
    const request = client.get(parsed, { timeout: timeoutSeconds * 1000 }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        resolve({
          status: response.statusCode || 0,
          headers: response.headers,
          body: Buffer.concat(chunks).toString("utf8"),
        });
      });
    });
    request.on("timeout", () => {
      request.destroy(new Error(`Timeout after ${timeoutSeconds}s`));
    });
    request.on("error", reject);
  });
}

async function requestText(url, timeoutSeconds, retries = 0) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await requestTextOnce(url, timeoutSeconds);
    } catch (error) {
      lastError = error;
      if (attempt < retries) await delay(500);
    }
  }
  throw lastError;
}

function absoluteUrl(baseUrl, maybeRelative) {
  return new URL(maybeRelative, baseUrl).toString();
}

function collectAssetPaths(html) {
  const paths = new Set();
  const regex = /\b(?:src|href)="([^"]+)"/g;
  let match;
  while ((match = regex.exec(html))) {
    const value = match[1];
    if (value.startsWith("/assets/")) {
      paths.add(value);
    }
  }
  return [...paths].sort();
}

function expectedAssetContentType(assetPath) {
  if (assetPath.endsWith(".js")) return "javascript";
  if (assetPath.endsWith(".css")) return "text/css";
  return "";
}

function hasExpectedContentType(contentType, expected) {
  if (!expected) return true;
  return (contentType || "").toLowerCase().includes(expected);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const baseUrl = args.baseUrl.replace(/\/$/, "");
  const failures = [];

  let root;
  try {
    root = await requestText(`${baseUrl}/`, args.timeout, args.retries);
  } catch (error) {
    console.log(JSON.stringify({ ok: false, base_url: baseUrl, error: String(error) }, null, 2));
    return 1;
  }

  if (root.status !== 200) failures.push({ path: "/", status: root.status, error: "root status is not 200" });
  if (!hasExpectedContentType(root.headers["content-type"], "text/html")) {
    failures.push({
      path: "/",
      status: root.status,
      error: "root content-type does not include text/html",
      content_type: root.headers["content-type"] || "",
    });
  }
  if (!root.body.includes('id="root"')) failures.push({ path: "/", status: root.status, error: "missing #root mount node" });
  if (!root.body.includes("TraceMind AI 智能运维平台")) failures.push({ path: "/", status: root.status, error: "missing TraceMind AI 智能运维平台 title text" });

  const assetPaths = collectAssetPaths(root.body);
  if (!assetPaths.length) failures.push({ path: "/", status: root.status, error: "no /assets references found in root HTML" });

  const assetResults = [];
  for (const assetPath of assetPaths) {
    try {
      const response = await requestText(absoluteUrl(baseUrl, assetPath), args.timeout, args.retries);
      const contentType = response.headers["content-type"] || "";
      const expectedContentType = expectedAssetContentType(assetPath);
      assetResults.push({ path: assetPath, status: response.status, content_type: contentType });
      if (response.status !== 200) failures.push({ path: assetPath, status: response.status, error: "asset status is not 200" });
      if (!hasExpectedContentType(contentType, expectedContentType)) {
        failures.push({
          path: assetPath,
          status: response.status,
          error: `asset content-type does not include ${expectedContentType}`,
          content_type: contentType,
        });
      }
    } catch (error) {
      assetResults.push({ path: assetPath, status: null, error: String(error) });
      failures.push({ path: assetPath, status: null, error: String(error) });
    }
  }

  let health = null;
  if (args.checkHealth) {
    try {
      const response = await requestText(`${baseUrl}/health`, args.timeout, args.retries);
      const contentType = response.headers["content-type"] || "";
      health = { status: response.status, content_type: contentType, body: response.body.slice(0, 200) };
      if (response.status !== 200) failures.push({ path: "/health", status: response.status, error: "proxied health status is not 200" });
      if (!hasExpectedContentType(contentType, "application/json")) {
        failures.push({
          path: "/health",
          status: response.status,
          error: "proxied health content-type does not include application/json",
          content_type: contentType,
        });
      }
    } catch (error) {
      health = { status: null, error: String(error) };
      failures.push({ path: "/health", status: null, error: String(error) });
    }
  }

  const report = {
    ok: failures.length === 0,
    base_url: baseUrl,
    root_status: root.status,
    root_content_type: root.headers["content-type"] || "",
    asset_count: assetPaths.length,
    asset_results: assetResults,
    health,
    failure_count: failures.length,
    failures,
  };
  console.log(JSON.stringify(report, null, 2));
  return failures.length ? 1 : 0;
}

main().then((code) => {
  process.exitCode = code;
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
