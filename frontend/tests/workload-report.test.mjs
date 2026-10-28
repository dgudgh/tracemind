import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";


const page = fs.readFileSync(new URL("../src/WorkloadReportPage.jsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/modules/performanceApi.js", import.meta.url), "utf8");
const menu = fs.readFileSync(new URL("../src/menuConfig.jsx", import.meta.url), "utf8");
const router = fs.readFileSync(new URL("../src/AppPageRouter.jsx", import.meta.url), "utf8");


test("four-engine workload report is routed from the performance menu", () => {
  assert.match(menu, /key: "perf\.workload"/);
  assert.match(menu, /label: "引擎深度报告"/);
  assert.match(router, /"perf\.workload": \{ c: WorkloadReportPage, data: "selected" \}/);
});


test("workload report exposes collect, detail, history, and comparison contracts", () => {
  assert.match(api, /getWorkloadReportCapabilities/);
  assert.match(api, /collectWorkloadReport/);
  assert.match(api, /getWorkloadReports/);
  assert.match(api, /compareWorkloadReports/);
  assert.match(page, /Oracle/);
  assert.match(page, /MySQL/);
  assert.match(page, /PostgreSQL/);
  assert.match(page, /SQL Server/);
  assert.match(page, /对比所选/);
});


test("SQL text and Oracle AWR require explicit UI approvals", () => {
  assert.match(page, /include_sql_text: includeSqlText/);
  assert.match(page, /sensitive_data_approved: sensitiveApproved/);
  assert.match(page, /oracle_diagnostics_pack_confirmed: licenseConfirmed/);
  assert.match(page, /仅保存不可逆指纹/);
  assert.match(page, /Diagnostics Pack/);
});


test("detail and compare dialogs are centered and viewport constrained", () => {
  assert.ok((page.match(/<Modal/g) || []).length >= 2);
  assert.ok((page.match(/\n      centered\n/g) || []).length >= 2);
  assert.match(page, /calc\(100vw - 40px\)/);
});
