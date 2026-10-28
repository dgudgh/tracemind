import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pages = readFileSync(path.join(root, "src", "IncidentSnapshotPages.jsx"), "utf8");
const api = readFileSync(path.join(root, "src", "modules", "incidentSnapshotApi.js"), "utf8");
const router = readFileSync(path.join(root, "src", "AppPageRouter.jsx"), "utf8");

test("incident snapshot console exposes incidents agents and structured policies", () => {
  assert.match(pages, /export function IncidentCenterPage/);
  assert.match(pages, /export function IncidentAgentPage/);
  assert.match(pages, /export function SnapshotPolicyPage/);
  assert.match(pages, /故障时间线/);
  assert.match(pages, /原始证据/);
  assert.match(pages, /因果与根因/);
  assert.match(pages, /include_database_state/);
  assert.match(pages, /数据库状态/);
  assert.match(pages, /Form\.List name="conditions"/);
});

test("incident modals are centered and viewport bounded", () => {
  assert.match(pages, /function AdaptiveModal/);
  assert.match(pages, /\bcentered\b/);
  assert.match(pages, /maxHeight: "calc\(100vh - 180px\)"/);
  assert.doesNotMatch(pages, /<Drawer/);
});

test("incident routes use the shared API client and are registered", () => {
  assert.match(api, /import \{ api, buildQuery \} from "\.\.\/api"/);
  assert.match(api, /\/incident-snapshots\/\$\{snapshotId\}\/causal-graph/);
  assert.match(api, /\/incident-snapshots\/\$\{snapshotId\}\/download/);
  assert.match(api, /api\.downloadFile/);
  assert.match(api, /\/incident-snapshot-health/);
  assert.match(pages, /quality_details\?\.missing_ranges/);
  assert.match(pages, /采集链路需要处理/);
  assert.match(pages, /待续传/);
  assert.match(router, /"diagnose\.incidents": \{ c: IncidentCenterPage/);
  assert.match(router, /"diagnose\.snapshot\.agents": \{ c: IncidentAgentPage/);
  assert.match(router, /"diagnose\.snapshot\.policies": \{ c: SnapshotPolicyPage/);
});
