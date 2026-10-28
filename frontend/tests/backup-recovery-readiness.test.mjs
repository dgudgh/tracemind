import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("backup execution is gated by side-effect-free preflight", () => {
  const page = read("../src/BackupRecoveryCenterPage.jsx");
  const api = read("../src/modules/backupCenterApi.js");

  assert.match(api, /\/backup-center\/preflight/);
  assert.match(page, /const context = await runPreflight\(false, extra\)/);
  assert.match(page, /if \(preflight\.status !== "passed"\)/);
  assert.match(page, /预检通过，确认在服务器执行/);
  assert.match(page, /本次预检没有创建目录、文件或数据库对象/);
});

test("backup overview distinguishes recovery points from proven recoverability", () => {
  const overview = read("../src/BackupRecoveryOverviewSections.jsx");
  const api = read("../src/modules/backupCenterApi.js");

  assert.match(api, /\/backup-center\/recovery-readiness/);
  for (const label of ["已有恢复点实例", "已证明可恢复", "证据未闭环", "独立校验", "恢复演练 \/ RTO"]) {
    assert.match(overview, new RegExp(label));
  }
  assert.match(overview, /unverified/);
  assert.match(overview, /undrilled/);
  assert.match(overview, /rto_breached/);
});

test("plan-only drivers are visible but cannot trigger server execution", () => {
  const page = read("../src/BackupRecoveryCenterPage.jsx");

  assert.match(page, /仅生成方案/);
  assert.match(page, /disabled=\{!executionEnabled\}/);
  assert.match(page, /capability\.unavailable_reason/);
});
