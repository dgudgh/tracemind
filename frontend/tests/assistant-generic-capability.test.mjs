import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { schemaFields, initialValues, buildParams, validateValues, isExecutable, effectNeedsApproval, targetProblem, planPayload,
  customerCapabilityName, customerCapabilityFieldLabel, customerCapabilityEnumLabel } from "../src/modules/assistantCapabilityForm.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const panel = readFileSync(path.join(root, "src/AssistantGenericCapabilityPanel.jsx"), "utf8");

const schema = {
  title: "SlowSqlParams",
  type: "object",
  properties: {
    page: { type: "integer", title: "页码", default: 1, minimum: 1, maximum: 1000000 },
    page_size: { type: "integer", default: 50, minimum: 1, maximum: 200 },
    time_range: { anyOf: [{ type: "string", enum: ["1h", "3h", "24h"] }, { type: "null" }], default: null },
    enabled: { anyOf: [{ type: "boolean" }, { type: "null" }] },
  },
  required: ["page"],
};

test("schema fields keep types, enums, bounds and required flags", () => {
  const fields = schemaFields(schema);
  assert.deepEqual(fields.map((field) => field.name), ["page", "page_size", "time_range", "enabled"]);
  assert.equal(fields[0].type, "integer");
  assert.equal(fields[0].required, true);
  assert.equal(fields[1].default, 50);
  assert.deepEqual(fields[2].enum, ["1h", "3h", "24h"]);
  assert.equal(fields[3].type, "boolean");
});

test("initial values only take declared defaults", () => {
  assert.deepEqual(initialValues(schemaFields(schema)), { page: 1, page_size: 50 });
});

test("buildParams emits typed values and omits blank optional fields", () => {
  const fields = schemaFields(schema);
  assert.deepEqual(buildParams(fields, { page: 2, page_size: 20, time_range: undefined, enabled: false }),
    { page: 2, page_size: 20, enabled: false });
});

test("required, integer bounds and enum are enforced before any request", () => {
  const fields = schemaFields(schema);
  assert.match(validateValues(fields, {})[0], /页码/);
  assert.match(validateValues(fields, { page: 0 }).join(" "), /不得小于/);
  assert.match(validateValues(fields, { page: 1.5 }).join(" "), /整数/);
  assert.match(validateValues(fields, { page: 1, time_range: "7d" }).join(" "), /允许取值/);
});

test("only a wired, available server capability is offered", () => {
  const base = { kind: "read", execution: "server", plan_executable: true, availability: "available", handler_status: "wired" };
  assert.equal(isExecutable(base), true);
  assert.equal(isExecutable({ ...base, availability: "blocked" }), false);
  assert.equal(isExecutable({ ...base, plan_executable: false }), false);
  assert.equal(isExecutable({ ...base, execution: "client", handler_status: "client_bound" }), false);
  assert.equal(isExecutable({ ...base, handler_status: "declared_not_wired" }), false);
});

test("effects always require approval, reads do not", () => {
  assert.equal(effectNeedsApproval({ kind: "collect" }), true);
  assert.equal(effectNeedsApproval({ kind: "execute" }), true);
  assert.equal(effectNeedsApproval({ kind: "read" }), false);
  assert.equal(effectNeedsApproval({ kind: "query" }), false);
});

test("target selection follows requires_instance and the engine allow-list", () => {
  const item = { requires_instance: true, authorized_engines: ["mysql"] };
  assert.match(targetProblem(item, [], undefined), /必须选择/);
  assert.match(targetProblem(item, [{ id: 1, db_type: "oracle" }], 1), /引擎/);
  assert.equal(targetProblem(item, [{ id: 1, db_type: "mysql" }], 1), "");
  assert.equal(targetProblem({ requires_instance: false, authorized_engines: ["mysql"] }, [], undefined), "");
});

test("plan payload cannot downgrade an effect to an unapproved plan", () => {
  const effect = { capability_id: "monitor.capacity.execute", definition_version: 1, kind: "collect" };
  const read = { capability_id: "report.catalog.read", definition_version: 1, kind: "read" };
  assert.equal(planPayload(effect, { targetId: 3, params: {}, requestApproval: false, validForSeconds: 60 }).request_approval, true);
  assert.equal(planPayload(read, { targetId: 3, params: {}, requestApproval: false, validForSeconds: 60 }).request_approval, false);
  assert.equal(planPayload(read, { targetId: 3, params: {}, requestApproval: true, validForSeconds: 60 }).request_approval, true);
});

test("customer report labels translate presentation without changing submitted values", () => {
  assert.equal(customerCapabilityName("report.document.generate"), "生成分析报告");
  assert.equal(customerCapabilityFieldLabel({ name: "report_type", label: "Report Type" }), "报告类型");
  assert.equal(customerCapabilityFieldLabel({ name: "window_hours", label: "Window Hours" }), "最近小时数（7 天 = 168 小时）");
  assert.equal(customerCapabilityFieldLabel({ name: "format", label: "Format" }), "报告格式");
  assert.equal(customerCapabilityFieldLabel({ name: "include_ai", label: "Include Ai" }), "使用 AI 分析");
  assert.equal(customerCapabilityEnumLabel("report_type", "after_sales"), "售后分析");
  assert.equal(customerCapabilityEnumLabel("report_type", "unified"), "统一运维");
  assert.equal(customerCapabilityEnumLabel("format", "markdown"), "Markdown");
  assert.equal(customerCapabilityEnumLabel("format", "html"), "HTML");
  assert.equal(customerCapabilityEnumLabel("future", "raw_value"), "raw_value");

  const reportFields = schemaFields({ type: "object", required: ["report_type", "window_hours", "format", "include_ai"], properties: {
    report_type: { type: "string", enum: ["unified", "after_sales"], default: "after_sales" },
    window_hours: { type: "integer", minimum: 1, maximum: 720, default: 168 },
    format: { type: "string", enum: ["markdown", "html"], default: "markdown" },
    include_ai: { type: "boolean", default: true },
  } });
  assert.deepEqual(buildParams(reportFields,
    { report_type: "unified", window_hours: 24, format: "html", include_ai: false }),
  { report_type: "unified", window_hours: 24, format: "html", include_ai: false });
});

test("customer mode hides capability identity and folds technical descriptions", () => {
  assert.match(panel, /title=\{customerMode \? "生成分析报告" : "通用能力清单"\}/);
  assert.match(panel, /label: customerMode \? customerCapabilityName\(item\.capability_id\)/);
  assert.match(panel, /customerMode[\s\S]*<Collapse[\s\S]*label: "技术说明"/);
  assert.match(panel, /customerMode \? customerCapabilityFieldLabel\(field\) : field\.label/);
  assert.match(panel, /customerMode \? customerCapabilityEnumLabel\(field\.name, value\) : String\(value\)/);
});

test("the panel never builds a prompt or a raw request body field", () => {
  for (const forbidden of ["prompt:", "raw_body", "script:", "command:", "sql:"]) {
    assert.equal(panel.includes(forbidden), false, `generic form must not contain ${forbidden}`);
  }
  assert.match(panel, /params_schema/);
  assert.match(panel, /createAssistantPlan/);
  assert.match(panel, /requestAssistantPlanApproval/);
});
