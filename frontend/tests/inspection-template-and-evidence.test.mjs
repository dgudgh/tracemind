import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runPage = readFileSync(path.join(root, "src/InspectionPages.jsx"), "utf8");
const reportPage = readFileSync(path.join(root, "src/InspectionReportDetailPage.jsx"), "utf8");
const reportArchivePage = readFileSync(path.join(root, "src/ReportPages.jsx"), "utf8");
const inspectionApi = readFileSync(path.join(root, "src/modules/inspectionApi.js"), "utf8");

test("one-click inspection loads real templates and sends the selected policy", () => {
  assert.match(runPage, /listInspectionPolicies\(\)/);
  assert.match(runPage, /runInspectionTask\(engine, instanceId, templateKey === AUTO_TEMPLATE_KEY \? null : templateKey\)/);
  assert.match(runPage, /其他数据库模板（/);
  assert.match(runPage, /适用但已停用（/);
  assert.doesNotMatch(runPage, /options=\{\[\s*\{\s*value:\s*"daily_standard"/);
  assert.match(inspectionApi, /\{\s*policy_key:\s*policyKey\s*\}/);
});

test("template edits preserve collector binding and refresh the run page immediately", () => {
  assert.match(runPage, /normalized\.source_key\s*=\s*String\(item\.source_key\s*\|\|\s*item\.key/);
  assert.match(runPage, /name=\{\[name,\s*"source_key"\]\}\s+hidden/);
  assert.match(runPage, /notifyInspectionPoliciesChanged\(\)/);
  assert.match(runPage, /window\.localStorage\.setItem\(INSPECTION_POLICIES_CHANGED_STORAGE_KEY/);
  assert.match(runPage, /window\.addEventListener\(INSPECTION_POLICIES_CHANGED_EVENT,\s*loadTemplates\)/);
  assert.match(runPage, /window\.addEventListener\("storage",\s*handleStorageChange\)/);
  assert.match(runPage, /label="模板 ID"/);
  assert.match(runPage, /<Input disabled=\{mode === "edit"\}/);
  assert.match(reportPage, /label="模板修订"/);
});

test("template library exposes runtime validation and blocks empty execution mappings", () => {
  assert.match(runPage, /item\.runtime_valid\s*!==\s*false/);
  assert.match(runPage, /运行校验未通过/);
  assert.match(runPage, /selectedTemplate\?\.runtime_valid\s*===\s*false/);
  assert.match(runPage, /row\.executable_item_count\s*\?\?/);
  assert.match(runPage, /运行校验通过/);
  assert.match(runPage, /不可执行/);
  assert.match(runPage, /已配置 \{templateDetail\.item_count\} 项，可执行 \{templateDetail\.executable_item_count\} 项/);
  assert.match(runPage, /execution_unit_count/);
});

test("inspection report exposes SQL and returned rows for every finding", () => {
  assert.match(reportPage, /label:\s*"SQL 与结果"/);
  assert.match(reportPage, /Array\.isArray\(payload\.rows\)\s*\?\s*payload\.rows\s*:\s*payload\.items/);
  assert.match(reportPage, /查看 SQL 与结果/);
  assert.match(reportPage, /label="展示行数"/);
  assert.match(reportPage, /items=\{allFindings\}/);
  assert.match(reportPage, /onViewModule/);
  assert.match(reportPage, /<Modal/);
});

test("inspection reports open in the same centered report modal from every entry", () => {
  assert.match(runPage, /className="inspection-report-modal"/);
  assert.match(reportArchivePage, /className="inspection-report-modal"/);
  assert.match(runPage, /centered[\s\S]*width=\{1360\}/);
  assert.match(reportArchivePage, /centered[\s\S]*width=\{1360\}/);
  assert.doesNotMatch(reportArchivePage, /<Drawer[\s\S]*巡检报告/);
});
