import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("backup overview exposes an external platform workspace", () => {
  const page = read("../src/BackupRecoveryCenterPage.jsx");
  const api = read("../src/modules/backupPlatformApi.js");
  assert.match(page, /备份平台/);
  assert.match(page, /BackupPlatformPanel/);
  assert.match(api, /\/backup-center\/platform\/configs/);
  assert.match(api, /\/backup-center\/platform\/runs/);
});

test("data governance exposes governed object storage tabs", () => {
  const page = read("../src/DataGovernancePage.jsx");
  const panel = read("../src/DataGovernanceObjectStoragePanel.jsx");
  const api = read("../src/modules/objectStorageApi.js");
  assert.match(page, /对象存储/);
  for (const label of ["存储配置", "归档制品", "传输任务", "校验与恢复"]) {
    assert.match(panel, new RegExp(label));
  }
  assert.match(api, /const ROOT = "\/data-governance\/object-storage"/);
  assert.match(api, /ROOT}\/configs/);
  assert.match(panel, /PlanSafetyNotice/);
});
