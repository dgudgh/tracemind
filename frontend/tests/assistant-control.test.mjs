import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createHash } from "node:crypto";
import { parse } from "@babel/parser";
import { createServer } from "vite";

// Risk matrix: exact DTO/CAS/key correlation; explicit consent; accepted != stopped;
// unknown never replays; normal GET never resolves original intent; close/target/auth
// epochs reject late publication; reload stores only typed locators, never secrets.
const pid = (id = 1) => `pl${id.toString(16).padStart(26, "0")}`;
const cid = (id = 1) => `CC${id.toString(16).padStart(28, "0")}`;
const bid = (id = 1) => `CB${id.toString(16).padStart(28, "0")}`;
const digest = (id = 1) => id.toString(16).padStart(64, "0");
const epochId = (id = 1) => id.toString(16).padStart(32, "0");
const key = `control:${"a".repeat(32)}`;
const command = (patch = {}) => ({ command_id: cid(), action: "pause", status: "accepted", ack_stage: null, control_version: 1, ...patch });
const view = (patch = {}) => ({ schema: "capability.control/v1", execution_id: 1, plan_id: pid(), control_enabled: false,
  control_version: 0, desired_state: "run", observed_state: null, queue_state: "queued", checkpoint_hash: null,
  fencing_epoch: 0, scope: "local_step_boundary", external_stop: "unsupported", external_fence: "unsupported", reused: false, command: null, ...patch });
const accepted = (patch = {}) => view({ control_enabled: true, control_version: 1, desired_state: "pause", observed_state: "running", queue_state: "running", fencing_epoch: 2, command: command(), ...patch });
const paused = () => accepted({ observed_state: "paused", queue_state: "paused", checkpoint_hash: digest(), command: command({ status: "applied", ack_stage: "quiesced" }) });
const member = (id = 1, patch = {}) => ({ plan_id: pid(id), seq: id, queue_state: "queued", reason: null, status: "validated", outcome: null, outcome_kind: null,
  bound_revision: 1, binding_hash: digest(id), binding_current: true, result_ref: null, attempt: 0, started_at: null, finished_at: null,
  execution_id: id, control_enabled: false, control_version: 0, desired_state: "run", observed_state: null, checkpoint_hash: null, fencing_epoch: 0, ...patch });
const batch = (patch = {}) => ({ schema: "capability.batch_read/v2", batch_id: bid(), requested_by: 7, status: "queued", outcome: null,
  accepted: true, reused: false, total: 1, settled_plans: 0, queue_counts: { queued: 1 }, outcomes: {}, requires_attention: false,
  cancel_requested: false, cancel_requested_at: null, created_at: "2026-10-20T09:00:00Z", deadline_at: "2026-10-20T10:00:00Z", items: [member()], ...patch });
const expected = (patch = {}) => ({ plan_id: pid(), action: "pause", expected_control_version: 0, command_id: null, ...patch });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let vite, transport, shared;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  transport = await vite.ssrLoadModule("/src/modules/assistantControlApi.js"); shared = await vite.ssrLoadModule("/src/api.js");
});
test.after(async () => { await vite?.close(); });

test("control routes use shared API, exact two-field POST, independent explicit key and original-key GET only", async (t) => {
  const calls = []; t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]); return accepted(); });
  await transport.getAssistantControl(1);
  await transport.getAssistantControl(1, { idempotencyKey: key, expected: expected() });
  await transport.requestAssistantControl(1, "pause", { expected_control_version: 0, reason: "manual local pause" }, { idempotencyKey: key, planId: pid() });
  assert.deepEqual(calls.map(([path]) => path), ["/assistant/executions/1/control", "/assistant/executions/1/control", "/assistant/executions/1/pause"]);
  assert.equal(calls[0][1].headers, undefined); assert.equal(calls[1][1].headers["Idempotency-Key"], key); assert.equal(calls[1][1].method, undefined);
  assert.deepEqual(JSON.parse(calls[2][1].body), { expected_control_version: 0, reason: "manual local pause" });
  assert.equal(calls[2][1].method, "POST"); assert.equal(calls[2][1].headers["Idempotency-Key"], key);
});

test("invalid IDs/actions/keys/CAS/reason and execution payloads fail before HTTP", (t) => {
  let calls = 0; t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const id of [null, undefined, 0, -1, 1.2, "1", "1/stop", Number.MAX_SAFE_INTEGER + 1, {}, []]) {
    assert.throws(() => transport.getAssistantControl(id), TypeError);
    assert.throws(() => transport.requestAssistantControl(id, "pause", { expected_control_version: 0, reason: "reason" }, { idempotencyKey: key, planId: pid() }), TypeError);
  }
  for (const value of ["", "key\n", "../x", "https://invalid", "x".repeat(129), 1]) assert.throws(() => transport.getAssistantControl(1, { idempotencyKey: value }), TypeError);
  for (const action of ["execute", "reconcile", "kill", "pause/../stop", null]) assert.throws(() => transport.requestAssistantControl(1, action, {}, { idempotencyKey: key, planId: pid() }), TypeError);
  for (const payload of [{}, { expected_control_version: -1, reason: "r" }, { expected_control_version: "0", reason: "r" },
    { expected_control_version: Number.MAX_SAFE_INTEGER, reason: "r" }, { expected_control_version: 0, reason: " " },
    { expected_control_version: 0, reason: "x".repeat(301) }, { expected_control_version: 0, reason: "r", actor_id: 7 },
    { expected_control_version: 0, reason: "r", sql: "select 1" }, { expected_control_version: 0, reason: "r", url: "https://invalid" }]) {
    assert.throws(() => transport.requestAssistantControl(1, "pause", payload, { idempotencyKey: key, planId: pid() }), TypeError);
  }
  assert.throws(() => transport.getAssistantControl(1, { expected: expected() }), TypeError); assert.equal(calls, 0);
});

test("strict control DTO rejects ACK-only, extra fields, unsupported claims and invalid enums/budgets", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  for (response of [null, {}, { accepted: true }, "<html>private</html>", view({ schema: "capability.control/v2" }), view({ execution_id: 2 }), view({ plan_id: `${pid()}\n` }),
    view({ control_enabled: "false" }), view({ control_version: -1 }), view({ control_version: Number.MAX_SAFE_INTEGER + 1 }), view({ desired_state: "kill" }),
    view({ observed_state: "success" }), view({ queue_state: "succeeded" }), view({ checkpoint_hash: "A".repeat(64) }), view({ fencing_epoch: -1 }), view({ reused: 1 }),
    view({ scope: "global" }), view({ external_stop: "supported" }), view({ external_fence: true }), { ...view(), result_ref: {} },
    accepted({ command: command({ command_id: `${cid()}\n` }) }), accepted({ command: command({ action: "kill" }) }), accepted({ command: command({ control_version: 2 }) }),
    accepted({ command: command({ status: "applied", ack_stage: "received" }) }), accepted({ command: command({ status: "future" }) }),
    accepted({ command: command({ ack_stage: "running" }) }), accepted({ command: { ...command(), reason: "private" } }),
    view({ queue_state: "paused" }), view({ queue_state: "stopped", control_enabled: true })]) {
    await assert.rejects(transport.getAssistantControl(1), { code: "assistant_control_response_invalid" });
  }
});

test("original key matches exact action/plan/command version while current projection and reused may differ", async (t) => {
  let response = accepted({ control_version: 5, command: command({ status: "superseded" }), reused: false });
  t.mock.method(shared.api, "request", async () => response);
  assert.equal((await transport.getAssistantControl(1, { idempotencyKey: key, expected: expected() })).control_version, 5);
  for (response of [view(), accepted({ plan_id: pid(2) }), accepted({ command: command({ action: "stop" }) }),
    accepted({ control_version: 2, command: command({ control_version: 2 }) }), accepted({ command: command({ command_id: cid(2) }) })]) {
    await assert.rejects(transport.getAssistantControl(1, { idempotencyKey: key, expected: expected({ command_id: cid() }) }), { code: "assistant_control_response_invalid" });
  }
  response = view(); await assert.rejects(transport.getAssistantControl(1, { idempotencyKey: key }), { code: "assistant_control_response_invalid" });
});

test("v2 batch lookup is independently explicit and validates full members/counters without changing v1", async (t) => {
  const calls = []; let response = batch(); t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]); return response; });
  assert.equal(await transport.getAssistantControlBatch(bid()), response);
  assert.equal(calls[0][0], `/assistant/batches/${bid()}`); assert.deepEqual(calls[0][1].headers, { "X-Assistant-Batch-Version": "2" });
  assert.equal(calls[0][1].method, undefined);
  const malformed = [batch({ schema: "capability.batch_read/v1" }), batch({ batch_id: bid(2) }), { ...batch(), schema_version: 2 },
    batch({ items: [] }), batch({ total: 101 }), batch({ requested_by: "7" }), batch({ accepted: false }), batch({ settled_plans: 1 }),
    batch({ queue_counts: { queued: 2 } }), batch({ outcomes: { succeeded: 1 } }), batch({ requires_attention: true }), batch({ outcome: "succeeded" }),
    batch({ deadline_at: "2026-02-30T10:00:00Z" }), batch({ created_at: "2026-10-21T10:00:00Z" }),
    batch({ items: [member(1, { execution_id: "1" })] }), batch({ items: [member(1, { execution_id: 0 })] }), batch({ items: [member(1, { plan_id: `${pid()}\n` })] }),
    batch({ items: [member(1, { control_version: -1 })] }), batch({ items: [member(1, { desired_state: "kill" })] }), batch({ items: [member(1, { control_enabled: 1 })] }),
    batch({ items: [member(1, { observed_state: "success" })] }), batch({ items: [member(1, { checkpoint_hash: "wrong" })] }), batch({ items: [member(1, { fencing_epoch: -1 })] }),
    batch({ items: [member(1, { binding_current: false })] }), batch({ items: [member(1, { result_ref: { url: "https://invalid" } })] }),
    batch({ items: [member(1, { seq: 2 })] }), batch({ items: [member(1, { outcome: "succeeded" })] }),
    batch({ items: [member(1, { instance_id: 1 })] }),
    batch({ items: [member(1, { instance_id: 1, db_type: "oracle" })] }),
    batch({ items: [member(1, { instance_id: 0, db_type: "oracle", capability_id: "inspection.run.query" })] }),
    batch({ items: [member(1, { instance_id: 1, db_type: "sqlite", capability_id: "inspection.run.query" })] }),
    batch({ items: [member(1, { instance_id: 1, db_type: "oracle", capability_id: "invalid" })] }),
    batch({ items: [member(1, { instance_id: 1, db_type: "oracle", capability_id: `${"a".repeat(65)}.b.c` })] }),
    batch({ total: 2, queue_counts: { queued: 2 }, items: [member(), member(2, { execution_id: 1 })] }),
    batch({ total: 2, queue_counts: { queued: 2 }, items: [member(), member(2, { plan_id: pid() })] })];
  for (response of malformed) await assert.rejects(transport.getAssistantControlBatch(bid()), { code: "assistant_control_response_invalid" });
  response = batch({ items: [member(1, { instance_id: 9, db_type: "oracle", capability_id: "inspection.run.query" })] });
  assert.deepEqual((await transport.getAssistantControlBatch(bid())).items[0], response.items[0]);
  response = batch({ items: [member(1, { instance_id: null, db_type: "host", capability_id: "operations.host.collect" })] });
  assert.deepEqual((await transport.getAssistantControlBatch(bid())).items[0], response.items[0]);
  for (const id of ["", `${bid()}\n`, `${bid()}/cancel`, `${bid()}?schema_version=2`, `CB${"A".repeat(28)}`]) assert.throws(() => transport.getAssistantControlBatch(id), TypeError);
});

test("v2 paused/stopped/probe and terminal result schema match actual server writer/evidence source", async (t) => {
  const writer = readFileSync(new URL("../../backend/app/services/plan_service.py", import.meta.url), "utf8");
  const evidence = readFileSync(new URL("../../backend/app/services/plan_queue.py", import.meta.url), "utf8");
  const oldApi = readFileSync(new URL("../src/modules/assistantBatchApi.js", import.meta.url), "utf8");
  for (const source of [writer, evidence, oldApi]) assert.match(source, /["']capability\.plan_results\/v1["']/);
  for (const source of [writer, evidence]) assert.match(source, /["']kind["']:\s*["']capability_plan_steps["']/);
  let response; t.mock.method(shared.api, "request", async () => response);
  for (const state of ["paused", "stopped"]) {
    response = batch({ status: state, queue_counts: { [state]: 1 }, items: [member(1, { queue_state: state, status: "running", control_enabled: true,
      control_version: 2, desired_state: state === "paused" ? "pause" : "stop", observed_state: state, checkpoint_hash: digest(), fencing_epoch: 2 })] });
    assert.equal((await transport.getAssistantControlBatch(bid())).status, state);
  }
  const ref = { kind: "capability_plan_steps", plan_id: pid(), schema: "capability.plan_results/v1", steps: [1, 2] };
  response = batch({ status: "settled", outcome: "succeeded", queue_counts: { archived: 1 }, settled_plans: 1, outcomes: { succeeded: 1 },
    items: [member(1, { queue_state: "archived", status: "settled", outcome: "succeeded", result_ref: ref, control_version: 1 })] });
  assert.deepEqual((await transport.getAssistantControlBatch(bid())).items[0].result_ref, ref);
  response.items[0].result_ref = { ...ref, schema: "capability.plan_result/v1" };
  await assert.rejects(transport.getAssistantControlBatch(bid()), { code: "assistant_control_response_invalid" });
});

const batchSummary = (value = batch()) => { const { items, ...summary } = value; return summary; };
const controlHistory = (patch = {}) => ({ schema: "capability.batch_list/v2", total: 1, page: 1, page_size: 10, items: [batchSummary()], ...patch });

test("v2 history is one explicit versioned GET with typed pagination and captured authSession", async (t) => {
  const calls = [], authSession = { token: "synthetic-history-token", generation: 3, loginEpoch: epochId(3) };
  t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]);
    return controlHistory({ page: 2, page_size: 2, total: 3, items: [batchSummary(batch({ batch_id: bid(3) }))] });
  });
  const result = await transport.listAssistantControlBatches({ page: 2, page_size: 2 }, { authSession });
  assert.equal(calls.length, 1); assert.equal(calls[0][0], "/assistant/batches?page=2&page_size=2");
  assert.deepEqual(calls[0][1].headers, { "X-Assistant-Batch-Version": "2" }); assert.equal(calls[0][1].authSession, authSession);
  assert.equal(calls[0][1].method, undefined); assert.equal(calls[0][1].body, undefined);
  assert.equal(result.items[0].schema, "capability.batch_read/v2"); assert.equal(Object.hasOwn(result.items[0], "items"), false);
});

test("v2 history input bounds and unknown options are rejected before any I/O", (t) => {
  let calls = 0; t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const params of [null, [], { page: 0 }, { page: -1 }, { page: 1.5 }, { page: true }, { page: "1" }, { page: 1000001 },
    { page_size: 0 }, { page_size: 101 }, { page_size: 1.5 }, { page_size: "10" }, { page_size: true },
    { schema_version: 2 }, { batch_id: bid() }, { url: "https://invalid" }, { headers: {} }]) {
    assert.throws(() => transport.listAssistantControlBatches(params), TypeError);
  }
  assert.equal(calls, 0);
});

test("v2 history validates pagination echo, exact page size, total and bounded unique IDs", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  for (response of [null, [], {}, { ...controlHistory(), schema: "capability.batch_list/v1" }, { ...controlHistory(), next: "arbitrary" },
    controlHistory({ page: 2 }), controlHistory({ page: "1" }), controlHistory({ page_size: 50 }), controlHistory({ page_size: "10" }),
    controlHistory({ total: -1 }), controlHistory({ total: "1" }), controlHistory({ total: Number.MAX_SAFE_INTEGER + 1 }),
    controlHistory({ total: 0 }), controlHistory({ items: [] }), controlHistory({ total: 2 }), controlHistory({ items: "private" }),
    controlHistory({ total: 2, items: [batchSummary(), batchSummary()] }), controlHistory({ items: [batchSummary(batch({ batch_id: `${bid()}\n` }))] }),
    controlHistory({ items: [batchSummary(batch({ batch_id: `CB${"A".repeat(28)}` }))] }),
    controlHistory({ items: [batchSummary(batch({ batch_id: `${bid()}/control` }))] })]) {
    await assert.rejects(transport.listAssistantControlBatches(), { code: "assistant_control_response_invalid" });
  }
  response = controlHistory({ total: 0, items: [] }); assert.equal((await transport.listAssistantControlBatches()).total, 0);
  response = controlHistory({ page: 1000000, page_size: 1, total: 1, items: [] });
  assert.equal((await transport.listAssistantControlBatches({ page: 1000000, page_size: 1 })).items.length, 0);
  response = controlHistory({ total: 100, page_size: 100, items: Array.from({ length: 100 }, (_, index) => batchSummary(batch({ batch_id: bid(index + 1) }))) });
  assert.equal((await transport.listAssistantControlBatches({ page_size: 100 })).items.length, 100);
  response = controlHistory({ total: 101, page_size: 100, items: [...response.items, batchSummary(batch({ batch_id: bid(101) }))] });
  await assert.rejects(transport.listAssistantControlBatches({ page_size: 100 }), { code: "assistant_control_response_invalid" });
});

test("v2 summary keeps strict schema, counters, outcomes and attention consistency without a nested member array", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  const malformed = [batch(), { ...batchSummary(), execution_id: 1 }, batchSummary(batch({ schema: "capability.batch_read/v1" })),
    batchSummary(batch({ requested_by: "7" })), batchSummary(batch({ total: 0 })), batchSummary(batch({ total: 101 })),
    batchSummary(batch({ accepted: false })), batchSummary(batch({ reused: 1 })), batchSummary(batch({ status: "unknown" })),
    batchSummary(batch({ settled_plans: 1 })), batchSummary(batch({ queue_counts: { queued: 0 } })),
    batchSummary(batch({ queue_counts: { queued: 2 } })), batchSummary(batch({ queue_counts: { future: 1 } })),
    batchSummary(batch({ queue_counts: { running: 1 } })), batchSummary(batch({ queue_counts: {} })),
    batchSummary(batch({ outcomes: { succeeded: 1 } })), batchSummary(batch({ outcomes: { future: 1 }, settled_plans: 1 })),
    batchSummary(batch({ status: "settled", outcome: "succeeded" })), batchSummary(batch({ requires_attention: true })),
    batchSummary(batch({ queue_counts: { blocked: 1 }, status: "reconciliation_required", requires_attention: false })),
    batchSummary(batch({ created_at: "2026-02-30T00:00:00Z" })), batchSummary(batch({ deadline_at: "2026-10-20T08:00:00Z" })),
    batchSummary(batch({ created_at: "2026-10-20T09:00:00+08:00" })), batchSummary(batch({ cancel_requested_at: "bad" }))];
  for (const summary of malformed) {
    response = controlHistory({ items: [summary] });
    await assert.rejects(transport.listAssistantControlBatches(), { code: "assistant_control_response_invalid" });
  }
  for (const status of ["paused", "stopped", "running", "queued", "reconciliation_required"]) {
    const queueState = status === "reconciliation_required" ? "blocked" : status;
    response = controlHistory({ items: [batchSummary(batch({ status, queue_counts: { [queueState]: 1 }, requires_attention: status === "reconciliation_required" }))] });
    assert.equal((await transport.listAssistantControlBatches()).items[0].status, status);
  }
  for (const state of ["cancelled", "expired"]) {
    response = controlHistory({ items: [batchSummary(batch({ status: "settled", outcome: state, queue_counts: { [state]: 1 } }))] });
    assert.equal((await transport.listAssistantControlBatches()).items[0].settled_plans, 0);
  }
  response = controlHistory({ items: [batchSummary(batch({ status: "settled", outcome: "succeeded", queue_counts: { archived: 1 }, settled_plans: 1, outcomes: { succeeded: 1 } }))] });
  assert.equal((await transport.listAssistantControlBatches()).items[0].outcome, "succeeded");
  response = controlHistory({ items: [batchSummary(batch({ status: "reconciliation_required", queue_counts: { reconciliation_required: 1 },
    requires_attention: true, outcomes: { unknown: 1 }, settled_plans: 1 }))] });
  assert.equal((await transport.listAssistantControlBatches()).items[0].requires_attention, true);
});

test("history never changes version, retries or creates a batch after any transport/protocol failure", async (t) => {
  const calls = []; let failure;
  t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]); if (failure) throw failure; return controlHistory({ schema: "capability.batch_list/v1" }); });
  for (failure of [Object.assign(new Error("private"), { status: 401 }), Object.assign(new Error("private"), { status: 403 }),
    Object.assign(new Error("private"), { status: 409 }), Object.assign(new Error("private"), { status: 503 }), new TypeError("network"), null]) {
    await assert.rejects(transport.listAssistantControlBatches());
  }
  assert.equal(calls.length, 6); assert.ok(calls.every(([path, options]) => path === "/assistant/batches?page=1&page_size=10"
    && options.headers["X-Assistant-Batch-Version"] === "2" && options.method === undefined && options.body === undefined));
});

test("history summary source contract matches the backend's v2 projection with items removed", () => {
  const backend = readFileSync(new URL("../../backend/app/services/plan_queue.py", import.meta.url), "utf8");
  assert.match(backend, /summary = _view\(batch, loaded, schema_version=schema_version\)/);
  assert.match(backend, /summary\.pop\("items"\)/); assert.match(backend, /capability\.batch_list\/v\{schema_version\}/);
});

for (const status of [400, 401, 403, 404, 409, 422, 500, 503, 504]) {
  test(`HTTP ${status} is reported once without retry, v2 fallback, raw message leakage or implicit probe`, async (t) => {
    let calls = 0; t.mock.method(shared.api, "request", async () => { calls += 1; throw Object.assign(new Error("private SQL/token"), {
      status, responseData: { detail: { code: "control_version_conflict" } },
    }); });
    await assert.rejects(transport.getAssistantControl(1), (error) => error.status === status && error.code === "control_version_conflict" && !error.message.includes("private"));
    await assert.rejects(transport.getAssistantControlBatch(bid()), { status }); assert.equal(calls, 2);
  });
}

test("real shared HTTP cannot accept HTML, null or ACK-only as a control response", async (t) => {
  let response; t.mock.method(globalThis, "fetch", async () => response);
  for (response of [new Response("<html>private</html>"), new Response(null, { status: 204 }), new Response(JSON.stringify({ accepted: true }))]) {
    await assert.rejects(transport.getAssistantControl(1), { code: "assistant_control_response_invalid" });
  }
});

test("session fingerprint is SHA-256 only and locator rejects credentials, reason, results and schema extras", async () => {
  const value = await transport.getAssistantControlSessionFingerprint("synthetic-control-token");
  assert.equal(value, createHash("sha256").update("synthetic-control-token").digest("hex"));
});

const source = readFileSync(new URL("../src/AssistantControlPanel.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const component = ast.program.body.find((node) => node.declaration?.id?.name === "AssistantControlPanel").declaration;
function hasJsx(node) {
  return node && typeof node === "object" && (["JSXElement", "JSXFragment"].includes(node.type)
    || Object.values(node).some((value) => (Array.isArray(value) ? value : [value]).some(hasJsx)));
}
const statements = component.body.body.filter((node) => node.type !== "ReturnStatement" && !hasJsx(node));
const names = statements.filter((node) => node.type === "VariableDeclaration").flatMap((node) => node.declarations.flatMap((value) => value.id.type === "ArrayPattern" ? value.id.elements.filter(Boolean).map((item) => item.name) : [value.id.name]));
const helpers = ast.program.body.filter((node) => node.type === "VariableDeclaration" || node.type === "FunctionDeclaration").map((node) => source.slice(node.start, node.end)).join("\n");
const body = `${helpers}\nconst ${source.slice(component.params[0].start, component.params[0].end)} = props;\n${statements.map((node) => source.slice(node.start, node.end)).join("\n")}\nreturn {${names.join(",")},actionAllowed,controlError};`;

// Executes source-extracted production callbacks; this is not browser/DOM evidence.
function harness(overrides = {}, options = {}) {
  const slots = [], effects = [], timers = new Map(), calls = [], changed = [], storage = options.storage || new Map(), failures = options.failures || {};
  let cursor = 0, dirty = true, value, time = 0, timerId = 0, generation = options.generation ?? 1, loginEpoch = options.loginEpoch === undefined ? epochId() : options.loginEpoch;
  let props = { executionId: 1, onChanged: (dto) => changed.push(dto), ...options.props };
  const defaults = {
    getAssistantControl: async (id, input) => input?.idempotencyKey ? accepted({ execution_id: id, plan_id: input.expected?.plan_id || pid(id),
      control_version: (input.expected?.expected_control_version || 0) + 1, command: command({ action: input.expected?.action || "pause",
        control_version: (input.expected?.expected_control_version || 0) + 1, command_id: input.expected?.command_id || cid() }) }) : view({ execution_id: id, plan_id: pid(id) }),
    requestAssistantControl: async (id, action, payload) => accepted({ execution_id: id, plan_id: pid(id), control_version: payload.expected_control_version + 1,
      command: command({ action, control_version: payload.expected_control_version + 1 }) }),
    getAssistantControlSessionFingerprint: async () => digest(777),
  };
  const wrapped = Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name, (...args) => { calls.push({ name, args }); return (overrides[name] || fallback)(...args); }]));
  const dependencies = { ...wrapped, isAssistantControlExecutionId: transport.isAssistantControlExecutionId, isAssistantControlLoginEpoch: transport.isAssistantControlLoginEpoch,
    validateAssistantControlRecovery: transport.validateAssistantControlRecovery,
    Date: options.clock ? class extends Date { static now() { return options.clock.now; } } : Date,
    getAuthSession: options.auth?.getAuthSession || (() => ({ token: "control-test-only", generation, loginEpoch })),
    isAuthSessionCurrent: options.auth?.isAuthSessionCurrent || ((snapshot) => snapshot.token === "control-test-only" && snapshot.generation === generation && snapshot.loginEpoch === loginEpoch),
    globalThis: { crypto: globalThis.crypto, performance: { timeOrigin: options.navigation ?? 1000 }, sessionStorage: {
      getItem: (key) => { if (failures.get) throw new Error("storage denied"); return storage.get(key) ?? null; },
      setItem: (key, text) => { if (failures.set) throw new Error("storage denied"); storage.set(key, text); },
      removeItem: (key) => { if (failures.remove) throw new Error("storage denied"); storage.delete(key); },
    } },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { at: time + delay, callback }); return id; }, clearTimeout: (id) => timers.delete(id),
    useState: (initial) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (update) => { const next = typeof update === "function" ? update(slots[i].value) : update; if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true; } }]; },
    useRef: (initial) => { const i = cursor++; slots[i] ??= { ref: { current: initial } }; return slots[i].ref; },
    useEffect: (callback, deps) => { const i = cursor++, old = slots[i]; if (!old || deps.some((dep, index) => !Object.is(dep, old.deps[index]))) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: callback() }; }); },
  };
  const render = new Function(...Object.keys(dependencies), "props", body);
  const settle = async () => { for (let i = 0; i < 60; i += 1) { if (dirty) { dirty = false; cursor = 0; value = render(...Object.values(dependencies), props); while (effects.length) effects.shift()(); } await Promise.resolve(); } assert.equal(dirty, false); };
  return { get value() { return value; }, calls, changed, storage, settle, count: (name) => calls.filter((item) => item.name === name).length,
    update: async (patch) => { props = { ...props, ...patch }; dirty = true; await settle(); },
    session: async () => { generation += 1; loginEpoch = epochId(generation); dirty = true; await settle(); },
    advance: async (duration) => { const target = time + duration; for (;;) { const next = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0]; if (!next) break; timers.delete(next[0]); time = next[1].at; next[1].callback(); await settle(); } time = target; await settle(); },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}
const recoveryKey = "mesguard.assistant.control-recovery.v1:1";
const stored = (h) => JSON.parse(h.storage.get(recoveryKey));
const lose = () => Promise.reject(Object.assign(new Error("private lost response"), { status: 504 }));
async function consent(h) { await h.settle(); h.value.setReason("isolated manual control"); h.value.setConfirmation(true); await h.settle(); }

test("mount and refresh only GET current projection, never POST probe or look up a key automatically", async () => {
  const h = harness(); await h.settle(); assert.equal(h.count("getAssistantControl"), 1); assert.equal(h.value.control.command, null);
  await h.value.refresh(); await h.settle(); assert.equal(h.count("getAssistantControl"), 2);
  await h.value.issue("probe"); assert.equal(h.count("requestAssistantControl"), 0);
  assert.ok(h.calls.filter((call) => call.name === "getAssistantControl").every((call) => call.args[1] === undefined)); h.unmount();
});

test("explicit reason plus confirmation sends one command, consumes consent before render, never synthesizes paused", async () => {
  const h = harness(); await h.settle(); await h.value.issue("pause"); assert.equal(h.count("requestAssistantControl"), 0);
  await consent(h); const staleIssue = h.value.issue; const first = staleIssue("pause"); await staleIssue("pause"); await first; await staleIssue("pause");
  await h.settle(); assert.equal(h.count("requestAssistantControl"), 1); assert.equal(h.value.control.queue_state, "running");
  assert.equal(h.value.control.command.status, "accepted"); assert.equal(h.value.confirmed, false); assert.equal(h.changed.length, 1);
  assert.equal(stored(h).uncertain, false); assert.doesNotMatch(JSON.stringify(stored(h)), /control-test-only|reason|isolated manual|result|token|SQL/); h.unmount();
});

test("each newly confirmed action uses a new key and current server CAS, including durable probe", async () => {
  const h = harness(); await consent(h); await h.value.issue("pause"); await h.settle();
  await consent(h); await h.value.issue("probe"); await h.settle();
  const writes = h.calls.filter((call) => call.name === "requestAssistantControl");
  assert.equal(writes.length, 2); assert.notEqual(writes[0].args[3].idempotencyKey, writes[1].args[3].idempotencyKey);
  assert.equal(writes[0].args[2].expected_control_version, 0); assert.equal(writes[1].args[2].expected_control_version, 1); h.unmount();
});

test("action boundaries never promise remote control, restart unknown execution or resume irreversible stop", async () => {
  const h = harness(); await h.settle(); const allowed = h.value.actionAllowed;
  assert.equal(allowed(view(), "pause"), true); assert.equal(allowed(paused(), "resume"), true);
  assert.equal(allowed(view(), "resume"), false); assert.equal(allowed(accepted(), "pause"), false);
  for (const value of [accepted({ observed_state: "unknown" }), accepted({ queue_state: "reconciliation_required" }), accepted({ desired_state: "stop" }), view({ queue_state: "archived" })]) {
    for (const action of ["pause", "resume"]) assert.equal(allowed(value, action), false);
    assert.equal(allowed(value, "probe"), true);
  }
  assert.equal(allowed(accepted({ observed_state: "unknown" }), "stop"), false); h.unmount();
});

for (const mode of ["network", "5xx", "protocol", "timeout"]) {
  test(`POST ${mode} locks unknown with same original key; ordinary GET and late success never unlock`, async () => {
    const late = deferred(), h = harness({ requestAssistantControl: () => late.promise }); await consent(h);
    const stale = h.value.issue, pending = stale("pause"); await h.settle(); const original = stored(h);
    if (mode === "timeout") await h.advance(90000);
    else late.reject(mode === "5xx" ? Object.assign(new Error("private"), { status: 503 })
      : mode === "protocol" ? Object.assign(new Error("private"), { code: "assistant_control_response_invalid" }) : new TypeError("private"));
    await pending; await stale("pause"); await h.settle(); assert.ok(h.value.unknown);
    await h.value.refresh(); await h.settle(); assert.ok(h.value.unknown); assert.equal(stored(h).idempotency_key, original.idempotency_key);
    await consent(h); await h.value.issue("probe"); assert.equal(h.count("requestAssistantControl"), 1); assert.doesNotMatch(h.value.error, /private/);
    late.resolve(paused()); await h.settle(); assert.ok(h.value.unknown); h.unmount();
  });
}

test("explicit original-key GET recovers the same command even if superseded and projection version advanced", async () => {
  const h = harness({ requestAssistantControl: lose, getAssistantControl: async (id, options) => options ? accepted({ control_version: 5, command: command({ status: "superseded" }) }) : view() });
  await consent(h); await h.value.issue("pause"); await h.settle(); const before = stored(h);
  assert.equal(h.count("getAssistantControl"), 1); await h.value.recover(); await h.settle();
  const read = h.calls.filter((call) => call.name === "getAssistantControl").at(-1).args[1];
  assert.equal(read.idempotencyKey, before.idempotency_key); assert.deepEqual(read.expected, expected()); assert.equal(h.value.unknown, null);
  assert.equal(h.value.control.control_version, 5); assert.equal(h.count("requestAssistantControl"), 1); assert.equal(h.value.confirmed, false); h.unmount();
});

for (const status of [401, 403, 404, 409, 503]) {
  test(`receipt GET ${status} keeps unknown/original journal and does not retry`, async () => {
    const h = harness({ requestAssistantControl: lose, getAssistantControl: async (id, options) => { if (options) throw Object.assign(new Error("private body"), { status }); return view(); } });
    await consent(h); await h.value.issue("pause"); await h.settle(); const before = h.storage.get(recoveryKey);
    await h.value.recover(); await h.settle(); assert.ok(h.value.unknown); assert.equal(h.storage.get(recoveryKey), before);
    assert.equal(h.count("requestAssistantControl"), 1); assert.equal(h.count("getAssistantControl"), 2); assert.doesNotMatch(h.value.error, /private body/); h.unmount();
  });
}

for (const status of [400, 401, 403, 404, 409, 422]) {
  test(`definite POST ${status} demands a new explicit GET and consent, with no automatic retry`, async () => {
    const h = harness({ requestAssistantControl: async () => { throw Object.assign(new Error("private"), { status, code: "control_version_conflict" }); } });
    await consent(h); await h.value.issue("pause"); await h.settle(); assert.equal(h.value.fresh, false); assert.equal(h.value.unknown, null);
    await consent(h); await h.value.issue("pause"); assert.equal(h.count("requestAssistantControl"), 1);
    assert.equal(h.count("getAssistantControl"), 1); assert.doesNotMatch(h.value.error, /private/); h.unmount();
  });
}

for (const change of ["target", "session", "unmount"]) {
  for (const kind of ["post", "receipt", "read"]) {
    test(`${kind} late after ${change} cannot overwrite view, journal or invoke parent callback`, async () => {
      const late = deferred(); let delay = false;
      const h = harness({ requestAssistantControl: kind === "post" ? () => late.promise : lose,
        getAssistantControl: (id, options) => {
          if (delay && (kind === "read" || options)) { delay = false; return late.promise; }
          return Promise.resolve(view({ execution_id: id, plan_id: pid(id) }));
        } });
      await consent(h); let pending;
      if (kind === "read") { delay = true; pending = h.value.refresh(); }
      else { pending = h.value.issue("pause"); if (kind === "receipt") { await pending; await h.settle(); delay = true; pending = h.value.recover(); } }
      await h.settle();
      if (change === "target") await h.update({ executionId: 2 });
      if (change === "session") await h.session();
      if (change === "unmount") h.unmount();
      const before = h.storage.get(recoveryKey);
      late.resolve(accepted()); await pending; await h.settle(); assert.equal(h.storage.get(recoveryKey), before); assert.equal(h.changed.length, 0);
      if (change === "target") assert.equal(h.value.control.execution_id, 2);
      assert.notEqual(h.value.control?.command?.command_id, cid()); h.unmount();
    });
  }
}

test("normal reload restores only the pending locator, and explicit key GET is required to unlock", async () => {
  const first = harness({ requestAssistantControl: lose }); await consent(first); await first.value.issue("pause"); await first.settle(); const saved = stored(first), storage = first.storage; first.unmount();
  const h = harness({}, { storage, generation: 0, navigation: 2000 }); await h.settle(); assert.ok(h.value.unknown);
  assert.equal(h.count("requestAssistantControl"), 0); assert.equal(h.count("getAssistantControl"), 1);
  assert.equal(h.calls.find((call) => call.name === "getAssistantControl").args[1], undefined);
  await h.value.recover(); await h.settle(); assert.equal(h.value.unknown, null); assert.equal(stored(h).idempotency_key, saved.idempotency_key);
  assert.equal(h.count("requestAssistantControl"), 0); h.unmount();
});

for (const change of ["epoch", "fingerprint", "generation"]) {
  test(`changed ${change} rejects old locator without display or an automatic original-key GET`, async () => {
    const first = harness({ requestAssistantControl: lose }); await consent(first); await first.value.issue("pause"); await first.settle(); const storage = first.storage; first.unmount();
    const h = harness({ getAssistantControlSessionFingerprint: async () => change === "fingerprint" ? digest(888) : digest(777) },
      { storage, generation: change === "generation" ? 2 : 0, navigation: change === "generation" ? 1000 : 2000, loginEpoch: change === "epoch" ? epochId(2) : epochId() });
    await h.settle(); assert.equal(h.value.journal, null); assert.equal(storage.has(recoveryKey), false);
    assert.equal(h.count("getAssistantControl"), 1); assert.equal(h.count("requestAssistantControl"), 0); h.unmount();
  });
}

test("actual authStorage normal reload recovers but same-token re-login plus reload rejects the previous control locator", async () => {
  const authSource = readFileSync(new URL("../src/modules/authStorage.js", import.meta.url), "utf8");
  const code = parse(authSource, { sourceType: "module" }).program.body.map((node) => { const item = node.declaration || node; return authSource.slice(item.start, item.end); }).join("\n");
  const data = new Map(), boot = () => vm.runInNewContext(`${code}\n({getAuthSession,isAuthSessionCurrent,saveAuthToken,clearAuthToken})`, {
    crypto: globalThis.crypto, localStorage: { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: (key) => data.delete(key) },
  });
  const auth = boot(); auth.saveAuthToken("control-test-only"); assert.match(auth.getAuthSession().loginEpoch, /^[a-f0-9]{32}$/);
  const first = harness({ requestAssistantControl: lose }, { auth }); await consent(first); await first.value.issue("pause"); await first.settle(); const storage = first.storage; first.unmount();
  const normal = harness({}, { auth: boot(), storage, navigation: 2000 }); await normal.settle(); assert.ok(normal.value.unknown); assert.ok(normal.value.journal); normal.unmount();
  const previous = auth.getAuthSession(); auth.clearAuthToken(); auth.saveAuthToken("control-test-only"); const current = boot();
  assert.notEqual(current.getAuthSession().loginEpoch, previous.loginEpoch); assert.equal(current.getAuthSession().generation, 0);
  const next = harness({}, { auth: current, storage, navigation: 3000 }); await next.settle();
  assert.equal(next.value.journal, null); assert.equal(next.value.unknown, null); assert.equal(next.count("requestAssistantControl"), 0);
  assert.ok(next.calls.filter((call) => call.name === "getAssistantControl").every((call) => call.args[1] === undefined)); next.unmount();
});

for (const mode of ["corrupt", "expired", "get", "set", "no-epoch"]) {
  test(`${mode} recovery context fails closed; manual original-key read cannot unlock writes`, async () => {
    const first = harness({ requestAssistantControl: lose }); await consent(first); await first.value.issue("pause"); await first.settle(); const storage = first.storage, locator = stored(first); first.unmount();
    if (mode === "corrupt") storage.set(recoveryKey, "{broken");
    if (mode === "expired") storage.set(recoveryKey, JSON.stringify({ ...locator, created_at: Date.now() - 10000, expires_at: Date.now() - 1000 }));
    const h = harness({}, { storage, failures: { [mode]: true }, generation: 0, navigation: 2000, loginEpoch: mode === "no-epoch" ? null : epochId() });
    await h.settle(); assert.ok(h.value.unknown); assert.equal(h.value.gate, true);
    h.value.setManualKey(locator.idempotency_key); await h.settle(); await h.value.recover(); await h.settle();
    assert.ok(h.value.manual); assert.ok(h.value.unknown); assert.equal(h.value.gate, true);
    await consent(h); await h.value.issue("probe"); assert.equal(h.count("requestAssistantControl"), 0); h.unmount();
  });
}

test("storage failure before POST retains one original in-memory key without sending or silently generating another", async () => {
  const failures = {}, h = harness({}, { failures }); await consent(h); failures.set = true;
  await h.value.issue("pause"); await h.settle(); const original = h.value.journal.idempotency_key;
  assert.ok(h.value.unknown); assert.equal(h.count("requestAssistantControl"), 0);
  await consent(h); await h.value.issue("pause"); assert.equal(h.value.journal.idempotency_key, original); assert.equal(h.count("requestAssistantControl"), 0); h.unmount();
});

test("storage failure after accepted POST keeps durable pending locator and can recover with GET after reload", async () => {
  const failures = {}, first = harness({ requestAssistantControl: async () => { failures.set = true; return accepted(); } }, { failures });
  await consent(first); await first.value.issue("pause"); await first.settle(); assert.ok(first.value.unknown);
  assert.equal(stored(first).uncertain, true); assert.equal(first.changed.length, 0);
  const storage = first.storage, original = stored(first).idempotency_key; first.unmount();
  const h = harness({}, { storage, generation: 0, navigation: 2000 }); await h.settle(); assert.ok(h.value.unknown);
  await h.value.recover(); await h.settle(); assert.equal(h.value.unknown, null); assert.equal(stored(h).idempotency_key, original);
  assert.equal(h.count("requestAssistantControl"), 0); h.unmount();
});

test("original-key query times out without auto replay and ignores a late successful receipt", async () => {
  const late = deferred(), h = harness({ requestAssistantControl: lose,
    getAssistantControl: (id, options) => options ? late.promise : Promise.resolve(view()) });
  await consent(h); await h.value.issue("pause"); await h.settle(); const pending = h.value.recover(); await h.settle();
  await h.value.recover(); assert.equal(h.count("getAssistantControl"), 2);
  await h.advance(90000); await pending; await h.settle(); assert.ok(h.value.unknown);
  late.resolve(paused()); await h.settle(); assert.ok(h.value.unknown); assert.equal(h.value.control.command, null);
  assert.equal(h.count("requestAssistantControl"), 1); h.unmount();
});

for (const kind of ["post", "receipt", "read"]) {
  test(`late ${kind} failure cannot replace a new execution's successful read or error state`, async () => {
    const late = deferred(); let delaying = false;
    const h = harness({ requestAssistantControl: kind === "post" ? () => late.promise : lose,
      getAssistantControl: (id, options) => {
        if (delaying && (kind === "read" || options)) { delaying = false; return late.promise; }
        return Promise.resolve(view({ execution_id: id, plan_id: pid(id) }));
      } });
    await consent(h); let pending;
    if (kind === "read") { delaying = true; pending = h.value.refresh(); }
    else { pending = h.value.issue("pause"); if (kind === "receipt") { await pending; await h.settle(); delaying = true; pending = h.value.recover(); } }
    await h.settle(); await h.update({ executionId: 2 }); const error = h.value.error;
    late.reject(Object.assign(new Error("private late error"), { status: 503 })); await pending; await h.settle();
    assert.equal(h.value.control.execution_id, 2); assert.equal(h.value.error, error); assert.equal(h.changed.length, 0); h.unmount();
  });
}

test("strict locator schema never persists reason, token, result, URL, future fields or excessive TTL", async () => {
  const h = harness({ requestAssistantControl: lose }); await consent(h); await h.value.issue("pause"); await h.settle(); const locator = stored(h);
  assert.equal(transport.validateAssistantControlRecovery(locator), locator);
  for (const invalid of [{ ...locator, reason: "private" }, { ...locator, token: "private" }, { ...locator, result_ref: {} },
    { ...locator, expires_at: locator.created_at + 86400001 }, { ...locator, login_epoch: "short" }, { ...locator, execution_id: "1" },
    { ...locator, command_id: "invalid" }, { ...locator, uncertain: false }, { ...locator, idempotency_key: "https://invalid" }]) {
    assert.throws(() => transport.validateAssistantControlRecovery(invalid), TypeError);
  }
  h.unmount();
});

test("real receipt validator rejects a wrong command without overwriting original journal or clearing unknown", async (t) => {
  t.mock.method(shared.api, "request", async () => accepted({ command: command({ action: "stop" }) }));
  const h = harness({ requestAssistantControl: lose, getAssistantControl: (id, options) => options ? transport.getAssistantControl(id, options) : Promise.resolve(view()) });
  await consent(h); await h.value.issue("pause"); await h.settle(); const before = h.storage.get(recoveryKey);
  await h.value.recover(); await h.settle(); assert.ok(h.value.unknown); assert.equal(h.storage.get(recoveryKey), before); assert.equal(h.value.control.command, null); h.unmount();
});

test("parent callback failure cannot invent an unknown command or trigger a replay", async () => {
  const h = harness({}, { props: { onChanged: () => { throw new Error("parent unavailable"); } } }); await consent(h); await h.value.issue("pause"); await h.settle();
  assert.equal(h.value.unknown, null); assert.equal(h.count("requestAssistantControl"), 1); assert.equal(stored(h).uncertain, false); h.unmount();
});

test("dynamic inactivity before React commit blocks every entry point and cached record visibility", async () => {
  let active = true;
  const h = harness({}, { props: { isActive: () => active } }); await consent(h);
  const snapshot = h.value, count = h.calls.length;
  active = false; // No rerender or unmount: the parent's close handler has only changed its ref.
  assert.equal(snapshot.visible(snapshot.record), false);
  await snapshot.issue("pause"); await snapshot.refresh(); await snapshot.recover(); await snapshot.initialize();
  assert.equal(h.calls.length, count); assert.equal(h.storage.size, 0); assert.equal(h.changed.length, 0); h.unmount();
});

test("inactive or failing activity predicates cannot start initialization or HTTP", async () => {
  for (const isActive of [() => false, () => { throw new Error("closed"); }, null]) {
    const h = harness({}, { props: { isActive } }); await h.settle();
    await h.value.refresh(); await h.value.recover(); await h.value.issue("probe");
    assert.equal(h.calls.length, 0); assert.equal(h.storage.size, 0); h.unmount();
  }
});

for (const kind of ["post", "receipt", "read", "identity"]) {
  for (const result of ["success", "failure"]) {
    test(`dynamic close-to-unmount ${kind} ${result} cannot publish, confirm journal or notify`, async () => {
      let active = true, delayRead = false;
      const late = deferred();
      const h = harness({
        getAssistantControlSessionFingerprint: kind === "identity" ? () => late.promise : async () => digest(777),
        requestAssistantControl: kind === "post" ? () => late.promise : lose,
        getAssistantControl: (id, options) => {
          if ((kind === "receipt" && options) || (kind === "read" && delayRead)) return late.promise;
          return Promise.resolve(view());
        },
      }, { props: { isActive: () => active } });
      let pending;
      if (kind === "identity") await h.settle();
      else {
        await consent(h);
        if (kind === "read") { delayRead = true; pending = h.value.refresh(); }
        else { pending = h.value.issue("pause"); if (kind === "receipt") { await pending; await h.settle(); pending = h.value.recover(); } }
        await h.settle();
      }
      const raw = h.storage.get(recoveryKey), before = h.value.record;
      active = false;
      if (result === "failure") late.reject(Object.assign(new Error("private hidden response"), { status: 503 }));
      else late.resolve(kind === "identity" ? digest(777) : accepted());
      if (pending) await pending;
      await h.settle();
      assert.equal(h.storage.get(recoveryKey), raw); assert.equal(h.value.record, before); assert.equal(h.changed.length, 0);
      if (kind === "post" || kind === "receipt") assert.equal(stored(h).uncertain, true);
      if (kind === "identity") assert.equal(h.count("getAssistantControl"), 0);
      const count = h.calls.length; await h.value.issue("pause"); await h.value.refresh(); await h.value.recover();
      assert.equal(h.calls.length, count); h.unmount();
    });
  }
}

test("latest activity function also gates an async callback captured by an older render", async () => {
  const late = deferred(), h = harness({ requestAssistantControl: () => late.promise }, { props: { isActive: () => true } });
  await consent(h); const pending = h.value.issue("pause"); await h.settle();
  const raw = h.storage.get(recoveryKey);
  await h.update({ isActive: () => false }); late.resolve(paused()); await pending; await h.settle();
  assert.equal(h.storage.get(recoveryKey), raw); assert.equal(stored(h).uncertain, true); assert.equal(h.changed.length, 0); h.unmount();
});

test("closing during intent persistence prevents dispatch before commit without deleting the original locator", async () => {
  let active = true, closeOnWrite = false;
  const storage = new Map(), originalSet = storage.set.bind(storage);
  storage.set = (key, value) => { const answer = originalSet(key, value); if (closeOnWrite) active = false; return answer; };
  const h = harness({}, { storage, props: { isActive: () => active } }); await consent(h); closeOnWrite = true;
  await h.value.issue("pause"); await h.settle();
  assert.equal(h.count("requestAssistantControl"), 0); assert.equal(stored(h).uncertain, true); assert.equal(h.changed.length, 0); h.unmount();
});

for (const offset of [0, 1]) {
  test(`locator expires at click without rerender (${offset}ms past TTL), so only a manual read remains possible`, async (t) => {
    t.mock.method(shared.api, "request", async () => accepted());
    const clock = { now: Date.now() }, h = harness({ requestAssistantControl: lose,
      getAssistantControl: (id, options) => options ? transport.getAssistantControl(id, options) : Promise.resolve(view()) }, { clock });
    await consent(h); await h.value.issue("pause"); await h.settle();
    const locator = stored(h), raw = h.storage.get(recoveryKey), snapshot = h.value;
    assert.equal(snapshot.recoveryUsable, true); assert.ok(snapshot.unknown);
    clock.now = locator.expires_at + offset; // Deliberately no settle/update: reproduce the cached render boolean.
    await snapshot.recover(); await h.settle();
    assert.ok(h.value.unknown); assert.equal(h.value.gate, true); assert.equal(h.count("getAssistantControl"), 1);
    assert.equal(h.storage.get(recoveryKey), raw); assert.equal(h.count("requestAssistantControl"), 1);
    h.value.setManualKey(locator.idempotency_key); await h.settle(); await h.value.recover(); await h.settle();
    assert.ok(h.value.manual); assert.ok(h.value.unknown); assert.equal(h.value.gate, true); assert.equal(h.storage.get(recoveryKey), raw);
    assert.equal(h.calls.filter((call) => call.name === "getAssistantControl").at(-1).args[1].expected, undefined);
    assert.equal(h.changed.length, 0); h.unmount();
  });
}

test("locator expiry while original GET is in flight shows only a read-only receipt and keeps uncertain", async () => {
  const clock = { now: Date.now() }, late = deferred(), h = harness({ requestAssistantControl: lose,
    getAssistantControl: (id, options) => options ? late.promise : Promise.resolve(view()) }, { clock });
  await consent(h); await h.value.issue("pause"); await h.settle(); const locator = stored(h), raw = h.storage.get(recoveryKey);
  const pending = h.value.recover(); await h.settle(); clock.now = locator.expires_at + 1;
  late.resolve(accepted()); await pending; await h.settle();
  assert.ok(h.value.unknown); assert.ok(h.value.manual); assert.equal(h.value.gate, true); assert.equal(h.storage.get(recoveryKey), raw);
  assert.equal(h.changed.length, 0); assert.equal(h.count("requestAssistantControl"), 1); h.unmount();
});

test("Control API forwards captured authSession out-of-band without putting it into the request body", async (t) => {
  const authSession = { token: "synthetic-captured-token", generation: 4, loginEpoch: epochId(4) };
  t.mock.method(shared.api, "request", async (path, options) => {
    assert.equal(path, "/assistant/executions/1/pause"); assert.equal(options.authSession, authSession);
    assert.deepEqual(JSON.parse(options.body), { expected_control_version: 0, reason: "manual" }); return accepted();
  });
  await transport.requestAssistantControl(1, "pause", { expected_control_version: 0, reason: "manual" }, { idempotencyKey: key, planId: pid(), authSession });
});

test("Panel passes exactly its confirmation session and treats auth_session_changed as definite unsent", async () => {
  let captured;
  const h = harness({ requestAssistantControl: async (id, action, payload, options) => {
    captured = options.authSession;
    throw Object.assign(new Error("private changed identity"), { code: "auth_session_changed", status: 409 });
  } });
  await consent(h); await h.value.issue("pause"); await h.settle();
  assert.deepEqual(captured, { token: "control-test-only", generation: 1, loginEpoch: epochId() });
  assert.equal(h.value.fresh, false); assert.equal(h.value.unknown, null); assert.equal(h.storage.has(recoveryKey), false);
  assert.match(h.value.error, /未发送/); await consent(h); await h.value.issue("pause"); assert.equal(h.count("requestAssistantControl"), 1); h.unmount();
});

test("actual shared API rejects a stale captured session before fetch, and never sends authSession as a fetch option", async (t) => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage"), data = new Map(), fetched = [];
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
    getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: (key) => data.delete(key),
  } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, "localStorage", previous); else delete globalThis.localStorage; });
  const auth = await vite.ssrLoadModule("/src/modules/authStorage.js");
  t.mock.method(globalThis, "fetch", async (url, options) => { fetched.push(options); return new Response(JSON.stringify(accepted())); });
  auth.saveAuthToken("synthetic-bound-A"); const snapshot = auth.getAuthSession();
  await transport.requestAssistantControl(1, "pause", { expected_control_version: 0, reason: "manual" }, { idempotencyKey: key, planId: pid(), authSession: snapshot });
  assert.equal(fetched[0].headers.Authorization, "Bearer synthetic-bound-A"); assert.equal(Object.hasOwn(fetched[0], "authSession"), false);
  for (const token of ["synthetic-bound-A", "synthetic-bound-B"]) {
    auth.saveAuthToken(token);
    await assert.rejects(transport.requestAssistantControl(1, "pause", { expected_control_version: 0, reason: "manual" },
      { idempotencyKey: key, planId: pid(), authSession: snapshot }), { status: 409, code: "auth_session_changed" });
  }
  assert.equal(fetched.length, 1); auth.clearAuthToken();
});

test("actual auth rotation during intent persistence blocks POST even without a React session update", async () => {
  const authSource = readFileSync(new URL("../src/modules/authStorage.js", import.meta.url), "utf8");
  const code = parse(authSource, { sourceType: "module" }).program.body.map((node) => { const item = node.declaration || node; return authSource.slice(item.start, item.end); }).join("\n");
  for (const token of ["control-test-only", "synthetic-next-user"]) {
    const data = new Map(), boot = () => vm.runInNewContext(`${code}\n({getAuthSession,isAuthSessionCurrent,saveAuthToken})`, {
      crypto: globalThis.crypto, localStorage: { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: (key) => data.delete(key) },
    });
    const auth = boot(), other = boot(); auth.saveAuthToken("control-test-only");
    let rotate = false; const storage = new Map(), originalSet = storage.set.bind(storage);
    storage.set = (key, value) => { const answer = originalSet(key, value); if (rotate) { rotate = false; other.saveAuthToken(token); } return answer; };
    const h = harness({}, { auth, storage }); await consent(h); const before = auth.getAuthSession(); rotate = true;
    await h.value.issue("pause");
    assert.equal(stored(h).uncertain, true);
    await h.settle();
    assert.equal(auth.isAuthSessionCurrent(before), false); assert.equal(h.count("requestAssistantControl"), 0);
    assert.equal(h.value.journal, null); assert.equal(h.changed.length, 0); h.unmount();
  }
});

test("new panel remains isolated, accessible, responsive and explicit about unsupported remote guarantees", () => {
  const apiSource = readFileSync(new URL("../src/modules/assistantControlApi.js", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/assistantControl.css", import.meta.url), "utf8");
  assert.doesNotMatch(apiSource, /\bfetch\s*\(|api\.raw|\/execute|\/approval|setTimeout|console\./);
  assert.doesNotMatch(source, /Input\.TextArea|dangerouslySetInnerHTML|\/execute|inspection\.run\.execute|setInterval|AssistantBatchWorkspace|AssistantPlanWorkspace/);
  assert.match(source, /external_stop \/ external_fence: unsupported/); assert.match(source, /在途调用可能继续/);
  assert.match(source, /aria-label="控制操作原因"/); assert.match(source, /核对原控制请求/); assert.match(css, /@media \(max-width: 600px\)/);
});
