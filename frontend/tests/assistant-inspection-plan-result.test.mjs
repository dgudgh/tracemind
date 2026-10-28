import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as antd from "antd";
import * as icons from "@ant-design/icons";
import { parse } from "@babel/parser";
import { createServer, transformWithOxc } from "vite";
import * as state from "../src/modules/assistantPlanState.js";
import * as executionState from "../src/InspectionExecutionState.js";
import * as stableKeys from "../src/modules/stableKeys.js";

// Offline fixtures follow plan_adapter._result / jobs.job_view and report.py's detail
// envelope. These exercise production validators/callbacks, not collection or FIELD acceptance.
const job = "a".repeat(30), otherJob = "b".repeat(30);
const id = `pl${"a".repeat(26)}`;
function fixture(status = "success", count = ["success", "partial"].includes(status) ? 1 : 0) {
  const outcome = ["timed_out", "interrupted"].includes(status) ? "unknown" : status;
  const stage = count > 0 && ["success", "partial", "failed"].includes(status) ? "completed" : status;
  const error = ({ partial: "collector_error", failed: "collector_error", timed_out: "timeout",
    interrupted: "lease_lost", cancelled: "cancelled_before_run", expired: "expired_in_queue" })[status] || null;
  const run = { job_id: job, instance_id: 7, db_type: "mysql", status, stage, attempt: 1,
    trigger_type: "capability_plan", policy_revision: "a".repeat(64), timeout_seconds: 300,
    created_at: "2026-10-20T10:00:00Z", started_at: "2026-10-20T10:00:01Z", finished_at: "2026-10-20T10:00:02Z",
    terminal: true, outcome, outcome_kind: error, requires_attention: outcome === "unknown" || outcome === "expired",
    results_published: stage === "completed" && count > 0, cancel_requested: status === "cancelled", can_retry: false, result_count: count };
  const inline = { schema: "inspection.plan_result/v1", job_id: job, instance_id: 7, status, outcome,
    error_code: error, run, report: { available: false, state: "not_synced", artifacts: [] } };
  const stepStatus = outcome === "success" ? "succeeded" : outcome;
  const step = { seq: 1, title: "Inspection", capability_id: "inspection.run.execute", definition_version: 2,
    status: stepStatus, result_ref: { kind: "inspection_job", capability_id: "inspection.run.execute", schema: inline.schema, inline } };
  return { schema: "capability.plan_read/v1", plan_id: id, capability_id: step.capability_id, definition_version: 2,
    instance_id: 7, db_type: "mysql", revision: 1, binding_hash: "a".repeat(64), status: "settled",
    outcome: stepStatus, steps: [step] };
}
function detailFor(plan = fixture()) {
  const inline = plan.steps[0].result_ref.inline, count = inline.run.result_count;
  return { task_id: inline.job_id, task: { task_id: inline.job_id, instance_id: inline.instance_id,
    task_status: inline.outcome, total_count: count, overall_score: inline.outcome === "success" ? 100 : null },
  instance: { id: inline.instance_id, db_type: "mysql", name: "Offline fixture" },
  summary: { total: count, task_status: inline.outcome }, modules: [], category_summary: [], risk_items: [],
  all_findings: Array.from({ length: count }, (_, index) => ({ id: index + 1, task_id: inline.job_id,
    instance_id: inline.instance_id, execution_status: inline.outcome })) };
}
const reference = (plan) => state.inspectionPlanResult(plan, plan.steps[0]);
const stepRows = (plan, index = 0) => plan?.steps?.[index]
  ? state.assistantPlanStepResult(plan, plan.steps[index]).rows : [];
const mutate = (callback, plan = fixture()) => { callback(plan, plan.steps[0], plan.steps[0].result_ref.inline); return plan; };

for (const status of ["success", "partial", "failed", "timed_out", "interrupted", "cancelled", "expired"]) {
  test(`production state recognizes correlated ${status} job without upgrading its verdict`, () => {
    const plan = fixture(status), result = reference(plan), presentation = state.planPresentation(plan);
    assert.equal(result.job_id, job);
    assert.equal(result.step_status, plan.steps[0].status);
    assert.equal(presentation.status, plan.outcome);
    if (status !== "success") assert.notEqual(presentation.color, "success");
    if (status === "partial") assert.equal(presentation.steps[0].status, "partial");
    assert.deepEqual(stepRows(plan), [], "Inspection envelopes never enter generic reader flattening");
  });
}

test("success and partial require actual nonzero published evidence, not artifact availability", () => {
  for (const status of ["success", "partial"]) {
    const plan = fixture(status, 0);
    plan.steps[0].result_ref.inline.report = { available: true, state: "synced", artifacts: [{ id: 9 }] };
    assert.equal(reference(plan), null);
    assert.equal(state.planPresentation(plan).status, "unknown");
  }
  const plan = fixture();
  assert.equal(reference(plan).report.available, false, "A source report does not require a synced index");
  const { steps, ...summary } = plan;
  assert.equal(state.planPresentation(summary).status, "unknown", "Inspection list summaries without evidence cannot prove success");
});

test("wrong target, job, version, schema, status and run evidence fail closed", () => {
  const patches = [
    (p) => { p.instance_id = 8; }, (p) => { p.instance_id = "7"; }, (p) => { p.db_type = "oracle"; },
    (p) => { p.plan_id += "\n"; }, (p) => { p.status = "running"; },
    (_, s) => { s.definition_version = 1; }, (_, s) => { s.capability_id = "inspection.run.query"; },
    (_, s) => { s.seq = 0; }, (_, s) => { s.result_ref.kind = "capability_read"; },
    (_, s) => { s.result_ref.capability_id = "inspection.run.query"; }, (_, s) => { s.result_ref.schema = "inspection.run_read/v1"; },
    (_, s, v) => { v.schema = "inspection.run_read/v1"; }, (_, s, v) => { v.job_id = otherJob; },
    (_, s, v) => { v.job_id = "A".repeat(30); v.run.job_id = v.job_id; },
    (_, s, v) => { v.job_id += "\n"; v.run.job_id = v.job_id; },
    (_, s, v) => { v.instance_id = 8; }, (_, s, v) => { v.run.instance_id = 8; },
    (_, s, v) => { v.run.job_id = otherJob; }, (_, s, v) => { v.status = "failed"; },
    (_, s, v) => { v.outcome = "failed"; }, (_, s, v) => { v.run.outcome = "partial"; },
    (_, s, v) => { v.run.status = "failed"; }, (_, s, v) => { v.run.terminal = false; },
    (_, s, v) => { v.run.terminal = 1; }, (_, s, v) => { v.run.result_count = "1"; },
    (_, s, v) => { v.run.result_count = -1; }, (_, s, v) => { v.run.result_count = 1.5; },
    (_, s, v) => { v.run.result_count = Number.MAX_SAFE_INTEGER + 1; },
    (_, s, v) => { v.run.results_published = false; }, (_, s, v) => { v.run.results_published = 1; },
    (_, s, v) => { v.run.stage = "saving"; }, (_, s, v) => { v.run.finished_at = null; },
    (_, s, v) => { v.run.finished_at = "garbage"; }, (_, s, v) => { v.report = null; },
    (_, s, v) => { v.error_code = {}; }, (_, s, v) => { v.run.finished_at = "1"; },
  ];
  for (const patch of patches) {
    const plan = mutate(patch);
    assert.equal(reference(plan), null, patch.toString());
    assert.notEqual(state.planPresentation(plan).status, "succeeded", patch.toString());
  }
});

test("unknown/partial and force-closed jobs remain unknown, including misleading success blobs", () => {
  const plan = fixture("interrupted");
  plan.outcome = "partial";
  assert.equal(state.planPresentation(plan).status, "unknown", "Unknown alone is not partial evidence");
  plan.steps.unshift({ ...fixture().steps[0], seq: 2 });
  assert.equal(state.planPresentation(plan).status, "partial");
  assert.equal(state.planPresentation(plan).steps[1].status, "unknown");
  plan.outcome = "succeeded";
  assert.equal(state.planPresentation(plan).status, "unknown");
  const forced = fixture("cancelled");
  forced.outcome = "unknown"; forced.steps[0].status = "unknown";
  forced.steps[0].result_ref.inline.error_code = "cancelled_force_closed";
  forced.steps[0].result_ref.inline.run.outcome_kind = "cancelled_force_closed";
  assert.equal(reference(forced).step_status, "unknown");
  assert.equal(state.planPresentation(forced).status, "unknown");
  forced.steps[0].status = "cancelled";
  assert.equal(reference(forced), null);
});

test("all three existing reader products still present and flatten exactly as readers", () => {
  for (const [capability, schema] of [["inspection.run.query", "inspection.run_list/v1"],
    ["inspection.template.read", "inspection.policy_list/v1"], ["operations.capability_matrix.read", "operations.capability_matrix"]]) {
    const inline = { schema, items: [{ name: "saved", password: "hidden" }], total: 1, page: 1, page_size: 20,
      instances: [], engines: [], summary: {} };
    const plan = { plan_id: id, status: "settled", outcome: "succeeded", steps: [{ seq: 1, capability_id: capability,
      definition_version: 1, status: "succeeded", result_ref: { kind: "capability_read", capability_id: capability, schema, inline } }] };
    assert.equal(state.planPresentation(plan).status, "succeeded");
    assert.equal(state.planPresentation(plan).steps[0].label, "读取成功");
    assert.ok(stepRows(plan).some((row) => row.value === "saved"));
    assert.ok(stepRows(plan).every((row) => row.value !== "hidden"));
    assert.equal(reference(plan), null);
  }
});

// Execute unmodified production callback bodies with deterministic hooks and clocks.
// Effect cleanup is invoked directly; this harness is not a DOM/browser or FIELD test.
const source = readFileSync(new URL("../src/AssistantInspectionResult.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const component = ast.program.body.find((node) => node.declaration?.id?.name === "AssistantInspectionResult").declaration;
const statements = component.body.body.filter((node) => node.type !== "ReturnStatement");
const jsx = component.body.body.find((node) => node.type === "ReturnStatement").argument;
const helpers = ast.program.body.filter((node) => ["FunctionDeclaration", "VariableDeclaration"].includes(node.type))
  .map((node) => source.slice(node.start, node.end)).join("\n");
const body = `${helpers}\nconst ${source.slice(component.params[0].start, component.params[0].end)} = props;
${statements.map((node) => source.slice(node.start, node.end)).join("\n")}
return { readReport, detail, enabled, reference, busy, error, tree: ${source.slice(jsx.start, jsx.end)} };`;
const transformedBody = await transformWithOxc(body, "AssistantInspectionResult.jsx", { jsx: { runtime: "classic" } });
const reportSource = readFileSync(new URL("../src/InspectionReportDetailPage.jsx", import.meta.url), "utf8");
const reportAst = parse(reportSource, { sourceType: "module", plugins: ["jsx"] });
const reportScript = reportAst.program.body.filter((node) => node.type !== "ImportDeclaration")
  .map((node) => { const declaration = node.declaration || node; return reportSource.slice(declaration.start, declaration.end); }).join("\n");
const reportCode = await transformWithOxc(reportScript, "InspectionReportDetailPage.jsx", { jsx: { runtime: "classic" } });
const reportDeps = { React, useState: React.useState, ...antd, ...icons, ...executionState, ...stableKeys, ItsmTicketDrawer: () => null };
delete reportDeps.default;
delete reportDeps["module.exports"];
const ReportPage = new Function(...Object.keys(reportDeps), `${reportCode.code}; return InspectionReportDetailPage;`)(...Object.values(reportDeps));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function harness(plan = fixture(), getter = async () => detailFor(plan), options = {}) {
  const slots = [], effects = [], calls = [], timers = new Map();
  let cursor = 0, dirty = true, generation = 1, serial = 0, mounted = true, snapshot, visible = true;
  let props = { plan, step: plan.steps[0], isActive: () => visible };
  const deps = { React, ...antd, ...icons, InspectionReportDetailPage: ReportPage,
    ...state, getAuthSession: () => ({ token: options.token ?? "offline-fixture", generation, loginEpoch: String(generation) }),
    isAuthSessionCurrent: (session) => session.generation === generation,
    getInspectionReportStructuredDetail: (...args) => { calls.push(args); return getter(...args); },
    setTimeout: (callback, delay) => { const timer = ++serial; timers.set(timer, { callback, delay }); return timer; },
    clearTimeout: (timer) => timers.delete(timer),
    useState: (initial) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (value) => {
      assert.equal(mounted, true, "No state publication after real effect cleanup");
      const next = typeof value === "function" ? value(slots[i].value) : value;
      if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true; }
    }]; },
    useRef: (initial) => { const i = cursor++; slots[i] ??= { ref: { current: initial } }; return slots[i].ref; },
    useEffect: (callback, values) => { const i = cursor++, previous = slots[i];
      if (!previous || values.some((value, index) => !Object.is(value, previous.values[index]))) {
        effects.push(() => { previous?.cleanup?.(); slots[i] = { values, cleanup: callback() }; });
      }
    },
  };
  delete deps.default;
  delete deps["module.exports"];
  const render = new Function(...Object.keys(deps), "props", transformedBody.code);
  const settle = async () => {
    for (let i = 0; i < 30; i += 1) {
      if (dirty && mounted) { dirty = false; cursor = 0; snapshot = render(...Object.values(deps), props); while (effects.length) effects.shift()(); }
      await Promise.resolve();
    }
  };
  return { get value() { return snapshot; }, calls, timers, settle,
    close: () => { visible = false; }, session: () => { generation += 1; },
    render: async () => { dirty = true; await settle(); },
    update: async (next) => { props = { ...props, ...next }; dirty = true; await settle(); },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); mounted = false; },
    timeout: async () => { for (const [key, timer] of [...timers]) { timers.delete(key); assert.equal(timer.delay, 90000); timer.callback(); } await settle(); },
  };
}

for (const status of ["success", "partial", "failed"]) {
  test(`explicit ${status} report callback uses only original job and target, and preserves its result`, async () => {
    const plan = fixture(status, 1), detail = detailFor(plan), h = harness(plan, async () => detail);
    await h.settle();
    assert.equal(h.calls.length, 0, "Mount performs no I/O");
    await h.value.readReport(); await h.settle();
    assert.deepEqual(h.calls, [[job, { instance_id: 7 }]]);
    assert.equal(h.value.detail, detail);
    assert.equal(h.value.detail.task.task_status, plan.steps[0].result_ref.inline.outcome);
    assert.equal(h.timers.size, 0);
    await h.render(); assert.equal(h.calls.length, 1, "Re-render never replays the read or executes anything");
  });
}

test("unknown with no published rows, zero-data, forged target and forged job never request a report", async () => {
  const plans = [fixture("interrupted"), fixture("failed"), fixture("cancelled"), fixture("success", 0), fixture("partial", 0),
    mutate((p) => { p.instance_id = 9; }), mutate((p, s, v) => { v.run.job_id = otherJob; })];
  for (const plan of plans) {
    const h = harness(plan); await h.settle(); await h.value.readReport(); await h.settle();
    assert.equal(h.calls.length, 0); assert.equal(h.value.detail, null);
  }
});

test("report response must agree on both task copies, target copies, findings and counts", async () => {
  const patches = [(v) => { v.task_id = otherJob; }, (v) => { v.task.task_id = otherJob; },
    (v) => { v.task.instance_id = 9; }, (v) => { v.instance.id = 9; }, (v) => { v.instance.id = "7"; },
    (v) => { v.all_findings[0].task_id = otherJob; }, (v) => { v.all_findings[0].instance_id = 9; },
    (v) => { v.all_findings = []; }, (v) => { v.task.total_count = 0; }, (v) => { v.summary.total = 0; },
    (v) => { v.task.total_count = 2; }, (v) => { v.risk_items = {}; }];
  for (const patch of patches) {
    const detail = detailFor(); patch(detail);
    const h = harness(fixture(), async () => detail); await h.settle(); await h.value.readReport(); await h.settle();
    assert.equal(h.value.detail, null, patch.toString()); assert.match(h.value.error, /不符/);
    assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
  }
  for (const detail of [null, "<html>not a report</html>", {}]) {
    const h = harness(fixture(), async () => detail); await h.settle(); await h.value.readReport(); await h.settle();
    assert.equal(h.value.detail, null); assert.match(h.value.error, /不符/);
  }
});

test("full large reports are not mistaken for paginated/truncated results", async () => {
  // get_task_rows(...).all() -> one finding per row -> detail.all_findings, without a cap.
  const plan = fixture("success", 5001), detail = detailFor(plan), h = harness(plan, async () => detail);
  await h.settle(); await h.value.readReport(); await h.settle();
  assert.equal(h.value.detail.all_findings.length, 5001); assert.equal(h.value.error, "");
});

test("production JSX embeds the existing report page only after an explicit matching read", async () => {
  const plainText = (tree) => renderToStaticMarkup(tree).replace(/<[^>]*>/g, "");
  for (const status of ["success", "partial", "failed"]) {
    const plan = fixture(status, 1), h = harness(plan); await h.settle();
    assert.match(plainText(h.value.tree), /读取原任务报告/);
    assert.doesNotMatch(plainText(h.value.tree), /TraceMind AI 巡检报告/);
    await h.value.readReport(); await h.settle();
    const text = plainText(h.value.tree);
    assert.match(text, /TraceMind AI 巡检报告/); assert.match(text, new RegExp(job));
    if (status !== "success") assert.match(text, /未生成完整健康分/);
    h.session(); await h.render();
    assert.doesNotMatch(plainText(h.value.tree), /TraceMind AI 巡检报告/);
    assert.match(plainText(h.value.tree), /登录会话已变化/);
  }
  const h = harness(fixture("failed")); await h.settle();
  assert.match(plainText(h.value.tree), /没有已发布的结果行/);
  assert.match(plainText(h.value.tree), /collector_error/);
  assert.match(renderToStaticMarkup(h.value.tree), /disabled/);
});

test("a rows report cannot upgrade partial or unknown to success", async () => {
  for (const status of ["partial", "interrupted"]) {
    const plan = fixture(status, 1), h = harness(plan, async () => detailFor(fixture()));
    await h.settle(); await h.value.readReport(); await h.settle();
    assert.equal(h.calls.length, status === "partial" ? 1 : 0); assert.equal(h.value.detail, null);
    if (status === "partial") assert.match(h.value.error, /不符/);
    assert.notEqual(state.planPresentation(plan).status, "succeeded");
  }
});

for (const status of [401, 403, 404, 500, 504]) {
  test(`report HTTP ${status} remains an explicit read failure without retry or execution`, async () => {
    const h = harness(fixture(), async () => { throw Object.assign(new Error("private upstream body"), { status }); });
    await h.settle(); await h.value.readReport(); await h.settle();
    assert.equal(h.value.detail, null); assert.equal(h.calls.length, 1);
    assert.doesNotMatch(h.value.error, /private upstream body/); assert.ok(h.value.error);
    if (status === 404) assert.match(h.value.error, /未找到或当前不可访问/);
  });
}

test("one read in flight; timeout is bounded, releases busy, ignores late success and never retries", async () => {
  const pending = deferred(), h = harness(fixture(), () => pending.promise); await h.settle();
  const first = h.value.readReport(); await h.value.readReport(); await h.settle();
  assert.equal(h.calls.length, 1); assert.equal(h.value.busy, true);
  await h.timeout(); await first; await h.settle();
  assert.equal(h.value.busy, false); assert.match(h.value.error, /超时/); assert.equal(h.timers.size, 0);
  pending.resolve(detailFor()); await h.settle();
  assert.equal(h.value.detail, null); assert.equal(h.calls.length, 1);
});

for (const boundary of ["close", "session", "unmount", "switch-job", "switch-plan", "switch-revision"]) {
  for (const response of ["resolve", "reject"]) test(`${boundary} fences late report ${response} and captured old callbacks`, async () => {
    const pending = deferred(), h = harness(fixture(), () => pending.promise); await h.settle();
    const oldRead = h.value.readReport, first = oldRead(); await h.settle();
    if (["close", "session", "unmount"].includes(boundary)) h[boundary]();
    else {
      const plan = fixture();
      if (boundary === "switch-job") { plan.steps[0].result_ref.inline.job_id = otherJob; plan.steps[0].result_ref.inline.run.job_id = otherJob; }
      if (boundary === "switch-plan") plan.plan_id = `pl${"b".repeat(26)}`;
      if (boundary === "switch-revision") plan.revision = 2;
      await h.update({ plan, step: plan.steps[0] });
    }
    if (response === "resolve") pending.resolve(detailFor());
    else pending.reject(new Error("late private upstream failure"));
    await first; await h.settle(); await oldRead(); await h.settle();
    assert.equal(h.calls.length, 1, "Old confirmation cannot borrow the new context");
    assert.equal(h.timers.size, 0);
    if (boundary !== "unmount") { await h.render(); assert.equal(h.value.detail, null); }
  });
}

test("a failed explicit re-read removes the preceding report rather than retaining stale success", async () => {
  let attempts = 0;
  const h = harness(fixture(), async () => {
    if (attempts++ === 0) return detailFor();
    throw Object.assign(new Error("permission was revoked"), { status: 403 });
  });
  await h.settle(); await h.value.readReport(); await h.settle(); assert.ok(h.value.detail);
  await h.value.readReport(); await h.settle(); assert.equal(h.value.detail, null); assert.match(h.value.error, /权限被拒绝/);
});

test("a dynamically inactive or throwing activity predicate blocks the original read callback", async () => {
  for (const isActive of [() => false, () => { throw new Error("unavailable parent"); }]) {
    const h = harness(); await h.settle(); const old = h.value.readReport;
    await h.update({ isActive }); await old(); await h.value.readReport(); assert.equal(h.calls.length, 0);
  }
});

test("same-token identity rotation before click does not borrow a newly sampled session", async () => {
  const h = harness(); await h.settle(); const oldRead = h.value.readReport;
  h.session(); await oldRead(); await h.render(); await h.value.readReport();
  assert.equal(h.calls.length, 0); assert.equal(h.value.enabled, false);
});

test("an absent login never dispatches a saved report locator", async () => {
  const h = harness(fixture(), async () => detailFor(), { token: "" });
  await h.settle(); await h.value.readReport(); assert.equal(h.calls.length, 0); assert.equal(h.value.enabled, false);
});

test("effect cleanup cancels the local wait and removes its timer without pretending to abort remote work", async () => {
  const h = harness(fixture(), () => new Promise(() => {})); await h.settle();
  const pending = h.value.readReport(); await h.settle(); assert.equal(h.timers.size, 1);
  h.unmount(); await pending; await h.settle(); assert.equal(h.timers.size, 0); assert.equal(h.calls.length, 1);
});

test("the existing report transport makes exactly the scoped GET with no collection call", async (t) => {
  const vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent",
    server: { middlewareMode: true, hmr: false } });
  try {
    const reports = await vite.ssrLoadModule("/src/modules/reportApi.js"), shared = await vite.ssrLoadModule("/src/api.js");
    const calls = []; t.mock.method(shared.api, "request", async (...args) => { calls.push(args); return detailFor(); });
    await reports.getInspectionReportStructuredDetail(job, { instance_id: 7 });
    assert.deepEqual(calls, [[`/reports/${job}/detail?instance_id=7`, {}]]);
  } finally { await vite.close(); }
});

// Optional artifact is captured from real HTTP/approval/queue/report code over isolated
// SQLite; engine collection remains a fixture. Enable for a cross-language contract run.
test("real backend HTTP JSON agrees with production JS validator and original report callback", {
  skip: !process.env.ASSISTANT_INSPECTION_HTTP_FIXTURES,
}, async () => {
  const cases = JSON.parse(readFileSync(process.env.ASSISTANT_INSPECTION_HTTP_FIXTURES, "utf8"));
  assert.equal(cases.length, 12);
  assert.deepEqual(new Set(cases.map(({ plan }) => plan.db_type)), new Set(["mysql", "oracle", "postgresql", "sqlserver"]));
  assert.deepEqual(new Set(cases.map(({ plan }) => plan.outcome)), new Set(["succeeded", "partial", "failed"]));
  for (const item of cases) {
    const plan = item.plan, result = reference(plan);
    assert.ok(result, `${item.case}: ${JSON.stringify(plan.steps[0])}`);
    assert.equal(state.planPresentation(plan).status, plan.outcome, item.case);
    const h = harness(plan, async () => item.report); await h.settle();
    assert.equal(h.calls.length, 0); await h.value.readReport(); await h.settle();
    assert.equal(h.value.error, "", item.case); assert.equal(h.value.detail, item.report, item.case);
    assert.deepEqual(h.calls, [[result.job_id, { instance_id: plan.instance_id }]]);
  }
});
