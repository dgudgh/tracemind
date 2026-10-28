const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  loadAuditMenuPages,
  loadOpsCapabilities,
} = require("../../scripts/frontend_audit_pages.cjs");

test("browser audits cover every current non-operations leaf page", () => {
  const pages = loadAuditMenuPages();
  const keys = new Set(pages.map((entry) => entry.key));

  for (const key of [
    "diagnose.incidents",
    "diagnose.snapshot.agents",
    "diagnose.snapshot.policies",
    "monitor.observability.logs",
    "monitor.observability.grafana",
    "monitor.observability.apm",
    "ops.governance.objectStorage",
    "protection.automation",
    "system.identity",
  ]) {
    assert.ok(keys.has(key), `audit page list is missing ${key}`);
  }

  assert.equal(keys.has("diagnose.ai"), false);
  assert.equal(keys.has("diagnose.rule"), false);
  assert.equal(keys.has("integration.ecosystem"), false);
  assert.equal(pages.every((entry) => entry.key && entry.label), true);
});

test("operations audit matrix comes from operations menu entries", () => {
  const capabilities = loadOpsCapabilities();
  const byKey = new Map(capabilities.map(([key, label, engines]) => [key, { label, engines }]));

  assert.equal(byKey.get("overview").label, "运行概览");
  assert.deepEqual(byKey.get("security").engines, ["mysql"]);
  assert.ok(byKey.get("ha").engines.includes("oracle"));
  assert.equal(byKey.has("governance.objectStorage"), false);
});

test("functional audit addresses inputs by their stable DOM index", () => {
  const source = fs.readFileSync(
    path.resolve(__dirname, "../../scripts/check_frontend_functional_audit.cjs"),
    "utf8",
  );

  assert.match(source, /locator\("\.admin-content input"\)\.nth\(action\.index\)/);
  assert.doesNotMatch(source, /input:nth-of-type/);
});

test("log analysis search action keeps readable button line height", () => {
  const pageSource = fs.readFileSync(
    path.resolve(__dirname, "../src/ObservabilityCenterSections.jsx"),
    "utf8",
  );
  const styles = fs.readFileSync(path.resolve(__dirname, "../src/styles.css"), "utf8");

  assert.match(pageSource, /integration-workspace observability-log-analysis/);
  assert.match(
    styles,
    /\.observability-log-analysis \.ant-input-search-button\s*\{[^}]*line-height:\s*20px/s,
  );
});
