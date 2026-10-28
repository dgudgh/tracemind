import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");
const clients = readFileSync(path.join(root, "src/apiClients.js"), "utf8");
const drawer = readFileSync(path.join(root, "src/InstanceDrawer.jsx"), "utf8");

test("normalized instances can open their native database connection editor", () => {
  assert.match(clients, /get\(id\) \{\s*return api\.get\(`\/instances\/\$\{id\}`\)/);
  assert.match(page, /instanceApi\.get\(row\.native_instance_id\)/);
  assert.match(page, />编辑连接<\/Button>/);
});

test("connection testing and topology discovery are explicit separate actions", () => {
  assert.match(page, /instanceApi\.test\(row\.native_instance_id\)/);
  assert.match(clients, /testConfig\(payload\)[\s\S]*\/instances\/connection-test/);
  assert.match(drawer, /form\.validateFields\(\)/);
  assert.match(drawer, /instanceApi\.testConfig/);
  assert.match(drawer, />测试<\/Button>/);
  assert.match(page, />测试<\/Button>/);
  assert.match(page, /applyAssetDiscovery\(\{ probe_live: true \}\)/);
  assert.doesNotMatch(page, /onSaved=\{async \(\) => \{[^}]*applyDiscovery\(\)/);
  assert.doesNotMatch(page, /if \(initialize &&/);
});

test("native database connections expose an audited delete flow", () => {
  assert.match(page, /instanceApi\.remove\(row\.native_instance_id\)/);
  assert.match(page, /历史巡检、诊断和报告仍会保留/);
  assert.match(page, /delete_connection: "删除数据库连接"/);
  assert.match(page, />删除<\/Button>/);
});
