import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { reportDocumentLaunchState } from "../src/modules/reportDocumentLauncher.js";

const instance = { id: 7, db_type: "mysql", instance_name: "orders-mysql" };
const capability = {
  capability_id: "report.document.generate", enabled: true, availability: "available",
  execution: "server", handler_status: "wired", plan_executable: true, kind: "collect",
  requires_instance: true, version: 3, definition_version: 3,
  output_schema: "report.document_result/v1", authorized_engines: ["mysql"],
};

test("manual report entry requires the exact authorized v3 typed capability and selected instance", () => {
  assert.equal(reportDocumentLaunchState([capability], [instance], 7).capability, capability);
  assert.match(reportDocumentLaunchState([capability], [instance], undefined).reason, /选择.*实例/);
  for (const patch of [
    { version: 2, definition_version: 2 }, { output_schema: "object" }, { availability: "blocked" },
    { plan_executable: false }, { authorized_engines: ["oracle"] },
  ]) assert.equal(reportDocumentLaunchState([{ ...capability, ...patch }], [instance], 7).capability, null);
  assert.equal(reportDocumentLaunchState([capability, capability], [instance], 7).capability, null);
});

test("opening the analysis-report form has no execution POST and hands created plans to customer workspace", () => {
  const launcher = readFileSync(new URL("../src/ReportDocumentLauncher.jsx", import.meta.url), "utf8");
  assert.match(launcher, />生成分析报告<\/Button>/);
  assert.match(launcher, /onClick=\{\(\) => setFormOpen\(true\)\}/);
  assert.match(launcher, /initialCapabilityId="report\.document\.generate"/);
  assert.match(launcher, /<AssistantGenericCapabilityPanel[\s\S]*customerMode/);
  assert.match(launcher, /report_type: "after_sales"[\s\S]*window_hours: 168/);
  assert.match(launcher, /<AssistantPlanWorkspace[\s\S]*customerMode/);
  assert.match(launcher, /setPlanRequest\(\{ planId \}\)/);
  assert.doesNotMatch(launcher, /requirementText/);
  assert.doesNotMatch(launcher, /createAssistantPlan|executeAssistantPlan|requestAssistantPlanApproval|createReportSnapshot/);
});

test("report page mount and ordinary refresh are read-only; source synchronization stays explicit", () => {
  const page = readFileSync(new URL("../src/UnifiedReportCenterPage.jsx", import.meta.url), "utf8");
  const mount = page.slice(page.indexOf("useEffect(() => {\n    mounted.current = true"), page.indexOf("const refreshCurrent"));
  const refresh = page.slice(page.indexOf("const refreshCurrent"), page.indexOf("const openDetail"));
  assert.match(mount, /loadCatalog\(false\)/);
  assert.doesNotMatch(mount, /loadCatalog\(true\)|syncReportCatalog/);
  assert.match(refresh, /loadCatalog\(false\)/);
  assert.doesNotMatch(refresh, /loadCatalog\(true\)|syncReportCatalog/);
  assert.match(page, /onClick=\{\(\) => loadCatalog\(false, true\)\}>同步来源/);
});
