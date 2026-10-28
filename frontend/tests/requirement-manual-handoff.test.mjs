import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import { createManualRequirementHandoff, manualRequirementHandoffMatches } from "../src/modules/requirementManualHandoff.js";

const session = { token: "synthetic-handoff-session", generation: 1, loginEpoch: "a".repeat(32) };
const menuItems = [{ key: "perf", label: "性能", children: [{ key: "perf.slowsql", label: "慢 SQL 分析" }] },
  { key: "report.unified", label: "统一报告" }, { key: "asset.center", label: "资产中心" }];
const payload = { kind: "manual_requirement", targetKey: "perf.slowsql", text: "检查近 7 天慢 SQL", title: "不能信任的页面名称", reason: "模型暂不可用", requestId: "synthetic-request" };
const context = { targetKey: "perf.slowsql", instanceId: 7, menuItems, session };
const create = (value = payload, ctx = context) => createManualRequirementHandoff(value, ctx);
const matches = (handoff, override = {}) => manualRequirementHandoffMatches(handoff, {
  activeKey: context.targetKey, instanceId: 7, menuItems, session, ...override,
});

test("manual handoff uses the visible menu title and carries only navigation context", () => {
  const result = create();
  assert.equal(result.title, "慢 SQL 分析");
  assert.equal(result.text, payload.text);
  assert.equal(result.instanceId, "7");
  assert.equal(result.steps.length, 3);
  assert.equal(result.executionResult, undefined);
  assert.equal(result.capability, undefined);
  assert.ok(matches(result, { instanceId: "7" }));
});

test("wrong target, hidden target, menu groups and malformed payloads cannot become handoffs", () => {
  for (const invalid of [null, {}, { ...payload, kind: "execution_result" }, { ...payload, targetKey: "report.unified" },
    { ...payload, text: " " }, { ...payload, text: "x".repeat(2001) }]) assert.equal(create(invalid), null);
  assert.equal(create(payload, { ...context, menuItems: [] }), null);
  assert.equal(create({ ...payload, targetKey: "perf" }, { ...context, targetKey: "perf" }), null);
  assert.equal(create(payload, { ...context, session: null }), null);
  assert.equal(create(payload, { ...context, session: { ...session, loginEpoch: null } }), null);
});

test("handoff cannot follow another route, instance, account session or changed permissions", () => {
  const handoff = create();
  assert.equal(matches(handoff, { activeKey: "report.unified" }), false);
  assert.equal(matches(handoff, { instanceId: 9 }), false);
  assert.equal(matches(handoff, { menuItems: [] }), false);
  for (const changed of [{ ...session, token: "another-session" }, { ...session, generation: 2 },
    { ...session, loginEpoch: "b".repeat(32) }, null]) assert.equal(matches(handoff, { session: changed }), false);
});

test("unselected instance stays unselected and generic pages give no fabricated button labels", () => {
  const handoff = create({ ...payload, targetKey: "asset.center" }, { ...context, targetKey: "asset.center", instanceId: null });
  assert.equal(handoff.instanceId, null);
  assert.equal(matches(handoff, { activeKey: "asset.center", instanceId: undefined }), true);
  assert.equal(matches(handoff, { activeKey: "asset.center", instanceId: 7 }), false);
  assert.match(handoff.steps.join(" "), /填写必要参数/);
  assert.doesNotMatch(handoff.steps.join(" "), /自动|立即执行|生成报告/);
});

const routerSource = readFileSync(new URL("../src/AppPageRouter.jsx", import.meta.url), "utf8");
let navigateSource, departureEffect;
traverseModule.default(parse(routerSource, { sourceType: "module", plugins: ["jsx"] }), {
  VariableDeclarator({ node }) {
    if (node.id.name === "navigateTo") navigateSource = routerSource.slice(node.init.start, node.init.end);
  },
  CallExpression({ node }) {
    if (node.callee.name === "useEffect" && routerSource.slice(node.start, node.end).includes("manualHandoff && !visible")) {
      departureEffect = routerSource.slice(node.arguments[0].start, node.arguments[0].end);
    }
  },
});

function navigateHarness() {
  const events = [];
  const bindings = {
    setExecutionResult: (result) => events.push(["executionResult", result]),
    onManualRequirementHandoff: (...args) => events.push(["handoff", ...args]),
    onHeaderInstanceChange: (id) => events.push(["instance", id]),
    onActiveKeyChange: (key) => events.push(["route", key]),
  };
  const navigate = new Function(...Object.keys(bindings), `return (${navigateSource});`)(...Object.values(bindings));
  return { events, navigate };
}

test("manual navigation never forwards its metadata as a Slow SQL execution result", () => {
  const h = navigateHarness();
  h.navigate("perf.slowsql", 7, payload);
  assert.deepEqual(h.events, [["executionResult", null], ["handoff", payload, "perf.slowsql", 7], ["instance", 7], ["route", "perf.slowsql"]]);
});

test("manual navigation explicitly clears a mismatched instance using the empty-header contract", () => {
  const h = navigateHarness();
  h.navigate("perf.slowsql", null, payload);
  assert.deepEqual(h.events, [["executionResult", null], ["handoff", payload, "perf.slowsql", null], ["instance", undefined], ["route", "perf.slowsql"]]);
});

test("existing execution-result navigation retains its contract and clears manual guidance", () => {
  const h = navigateHarness();
  const result = { engine: "oracle", instanceId: 7, slowSql: { data: { rows: [] } } };
  h.navigate("perf.slowsql", 7, result);
  assert.deepEqual(h.events[0], ["executionResult", { key: "perf.slowsql", instanceId: 7, result }]);
  assert.deepEqual(h.events[1], ["handoff", null, "perf.slowsql", 7]);
  h.navigate("report.unified");
  assert.equal(h.events.filter(([kind]) => kind === "instance").length, 1);
});

test("departure clears handoff memory so coming back does not revive it", () => {
  let handoff = create();
  const run = (visible) => new Function("manualHandoff", "visible", "setManualHandoff", `return (${departureEffect})();`)(handoff, visible, (value) => { handoff = value; });
  run(true);
  assert.ok(handoff);
  run(false);
  assert.equal(handoff, null);
  run(true);
  assert.equal(handoff, null);
});

test("guidance is a dismissible, bounded text surface with no business API actions", () => {
  const component = readFileSync(new URL("../src/ManualRequirementHandoff.jsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/manualRequirementHandoff.css", import.meta.url), "utf8");
  assert.match(component, /aria-label="关闭手动操作指引" onClick=\{onClose\}/);
  assert.match(component, /等待手动操作/);
  assert.match(component, /需求任务尚未启动/);
  assert.doesNotMatch(component, /fetch\(|dispatch|execute|dangerouslySetInnerHTML/);
  assert.match(css, /overflow-wrap: anywhere/);
  assert.match(css, /@media \(max-width: 640px\)/);
  assert.doesNotMatch(css, /min-width:\s*[1-9][0-9]*px/);
});
