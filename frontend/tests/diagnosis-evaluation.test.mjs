import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const page = readFileSync(path.join(root, "src", "DiagnosisEvaluationPage.jsx"), "utf8");
const api = readFileSync(path.join(root, "src", "modules", "diagnosisEvaluationApi.js"), "utf8");
const router = readFileSync(path.join(root, "src", "AppPageRouter.jsx"), "utf8");
const menu = readFileSync(path.join(root, "src", "menuConfig.jsx"), "utf8");

test("diagnosis evaluation is a routed system function", () => {
  assert.match(menu, /system\.aiEvaluation/);
  assert.match(menu, /诊断评测/);
  assert.match(router, /DiagnosisEvaluationPage/);
  assert.match(router, /"system\.aiEvaluation"/);
});

test("evaluation API covers datasets cases runs and audits", () => {
  for (const token of ["/datasets/seed", "createCase", "freezeDataset", "cloneDataset", "startRun", "/audits"]) {
    assert.match(api, new RegExp(token.replace("/", "\\/")));
  }
});

test("evaluation page exposes evidence-grounded quality metrics", () => {
  for (const label of ["金标案例库", "Top 1 准确率", "Top 3 覆盖率", "证据覆盖率", "无依据结论率", "P95 延迟", "版本对比", "拒绝项与原因"]) {
    assert.match(page, new RegExp(label));
  }
  assert.match(page, /centered/);
  assert.match(page, /AI 不具备执行数据库变更的权限/);
  assert.match(page, /top1_hit/);
  assert.match(page, /top3_hit/);
  assert.match(page, /degraded_to_rule/);
});
