import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createServer } from "vite";

const planId = `pl${"a".repeat(26)}`;
const capability = "inspection.run.query";
const payload = { capability_id: capability, definition_version: 1, instance_id: 7,
  params: { page: 1, page_size: 100 }, trigger_type: "assistant" };
const resultRef = (id = capability) => {
  const schema = { "inspection.run.query": "inspection.run_list/v1",
    "inspection.template.read": "inspection.policy_list/v1",
    "operations.capability_matrix.read": "operations.capability_matrix" }[id];
  if (id === "operations.capability_matrix.read") {
    return { kind: "capability_read", capability_id: id, schema, inline: {
      engines: [{ key: "mysql", label: "MySQL" }],
      items: [{ key: "mysql.session", title: "Sessions", engines: { mysql: { mode: "read_only", availability: "available" } } }],
      instances: [{ id: 7, name: "DB7", engine: "mysql", connection_status: "unknown", credential_ready: true }],
      selected_instance: { id: 7, name: "DB7", engine: "mysql" },
      summary: { capability_count: 1, engine_count: 1, configured_instance_count: 1,
        mode_counts: { read_only: 1 }, availability_counts: { available: 1 }, execution_permission: false },
    } };
  }
  return { kind: "capability_read", capability_id: id, schema,
    inline: { schema, items: [{ id: 91, status: "failed", name: "Stored task" }], total: 1, page: 1, page_size: 50 } };
};
const plan = (overrides = {}) => ({ schema: "capability.plan_read/v1", plan_id: planId,
  capability_id: capability, status: "settled", outcome: "succeeded", missing_params: [],
  result_ref: { kind: "capability_plan_steps", plan_id: planId, schema: "capability.plan_results/v1", steps: [1] },
  steps: [{ seq: 1, title: "Read saved tasks", capability_id: capability,
    status: "succeeded", result_ref: resultRef() }], ...overrides });
const stepRows = (value, index = 0) => value?.steps?.[index]
  ? state.assistantPlanStepResult(value, value.steps[index]).rows : [];
const needsInput = () => plan({ outcome: "needs_input", missing_params: ["instance_id"], result_ref: null,
  steps: [{ seq: 1, title: "Read saved tasks", capability_id: capability, status: "skipped", result_ref: null }] });
const capabilityDetail = { capability_id: capability, definition_version: 1 };
const capabilityList = { schema: "capability.registry/v1", items: [capabilityDetail] };
const planList = { schema: "capability.plan_list/v1", items: [plan()], total: 1, page: 1, page_size: 50 };
function responseFor(path, options = {}) {
  if (/^\/assistant\/capabilities(?:\?|$)/.test(path)) return capabilityList;
  if (path.startsWith("/assistant/capabilities/")) return { ...capabilityDetail, capability_id: path.split("/").at(-1) };
  if (/^\/assistant\/plans(?:\?|$)/.test(path) && options.method !== "POST") return planList;
  return plan();
}

let vite, transport, state, shared, storage;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname,
    appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  transport = await vite.ssrLoadModule("/src/modules/assistantApi.js");
  state = await vite.ssrLoadModule("/src/modules/assistantPlanState.js");
  shared = await vite.ssrLoadModule("/src/api.js");
  storage = await vite.ssrLoadModule("/src/modules/authStorage.js");
});
test.after(async () => { await vite?.close(); });

test("six T04 routes use shared request/get and preserve request/response contracts", async (t) => {
  const calls = [];
  const response = plan();
  t.mock.method(shared.api, "request", async (...args) => {
    calls.push(args);
    return args[0].includes("/capabilities") || args[0].includes("/plans?") ? responseFor(...args) : response;
  });
  const capabilitiesQuery = { engine: "mysql", kind: "query", execution: "server", include_disabled: false };
  const plansQuery = { page: 2, page_size: 100, status: "settled", outcome: "failed", capability_id: capability, instance_id: 7 };
  assert.equal(await transport.listAssistantCapabilities(capabilitiesQuery), capabilityList);
  assert.deepEqual(await transport.getAssistantCapability(capability), capabilityDetail);
  assert.equal(await transport.createAssistantPlan(payload, { idempotencyKey: "manual:read.1" }), response);
  assert.equal(await transport.listAssistantPlans(plansQuery), planList);
  assert.equal(await transport.getAssistantPlan(planId), response);
  assert.equal(await transport.executeAssistantPlan(planId), response);
  assert.deepEqual(calls, [
    [`/assistant/capabilities${shared.buildQuery(capabilitiesQuery)}`, {}],
    [`/assistant/capabilities/${capability}`, {}],
    ["/assistant/plans", { method: "POST", body: JSON.stringify(payload),
      headers: { "Idempotency-Key": "manual:read.1" }, preserveErrorData: true }],
    [`/assistant/plans${shared.buildQuery(plansQuery)}`, {}],
    [`/assistant/plans/${planId}`, {}],
    [`/assistant/plans/${planId}/execute`, { method: "POST", body: "{}" }],
  ]);
  assert.deepEqual(JSON.parse(calls[2][1].body), payload);
});

test("plan reads preserve a correlated durable execution locator and reject malformed queue metadata", async (t) => {
  let response = plan({ execution_batch_id: `CB${"b".repeat(28)}`, execution_queue_state: "queued" });
  t.mock.method(shared.api, "request", async () => response);
  const queued = await transport.executeAssistantPlan(planId);
  assert.equal(queued.execution_batch_id, response.execution_batch_id);
  assert.equal(queued.execution_queue_state, "queued");
  assert.equal(await transport.getAssistantPlan(planId), response);
  for (response of [
    plan({ execution_batch_id: `CB${"b".repeat(28)}` }),
    plan({ execution_queue_state: "queued" }),
    plan({ execution_batch_id: `CB${"b".repeat(27)}`, execution_queue_state: "queued" }),
    plan({ execution_batch_id: `CB${"b".repeat(28)}`, execution_queue_state: "ownership_lost" }),
  ]) {
    await assert.rejects(transport.getAssistantPlan(planId), { code: "assistant_response_invalid" });
    await assert.rejects(transport.executeAssistantPlan(planId), { code: "assistant_response_invalid" });
  }
});

test("default list queries and create add no role, dynamic fields, execute, sync or retries", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (...args) => { calls.push(args); return responseFor(...args); });
  await transport.listAssistantCapabilities();
  await transport.listAssistantPlans();
  await transport.createAssistantPlan(payload);
  assert.deepEqual(calls.map(([path]) => path), ["/assistant/capabilities", "/assistant/plans", "/assistant/plans"]);
  assert.deepEqual(calls[2][1].headers, {});
  assert.deepEqual(JSON.parse(calls[2][1].body), payload);
});

test("path IDs reject traversal, query, encodings, wrong segments, lengths and trailing newlines before requests", async (t) => {
  let requests = 0;
  t.mock.method(shared.api, "request", async () => { requests += 1; });
  for (const id of [undefined, null, 7, {}, "", "inspection.run", "inspection.run.query.extra", "Inspection.run.query",
    "inspection.run.query/execute", "inspection.run.query?role=admin", "inspection.run.%2f", "../run/query",
    `a.b.${"x".repeat(61)}`, "inspection.run.query\n", "inspection.run.query\r", "inspection.run.query\u2028"]) {
    assert.throws(() => transport.getAssistantCapability(id), /Invalid assistant capability ID/);
  }
  for (const id of [undefined, null, 7, {}, "", `pl${"a".repeat(25)}`, `pl${"a".repeat(27)}`,
    `pl${"A".repeat(26)}`, `${planId}/execute`, `${planId}?role=admin`, `../${planId}`, `${planId}\n`]) {
    assert.throws(() => transport.getAssistantPlan(id), /Invalid assistant plan ID/);
    assert.throws(() => transport.executeAssistantPlan(id), /Invalid assistant plan ID/);
  }
  assert.equal(requests, 0);
});

test("maximum capability ID is accepted and explicit idempotency keys are validated without echoing values", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (...args) => { calls.push(args); return responseFor(...args); });
  await transport.getAssistantCapability(`a.b.${"x".repeat(60)}`);
  await transport.createAssistantPlan(payload, { idempotencyKey: "x".repeat(128) });
  for (const idempotencyKey of [null, 1, "", "x".repeat(129), "credential\r\nAuthorization: hidden", "key\n"]) {
    await assert.rejects(transport.createAssistantPlan(payload, { idempotencyKey }),
      (error) => error instanceof TypeError && error.message === "Invalid assistant idempotency key");
  }
  assert.equal(calls.length, 2);
});

test("only exact saved needs_input 422 response is returned intact, including plan ID", async (t) => {
  const saved = needsInput();
  const error = Object.assign(new Error("missing input"), { status: 422, responseData: saved });
  t.mock.method(shared.api, "request", async () => { throw error; });
  assert.equal(await transport.createAssistantPlan(payload), saved);
  assert.equal(saved.plan_id, planId);
  assert.equal(state.planPresentation(saved).status, "needs_input");
});

test("ordinary and malformed 422 errors never become saved plans", async (t) => {
  let responseData;
  const error = Object.assign(new Error("invalid request"), { status: 422 });
  t.mock.method(shared.api, "request", async () => { error.responseData = responseData; throw error; });
  for (responseData of [undefined, null, {}, { detail: { code: "invalid_request" } },
    { ...needsInput(), schema: "other/v1" }, { ...needsInput(), plan_id: null },
    { ...needsInput(), plan_id: `${planId}\n` }, { ...needsInput(), status: "draft" },
    { ...needsInput(), outcome: "succeeded" }, { ...needsInput(), missing_params: [] },
    { ...needsInput(), missing_params: [7] }, { ...needsInput(), steps: null }]) {
    await assert.rejects(transport.createAssistantPlan(payload), (actual) => actual === error);
  }
});

for (const status of [401, 403, 409, 503]) {
  test(`HTTP ${status} is never swallowed, even with a saved-plan-shaped body`, async (t) => {
    const error = Object.assign(new Error("safe error code"), { status, responseData: needsInput() });
    let calls = 0;
    t.mock.method(shared.api, "request", async () => { calls += 1; throw error; });
    for (const request of [() => transport.listAssistantCapabilities(), () => transport.getAssistantCapability(capability),
      () => transport.createAssistantPlan(payload), () => transport.listAssistantPlans(),
      () => transport.getAssistantPlan(planId), () => transport.executeAssistantPlan(planId)]) {
      await assert.rejects(request(), (actual) => actual === error);
    }
    assert.equal(calls, 6);
  });
}

test("network failures remain failures with no implicit retry", async (t) => {
  const error = new TypeError("network unavailable");
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; throw error; });
  await assert.rejects(transport.createAssistantPlan(payload), (actual) => actual === error);
  await assert.rejects(transport.executeAssistantPlan(planId), (actual) => actual === error);
  assert.equal(calls, 2);
});

test("all successful routes reject HTML, null, arrays and malformed envelopes with a safe protocol error", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  const requests = [() => transport.listAssistantCapabilities(), () => transport.getAssistantCapability(capability),
    () => transport.createAssistantPlan(payload), () => transport.listAssistantPlans(),
    () => transport.getAssistantPlan(planId), () => transport.executeAssistantPlan(planId)];
  for (response of [undefined, null, "<html>password=do-not-echo</html>", [], {}, { schema: "future/v1", items: [] }]) {
    for (const request of requests) {
      await assert.rejects(request(), (error) => error.code === "assistant_response_invalid"
        && error.message === "Invalid assistant API response" && !Object.hasOwn(error, "responseData"));
    }
  }
});

test("list response contracts reject malformed items and pagination without inventing empty rows", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (response of [{ ...capabilityList, items: null }, { ...capabilityList, items: {} }, { ...capabilityList, items: [null] }]) {
    await assert.rejects(transport.listAssistantCapabilities(), { code: "assistant_response_invalid" });
  }
  for (response of [{ ...planList, items: null }, { ...planList, items: [null] }, { ...planList, total: "1" },
    { ...planList, total: -1 }, { ...planList, total: Infinity }, { ...planList, page: 0 },
    { ...planList, page: 1000001 }, { ...planList, page_size: 101 }, { ...planList, page_size: 0 },
    { ...planList, page_size: 1, items: [plan(), plan()] },
    { ...planList, items: [plan({ plan_id: "invalid" })] }, { ...planList, items: [plan({ status: null })] }]) {
    await assert.rejects(transport.listAssistantPlans(), { code: "assistant_response_invalid" });
  }
  response = { schema: "capability.plan_list/v1", items: [], total: 0, page: 1, page_size: 100 };
  assert.equal(await transport.listAssistantPlans(), response);
  response = { schema: "capability.registry/v1", items: [] };
  assert.equal(await transport.listAssistantCapabilities(), response);
});

test("read envelopes bound state strings and step objects but preserve unknown future state values", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (response of [plan({ plan_id: `${planId}\n` }), plan({ steps: null }), plan({ steps: [null] }),
    plan({ status: null }), plan({ status: "x".repeat(25) }), plan({ outcome: undefined }),
    plan({ outcome: {} }), plan({ outcome: "x".repeat(25) }), plan({ steps: [{ status: "" }] }),
    plan({ steps: Array(21).fill({ status: "pending" }) })]) {
    for (const request of [() => transport.createAssistantPlan(payload), () => transport.getAssistantPlan(planId),
      () => transport.executeAssistantPlan(planId)]) {
      await assert.rejects(request(), { code: "assistant_response_invalid" });
    }
  }
  response = plan({ status: "future", outcome: "future_outcome", steps: [{ status: "future" }] });
  assert.equal(await transport.getAssistantPlan(planId), response);
  assert.equal(state.planPresentation(response).status, "unknown");
  response = { ...capabilityDetail, capability_id: "inspection.template.read" };
  await assert.rejects(transport.getAssistantCapability(capability), { code: "assistant_response_invalid" });
});

test("detail and execution reject another valid plan ID instead of replacing the selected plan", async (t) => {
  t.mock.method(shared.api, "request", async () => plan({ plan_id: `pl${"b".repeat(26)}` }));
  await assert.rejects(transport.getAssistantPlan(planId), { code: "assistant_response_invalid" });
  await assert.rejects(transport.executeAssistantPlan(planId), { code: "assistant_response_invalid" });
  assert.equal((await transport.createAssistantPlan(payload)).plan_id, `pl${"b".repeat(26)}`);
});

test("actual shared HTTP 200 HTML and empty success responses fail protocol validation", async (t) => {
  let response = new Response("<html>private-body</html>", { status: 200, headers: { "Content-Type": "text/html" } });
  t.mock.method(globalThis, "fetch", async () => response);
  await assert.rejects(transport.listAssistantCapabilities(), { code: "assistant_response_invalid", message: "Invalid assistant API response" });
  response = new Response(null, { status: 204 });
  await assert.rejects(transport.getAssistantPlan(planId), { code: "assistant_response_invalid" });
  response = new Response("null", { status: 200 });
  await assert.rejects(transport.executeAssistantPlan(planId), { code: "assistant_response_invalid" });
});

test("actual shared request opt-in preserves saved 422 data and strips the non-RequestInit flag", async (t) => {
  const saved = needsInput();
  const calls = [];
  t.mock.method(globalThis, "fetch", async (...args) => {
    calls.push(args); return new Response(JSON.stringify(saved), { status: 422 });
  });
  assert.deepEqual(await transport.createAssistantPlan(payload, { idempotencyKey: "read:1" }), saved);
  assert.equal(calls[0][0], "/api/v1/assistant/plans");
  assert.equal(Object.hasOwn(calls[0][1], "preserveErrorData"), false);
  assert.equal(calls[0][1].headers["Idempotency-Key"], "read:1");
  await assert.rejects(shared.api.get("/assistant/plans"), (error) => error.status === 422 && !Object.hasOwn(error, "responseData"));
});

test("shared authentication expiration is retained; transport never logs credentials or rejected bodies", async (t) => {
  const previousWindow = globalThis.window;
  const logs = [];
  const events = new EventTarget();
  let expirations = 0;
  events.addEventListener("auth-expired", () => { expirations += 1; });
  globalThis.window = events;
  for (const method of ["log", "warn", "error", "info", "debug"]) t.mock.method(console, method, (...args) => { logs.push(args); });
  storage.saveAuthToken("test-only-sensitive-token");
  t.mock.method(globalThis, "fetch", async (_path, options) => {
    assert.equal(options.headers.Authorization, "Bearer test-only-sensitive-token");
    return new Response(JSON.stringify({ detail: { code: "authentication_required" } }), { status: 401 });
  });
  try {
    await assert.rejects(transport.createAssistantPlan(payload), (error) => error.status === 401);
    assert.equal(expirations, 1);
    assert.equal(storage.getAuthToken(), "");
    assert.deepEqual(logs, []);
  } finally {
    storage.clearAuthToken();
    if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow;
  }
});

test("transport is shared-only and pure state has no I/O, React, logging or execution side effects", () => {
  const apiSource = readFileSync(new URL("../src/modules/assistantApi.js", import.meta.url), "utf8");
  const stateSource = readFileSync(new URL("../src/modules/assistantPlanState.js", import.meta.url), "utf8");
  assert.doesNotMatch(apiSource, /\bfetch\s*\(|api\.raw|console\.|setInterval|setTimeout/);
  assert.doesNotMatch(stateSource, /\bimport\b|\bfetch\s*\(|console\.|localStorage|sessionStorage|setInterval|setTimeout|useState|useRef/);
});

test("persisted status and business outcome, not HTTP or inline job status, determine success", () => {
  const saved = plan();
  const view = state.planPresentation(saved);
  assert.deepEqual(Object.keys(view), ["status", "label", "color", "percent", "summary", "resultStatus", "resultSummary", "steps"]);
  assert.equal(view.status, "succeeded");
  assert.equal(view.color, "success");
  assert.equal(view.percent, 100);
  assert.equal(view.steps[0].status, "succeeded");
  assert.match(view.summary, /不代表/);
  assert.ok(stepRows(saved).some((row) => row.field === "items[0].status" && row.value === "failed"));
  assert.equal(saved.steps[0].result_ref.inline.items[0].status, "failed");
  const { steps, result_ref, ...summary } = saved;
  assert.equal(state.planPresentation(summary).status, "succeeded");
  assert.deepEqual(stepRows(summary), []);
});

for (const outcome of ["partial", "failed", "cancelled", "expired", "unknown", "plan_only", "blocked", "unsupported", "needs_input"]) {
  test(`settled ${outcome} keeps its business meaning and never becomes success`, () => {
    const saved = plan({ outcome });
    const view = state.planPresentation(saved);
    assert.equal(view.status, outcome);
    assert.notEqual(view.color, "success");
    if (outcome !== "partial") assert.deepEqual(stepRows(saved), []);
    if (["plan_only", "blocked", "unsupported", "needs_input"].includes(outcome)) assert.equal(view.percent, 0);
  });
}

test("unknown, legacy, missing and conflicting states never claim success or expose result evidence", () => {
  for (const saved of [null, {}, plan({ status: "completed" }), plan({ status: "succeeded" }),
    plan({ status: "future" }), plan({ outcome: "success" }), plan({ outcome: "future" }),
    plan({ outcome: null }), plan({ plan_id: null }), plan({ plan_id: `${planId}\n` }),
    plan({ status: "running", outcome: "succeeded" }), plan({ status: "validated", outcome: "succeeded" }),
    plan({ outcome: "__proto__" }), plan({ status: "constructor" })]) {
    const view = state.planPresentation(saved);
    assert.equal(view.status, "unknown");
    assert.notEqual(view.color, "success");
    assert.ok(view.steps.every((step) => step.status !== "succeeded"));
    assert.deepEqual(stepRows(saved), []);
  }
});

test("active lifecycle stays active without results, and closing a modal cannot cancel a running plan", () => {
  for (const status of ["draft", "validated", "awaiting_approval", "approved", "running"]) {
    const saved = plan({ status, outcome: null, modalOpen: false });
    const before = structuredClone(saved);
    const view = state.planPresentation(saved);
    assert.equal(view.status, status);
    assert.notEqual(view.color, "success");
    assert.ok(view.percent < 100);
    assert.deepEqual(stepRows(saved), []);
    assert.deepEqual(saved, before);
    if (status === "running") assert.match(view.summary, /关闭窗口不会取消/);
  }
});

test("late saved result blobs without settled success/partial cannot turn running or unknown plans into success", () => {
  const late = plan({ status: "running", outcome: null });
  assert.equal(state.planPresentation(late).status, "running");
  assert.deepEqual(stepRows(late), []);
  const settled = plan();
  assert.equal(state.planPresentation(settled).status, "succeeded");
  assert.equal(state.planPresentation(late).status, "running");
  assert.equal(state.planPresentation(plan({ outcome: "unknown" })).steps[0].status, "unknown");
});

test("known and generic object capability reads are projected without changing execution truth", () => {
  for (const id of [capability, "inspection.template.read", "operations.capability_matrix.read"]) {
    const saved = plan({ steps: [{ seq: 1, capability_id: id, status: "succeeded", result_ref: resultRef(id) }] });
    assert.ok(stepRows(saved).length > 0);
  }
  for (const ref of [null, {}, { ...resultRef(), kind: "url", url: "https://invalid.example/secret" },
    { ...resultRef(), schema: "future/v1" }, { ...resultRef(), capability_id: "inspection.template.read" },
    { ...resultRef(), inline: null }, { ...resultRef(), inline: { schema: "future/v1", items: [] } },
    { ...resultRef(), inline: { schema: resultRef().schema, items: {} } }]) {
    const saved = plan({ steps: [{ seq: 1, capability_id: capability, status: "succeeded", result_ref: ref }] });
    assert.deepEqual(stepRows(saved), []);
    assert.equal(state.planPresentation(saved).status, "succeeded");
    assert.equal(state.planPresentation(saved).steps[0].status, "succeeded");
    assert.equal(state.planPresentation(saved).steps[0].resultStatus, "unsupported");
  }
  for (const id of ["future.capability.read", "constructor", "__proto__"]) {
    const ref = { ...resultRef(), capability_id: id };
    assert.deepEqual(stepRows(plan({ steps: [{ seq: 1, capability_id: id, status: "succeeded", result_ref: ref }] })), []);
  }
  const id = "diagnosis.investigation.read";
  const generic = { kind: "capability_read", capability_id: id, schema: "object", inline: {
    items: [{ state: "degraded", password: "must-not-appear" }], total: 1, page: 1, page_size: 20,
    matches: [], captures: [], probe_leases: [], kernel_capabilities: [],
  } };
  const saved = plan({ capability_id: id, steps: [{ seq: 1, capability_id: id, definition_version: 2,
    status: "succeeded", result_ref: generic }] });
  const view = state.planPresentation(saved);
  assert.equal(view.status, "succeeded");
  assert.equal(view.resultStatus, "available");
  assert.equal(view.steps[0].resultStatus, "available");
  assert.ok(stepRows(saved).some((row) => row.field === "items[0].state" && row.value === "degraded"));
  assert.equal(JSON.stringify(stepRows(saved)).includes("must-not-appear"), false);
  assert.deepEqual(stepRows(plan({ steps: [], result_ref: resultRef() })), []);
});

test("detail success requires coherent terminal step states but not a frontend result adapter", () => {
  for (const overrides of [{ steps: [] }, { steps: undefined }, { steps: null },
    { steps: [null] }, { steps: Array(21).fill(plan().steps[0]) },
    { steps: [{ ...plan().steps[0], status: "failed" }] },
    { steps: [plan().steps[0], { seq: 2, capability_id: capability, status: "failed", result_ref: null }] }]) {
    const saved = plan(overrides);
    const view = state.planPresentation(saved);
    assert.equal(view.status, "unknown");
    assert.notEqual(view.color, "success");
    assert.equal(view.percent, 0);
    assert.deepEqual(stepRows(saved), []);
  }
  for (const steps of [[{ ...plan().steps[0], result_ref: null }],
    [{ ...plan().steps[0], result_ref: { ...resultRef(), schema: "future/v1" } }],
    [plan().steps[0], { seq: 2, capability_id: capability, status: "succeeded", result_ref: null }]]) {
    const view = state.planPresentation(plan({ steps }));
    assert.equal(view.status, "succeeded");
    assert.equal(view.resultStatus, steps.length === 2 ? "partially_supported" : "unsupported");
  }
  const { steps, ...summary } = plan();
  assert.equal(state.planPresentation(summary).status, "succeeded");
  assert.deepEqual(stepRows(summary), []);
});

test("partial detail requires coherent terminal steps but preserves unsupported result formats separately", () => {
  for (const steps of [[], undefined, [{ ...plan().steps[0], status: "unknown" }]]) {
    const saved = plan({ outcome: "partial", steps });
    const view = state.planPresentation(saved);
    assert.equal(view.status, "unknown");
    assert.equal(view.percent, 0);
    assert.deepEqual(stepRows(saved), []);
  }
  const saved = plan({ outcome: "partial", steps: [{ ...plan().steps[0], result_ref: null }] });
  assert.equal(state.planPresentation(saved).status, "partial");
  assert.equal(state.planPresentation(saved).resultStatus, "unsupported");
});

test("all three real read-product shapes and empty reads succeed without implying database health", () => {
  for (const id of [capability, "inspection.template.read", "operations.capability_matrix.read"]) {
    for (const empty of [false, true]) {
      const ref = resultRef(id);
      if (empty) {
        ref.inline.items = [];
        if (id === "operations.capability_matrix.read") {
          ref.inline.instances = [];
          ref.inline.selected_instance = null;
          ref.inline.summary.capability_count = 0;
          ref.inline.summary.configured_instance_count = 0;
        } else ref.inline.total = 0;
      }
      const saved = plan({ capability_id: id, steps: [{ seq: 1, title: "Read", capability_id: id, status: "succeeded", result_ref: ref }] });
      const view = state.planPresentation(saved);
      assert.equal(view.status, "succeeded");
      assert.equal(view.color, "success");
      assert.equal(view.percent, 100);
      assert.equal(view.steps[0].status, "succeeded");
      assert.match(view.summary, /读取成功不代表巡检成功、数据库健康/);
      assert.ok(stepRows(saved).length > 0);
    }
  }
  const ref = resultRef("operations.capability_matrix.read");
  assert.equal(Object.hasOwn(ref.inline, "schema"), false);
  for (const inline of [{ items: [] }, { ...ref.inline, engines: null }, { ...ref.inline, summary: [] },
    { ...ref.inline, instances: null }]) {
    const saved = plan({ steps: [{ seq: 1, capability_id: ref.capability_id, status: "succeeded", result_ref: { ...ref, inline } }] });
    assert.equal(state.planPresentation(saved).status, "succeeded");
    assert.equal(state.planPresentation(saved).resultStatus, "unsupported");
    assert.deepEqual(stepRows(saved), []);
  }
});

test("empty investigation context is successful but explicitly says no root-cause conclusion exists", () => {
  const id = "diagnosis.investigation.read";
  const saved = plan({ capability_id: id, steps: [{ seq: 1, title: "Investigation", capability_id: id,
    definition_version: 2, status: "succeeded", result_ref: { kind: "capability_read", capability_id: id,
      schema: "object", inline: { items: [], total: 0, page: 1, page_size: 20, matches: [], captures: [],
        probe_leases: [], kernel_capabilities: [] } } }] });
  const view = state.planPresentation(saved);
  assert.equal(view.status, "succeeded");
  assert.equal(view.resultStatus, "empty");
  assert.match(view.resultSummary, /读取成功，暂无上下文；尚未形成根因结论/);
  assert.equal(view.steps[0].resultStatus, "empty");
  assert.ok(stepRows(saved).some((row) => row.field === "total" && row.value === 0));
});

test("typed root-cause effects expose the persisted incident without treating it as a generic object", () => {
  const id = "diagnosis.root_cause.execute";
  const inline = { schema: "diagnosis.root_cause_result/v1", outcome: "success", instance_id: 7,
    incident_id: 42, incident_no: "INC-20261023120000-abc123", status: "root_caused", severity: "high",
    root_cause_summary: "锁等待是当前有证据支持的根因候选", primary_layer: "database", evidence_count: 8,
    root_cause_count: 1, execution_status: "success", data_quality: "sufficient",
    conclusion_status: "hypotheses_require_validation", sampling: { requested_time_window: "2026-10-23 10:00-11:00",
      impact_scope: "订单提交", collection_mode: "current_snapshot", historical_replay_performed: false } };
  const step = { seq: 1, title: "RCA", capability_id: id, definition_version: 1, status: "succeeded",
    result_ref: { kind: "capability_effect", capability_id: id, schema: inline.schema, inline } };
  const saved = plan({ capability_id: id, definition_version: 1, instance_id: 7, steps: [step] });
  const typed = state.rootCausePlanResult(saved, step);
  assert.equal(typed.incident_id, 42);
  assert.equal(typed.sampling.historical_replay_performed, false);
  assert.equal(state.assistantPlanStepResult(saved, step).kind, "root_cause");
  assert.equal(state.planPresentation(saved).status, "succeeded");
  assert.equal(state.planPresentation(saved).steps[0].label, "诊断完成");
  assert.equal(state.planPresentation(saved).resultStatus, "available");
  assert.match(state.planPresentation(saved).summary, /诊断事件/);
  assert.deepEqual(stepRows(saved), []);

  const malformed = structuredClone(saved);
  malformed.steps[0].result_ref.inline.instance_id = 8;
  assert.equal(state.rootCausePlanResult(malformed, malformed.steps[0]), null);
  assert.equal(state.planPresentation(malformed).status, "succeeded");
  assert.equal(state.planPresentation(malformed).resultStatus, "unsupported");

  const failedInline = { ...inline, outcome: "failed", status: "failed", execution_status: "failed",
    error_code: "diagnosis_failed" };
  const failedStep = { ...step, status: "failed", result_ref: { ...step.result_ref, inline: failedInline } };
  const failed = plan({ capability_id: id, definition_version: 1, instance_id: 7, outcome: "failed", steps: [failedStep] });
  assert.equal(state.planPresentation(failed).status, "failed");
  assert.equal(state.assistantPlanStepResult(failed, failedStep).kind, "root_cause");
});

test("typed report effects expose only a validated artifact locator and honest no-evidence result", () => {
  const id = "report.document.generate";
  const inline = { schema: "report.document_result/v1", outcome: "success", instance_id: 7,
    report_type: "after_sales", format: "markdown", window_hours: 168, include_ai: true,
    evidence_count: 12, artifact_id: 41, artifact_uid: "c".repeat(32), sha256: "b".repeat(64),
    analysis_source: "ai", model: "fixture-model", generated_at: "2026-10-23T12:00:00Z",
    download_url: "/api/v1/reports/catalog/41/download" };
  const step = { seq: 1, title: "Generate after-sales report", capability_id: id, definition_version: 3,
    status: "succeeded", params: { report_type: "after_sales", window_hours: 168, format: "markdown", include_ai: true },
    result_ref: { kind: "capability_effect", capability_id: id, schema: inline.schema, inline } };
  const saved = plan({ capability_id: id, definition_version: 3, instance_id: 7, steps: [step] });
  const typed = state.reportDocumentPlanResult(saved, step);
  assert.equal(typed.artifact_id, 41);
  assert.equal(typed.download_url, "/api/v1/reports/catalog/41/download");
  assert.equal(state.assistantPlanStepResult(saved, step).kind, "report_document");
  const view = state.planPresentation(saved);
  assert.equal(view.status, "succeeded");
  assert.equal(view.steps[0].label, "报告生成完成");
  assert.equal(view.resultStatus, "available");
  assert.match(view.summary, /可查看结果并下载报告/);

  for (const mutate of [
    (value) => { value.instance_id = 8; },
    (value) => { value.sha256 = "invalid"; },
    (value) => { value.download_url = "/api/v1/reports/catalog/99/download"; },
    (value) => { value.schema = "object"; },
    (value, target) => { target.params.report_type = "unified"; },
  ]) {
    const malformed = structuredClone(saved);
    mutate(malformed.steps[0].result_ref.inline, malformed.steps[0]);
    assert.equal(state.reportDocumentPlanResult(malformed, malformed.steps[0]), null);
    assert.equal(state.planPresentation(malformed).status, "unknown");
  }

  const partialInline = { schema: "report.document_result/v1", outcome: "partial", instance_id: 7,
    report_type: "after_sales", format: "markdown", window_hours: 168, include_ai: true,
    evidence_count: 0, reason_code: "no_evidence", artifact_id: null, artifact_uid: null,
    sha256: null, download_url: null };
  const partialStep = { ...step, status: "partial",
    result_ref: { kind: "capability_effect", capability_id: id, schema: partialInline.schema, inline: partialInline } };
  const partial = plan({ capability_id: id, definition_version: 3, instance_id: 7,
    outcome: "partial", steps: [partialStep] });
  assert.equal(state.reportDocumentPlanResult(partial, partialStep).artifact_id, null);
  assert.equal(state.assistantPlanStepResult(partial, partialStep).kind, "report_document");
  assert.match(state.planPresentation(partial).summary, /没有可用证据/);
});

function commonEffectPlan(capabilityId, { outcome = "success", data = {}, instanceId = 7, revision = 2, extraInline = {}, extraRef = {} } = {}) {
  const status = { success: "succeeded", partial: "partial", failed: "failed", unknown: "unknown" }[outcome];
  const step = { seq: 1, title: capabilityId, capability_id: capabilityId, definition_version: 3, status,
    result_ref: { kind: "capability_effect", capability_id: capabilityId, schema: "capability.effect_result/v1",
      inline: { schema: "capability.effect_result/v1", capability_id: capabilityId, definition_version: 3,
        instance_id: instanceId, execution_id: `${planId}:1:${revision}`, outcome,
        data: { outcome, ...data }, ...extraInline }, ...extraRef } };
  return plan({ capability_id: capabilityId, definition_version: 3, revision, instance_id: instanceId,
    outcome: status === "succeeded" ? "succeeded" : status, steps: [step] });
}

test("common v3 effect results require exact frozen identity and safely project real execution evidence", () => {
  const saved = commonEffectPlan("monitor.capacity.execute", { data: { count: 3, snapshot_id: 81 } });
  const result = state.assistantPlanStepResult(saved, saved.steps[0]);
  assert.equal(result.kind, "effect");
  assert.equal(result.value.execution_id, `${planId}:1:2`);
  assert.equal(result.value.plan_only, false);
  assert.ok(result.rows.some((row) => row.field === "count" && row.value === 3));
  const view = state.planPresentation(saved);
  assert.equal(view.status, "succeeded");
  assert.equal(view.steps[0].label, "执行完成");
  assert.match(view.summary, /保存的执行证据/);

  const global = commonEffectPlan("integration.prometheus.sync", { instanceId: null, data: { count: 4 } });
  assert.equal(state.assistantPlanStepResult(global, global.steps[0]).kind, "effect");

  const failed = commonEffectPlan("governance.candidate.scan", { outcome: "failed", data: { count: 0, error_code: "scan_failed" } });
  assert.equal(state.planPresentation(failed).status, "failed");
  assert.equal(state.assistantPlanStepResult(failed, failed.steps[0]).kind, "effect");
  assert.match(state.assistantPlanStepResult(failed, failed.steps[0]).message, /失败证据/);
});

test("common effect parser rejects legacy, unregistered, partial and conflicting envelopes", () => {
  const valid = commonEffectPlan("performance.workload.collect", { data: { count: 1 } });
  const mutations = [
    (value) => { value.steps[0].definition_version = 2; value.steps[0].result_ref.inline.definition_version = 2; },
    (value) => { value.steps[0].result_ref.inline.capability_id = "performance.index.collect"; },
    (value) => { value.steps[0].result_ref.inline.instance_id = 8; },
    (value) => { value.steps[0].result_ref.inline.execution_id = `${planId}:1:3`; },
    (value) => { value.steps[0].result_ref.inline.data.outcome = "partial"; },
    (value) => { value.steps[0].result_ref.inline.future = true; },
    (value) => { value.steps[0].result_ref.future = true; },
  ];
  for (const mutate of mutations) {
    const malformed = structuredClone(valid);
    mutate(malformed);
    assert.notEqual(state.assistantPlanStepResult(malformed, malformed.steps[0]).kind, "effect");
    assert.equal(state.planPresentation(malformed).status, "unknown");
  }
  const unregistered = commonEffectPlan("future.danger.execute", { data: { count: 1 } });
  assert.notEqual(state.assistantPlanStepResult(unregistered, unregistered.steps[0]).kind, "effect");
  assert.equal(state.planPresentation(unregistered).status, "succeeded");
});

test("plan-only effects prove only a generated review artifact and never claim a database change", () => {
  const startup = commonEffectPlan("operations.startup.plan", { data: {
    status: "plan_only", plan_only: true, execution_blocked: true, script: "STARTUP;", execute: false,
  } });
  const result = state.assistantPlanStepResult(startup, startup.steps[0]);
  assert.equal(result.kind, "effect");
  assert.equal(result.value.plan_only, true);
  assert.match(result.message, /尚未执行变更/);
  const view = state.planPresentation(startup);
  assert.equal(view.label, "方案已生成");
  assert.equal(view.steps[0].label, "方案已生成");
  assert.match(view.summary, /尚未执行任何变更/);

  const switchover = commonEffectPlan("operations.switchover.plan", { data: {
    status: "plan_only", plan_only: true, execution_blocked: true, script: "review SQL", sql: "select * from v$database",
    warnings: ["review standby"],
  } });
  assert.equal(state.assistantPlanStepResult(switchover, switchover.steps[0]).kind, "effect");
  for (const invalid of [
    commonEffectPlan("operations.startup.plan", { data: { status: "plan_only", plan_only: true, script: " ", execute: false } }),
    commonEffectPlan("operations.startup.plan", { data: { status: "plan_only", plan_only: true, script: "STARTUP;", executed: true } }),
    commonEffectPlan("operations.switchover.plan", { data: { status: "plan_only", plan_only: true, execution_blocked: true,
      script: "review", sql: "select 1", warnings: [] } }),
  ]) {
    assert.notEqual(state.assistantPlanStepResult(invalid, invalid.steps[0]).kind, "effect");
    assert.equal(state.planPresentation(invalid).status, "unknown");
  }
});

test("partial results expose only persisted successful steps, not failed or unknown sibling blobs", () => {
  const saved = plan({ outcome: "partial", steps: [
    { seq: 1, capability_id: capability, status: "succeeded", result_ref: resultRef() },
    { seq: 2, capability_id: capability, status: "failed", result_ref: resultRef() },
    { seq: 3, capability_id: capability, status: "unknown", result_ref: resultRef() },
  ] });
  assert.deepEqual([...new Set(stepRows(saved).map((row) => row.seq))], [1]);
  assert.deepEqual(stepRows(saved, 1), []);
  assert.deepEqual(stepRows(saved, 2), []);
  assert.equal(state.assistantPlanStepResult(saved, saved.steps[1]).status, "not_applicable");
  assert.equal(state.assistantPlanStepResult(saved, saved.steps[2]).status, "not_applicable");
  assert.equal(state.planPresentation(saved).status, "partial");
});

test("flattening is bounded, scalar-only, credential-key filtered and non-mutating", () => {
  const saved = plan();
  const inline = saved.steps[0].result_ref.inline;
  inline.items = Array.from({ length: 1000 }, (_, index) => ({ id: index, message: "x".repeat(2000),
    password: "hidden", auth_token: "hidden", connection_string: "hidden", api_key: "hidden",
    accessKey: "hidden", cookie: "hidden", pwd: "hidden", nested: { deep: { secret: "hidden" } } }));
  inline.extra = { authorization: "hidden", private_key: "hidden" };
  const before = structuredClone(saved);
  const rows = stepRows(saved);
  assert.ok(rows.length > 0 && rows.length <= 100);
  assert.ok(rows.every((row) => row.value === null || ["string", "number", "boolean"].includes(typeof row.value)));
  assert.ok(rows.every((row) => typeof row.value !== "string" || row.value.length <= 500));
  assert.equal(JSON.stringify(rows).includes("hidden"), false);
  assert.deepEqual(saved, before);
  inline.cycle = inline;
  assert.doesNotThrow(() => stepRows(saved));
  assert.doesNotThrow(() => state.planPresentation(plan({ steps: [null, 1, {}, ...Array(40).fill({ status: "future" })] })));
  assert.equal(state.planPresentation(plan({ steps: Array(40).fill({ status: "future" }) })).steps.length, 20);
});
