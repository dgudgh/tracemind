import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "@babel/parser";
import { createServer } from "vite";
import { createHash } from "node:crypto";
import vm from "node:vm";

const pid = (index = 1) => `pl${index.toString(16).padStart(26, "0")}`;
const bid = (index = 1) => `CB${index.toString(16).padStart(28, "0")}`;
const hash = (index = 1) => index.toString(16).padStart(64, "0");
const reader = "inspection.run.query";
const future = "2099-10-20T10:00:00.123456Z";
const definition = (capability_id = reader) => ({ capability_id, kind: "read", version: 1, definition_version: 1, plan_executable: true,
  execution: "server", availability: "available", authorized_engines: ["mysql"] });
const step = (overrides = {}) => ({ seq: 1, capability_id: reader, definition_version: 1, title: "Read",
  params: { page: 1, page_size: 20 }, depends_on: [], input_refs: {}, fanout: 1, retries: 0, ...overrides });
const preparedItem = (index = 1, overrides = {}) => ({ plan_id: pid(index), instance_id: index, revision: 1,
  binding_hash: hash(index), status: "validated", outcome: null, approval_state: "not_required", approval_available: false,
  expires_at: future, reused: false, ...overrides });
const payload = (overrides = {}) => ({ capability_id: reader, definition_version: 1,
  targets: [{ instance_id: 1, params: {} }], steps: [step()], valid_for_seconds: 3600, request_approval: false, ...overrides });
const preparation = (items = [preparedItem()]) => ({ schema: "capability.batch_preparation/v1", accepted: false,
  status: "prepared", total: items.length, items, notice: "冻结全部明确目标，尚未执行" });
const member = (index = 1, overrides = {}) => ({ plan_id: pid(index), seq: index, queue_state: "queued", reason: null,
  status: "validated", outcome: null, outcome_kind: null, bound_revision: 1, binding_hash: hash(index), binding_current: true,
  result_ref: null, attempt: 0, started_at: null, finished_at: null, ...overrides });
const batch = (overrides = {}) => ({ schema: "capability.batch_read/v1", batch_id: bid(), requested_by: 7,
  status: "queued", outcome: null, accepted: true, reused: false, total: 1, settled_plans: 0, queue_counts: { queued: 1 },
  outcomes: {}, requires_attention: false, cancel_requested: false, cancel_requested_at: null,
  created_at: "2026-10-20T09:00:00Z", deadline_at: "2026-10-20T10:00:00Z", items: [member()], ...overrides });
const summary = (value) => { const { items, ...rest } = value; return rest; };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
let vite, api, shared, state;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent",
    server: { middlewareMode: true, hmr: false } });
  api = await vite.ssrLoadModule("/src/modules/assistantBatchApi.js");
  shared = await vite.ssrLoadModule("/src/api.js");
  state = await vite.ssrLoadModule("/src/modules/assistantPlanState.js");
});
test.after(async () => { await vite?.close(); });

test("prepare, enqueue, history, detail, cancel and owner snapshot use shared routes only", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (path, options) => {
    calls.push([path, options]);
    if (path.endsWith("/prepare")) return preparation();
    if (path.includes("/plans/")) return { schema: "capability.plan_read/v1", ...preparedItem(), capability_id: reader, definition_version: 1, steps: [step()], result_ref: { private: "not returned" } };
    if (path.includes("?")) return { schema: "capability.batch_list/v1", items: [summary(batch())], total: 1, page: 2, page_size: 10 };
    return batch({ cancel_requested: path.endsWith("/cancel"), cancel_requested_at: path.endsWith("/cancel") ? "2026-10-20T09:30:00Z" : null });
  });
  await api.prepareAssistantBatch(payload(), { idempotencyKey: "prepare:one" });
  await api.enqueueAssistantBatch({ plan_ids: [pid()], deadline_seconds: 3600 }, { idempotencyKey: "enqueue:two" });
  await api.listAssistantBatches({ page: 2, page_size: 10 }); await api.getAssistantBatch(bid()); await api.cancelAssistantBatch(bid());
  assert.deepEqual(await api.getAssistantBatchPlanSnapshot(pid()), preparedItem());
  assert.deepEqual(calls.map(([path]) => path), ["/assistant/batches/prepare", "/assistant/batches", "/assistant/batches?page=2&page_size=10",
    `/assistant/batches/${bid()}`, `/assistant/batches/${bid()}/cancel`, `/assistant/plans/${pid()}`]);
  assert.equal(calls[0][1].headers["Idempotency-Key"], "prepare:one");
  assert.equal(calls[1][1].headers["Idempotency-Key"], "enqueue:two");
  assert.equal(calls[4][1].body, "{}"); assert.equal(calls.length, 6);
});

test("IDs and keys reject traversal, encodings, uppercase hex, suffixes and missing keys before I/O", (t) => {
  let calls = 0; t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const id of [undefined, null, 1, {}, bid().toLowerCase(), `${bid()}\n`, `${bid()}/cancel`, `${bid()}?role=admin`, `${bid()}%2f`, `CB${"A".repeat(28)}`, `CB${"a".repeat(29)}`]) {
    assert.throws(() => api.getAssistantBatch(id), TypeError); assert.throws(() => api.cancelAssistantBatch(id), TypeError);
  }
  for (const key of [undefined, null, "", "x".repeat(129), "key\n", "a b", "a/../b", "密钥"]) {
    assert.throws(() => api.prepareAssistantBatch(payload(), { idempotencyKey: key }), TypeError);
    assert.throws(() => api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: key }), TypeError);
  }
  assert.equal(calls, 0);
});

test("explicit targets are unique, bounded and typed; no SQL, URL, script, arbitrary JSON or forward refs", (t) => {
  let calls = 0; t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const patch of [{ targets: [] }, { targets: Array.from({ length: 101 }, (_, i) => ({ instance_id: i + 1 })) },
    { targets: [{ instance_id: 1 }, { instance_id: 1 }] }, { targets: [{ instance_id: "1" }] }, { targets: [{ instance_id: Number.MAX_SAFE_INTEGER + 1 }] },
    { targets: [{ instance_id: 1, params: null }] }, { targets: [{ instance_id: 1, params: { page: 2 } }] }, { targets: [{ instance_id: 1, params: { sql: "select 1" } }] },
    { capability_id: "inspection.run.execute" }, { definition_version: 2 }, { request_approval: "true" }, { valid_for_seconds: 59 }, { valid_for_seconds: 86401 },
    { steps: [] }, { steps: Array.from({ length: 21 }, (_, i) => step({ seq: i + 1 })) },
    { steps: [step({ depends_on: [1] })] }, { steps: [step({ params: { page: "${steps[1].id}" } })] },
    { steps: [step({ input_refs: { page: "steps[1].id" } })] }, { steps: [step({ params: { command: "uname" } })] },
    { steps: [step({ params: { url: "https://example.invalid" } })] }, { steps: [step({ fanout: 2 })] }, { actor_id: 9 }]) {
    assert.throws(() => api.prepareAssistantBatch(payload(patch), { idempotencyKey: "prepare:a" }), TypeError);
  }
  for (const value of [{ plan_ids: [] }, { plan_ids: [pid(), pid()] }, { plan_ids: [`${pid()}\n`] },
    { plan_ids: [pid()], deadline_seconds: 0 }, { plan_ids: [pid()], deadline_seconds: 86401 },
    { plan_ids: [pid()], expected_binding_hash: hash() }]) assert.throws(() => api.enqueueAssistantBatch(value, { idempotencyKey: "enqueue:a" }), TypeError);
  assert.throws(() => api.listAssistantBatches({ page: 0 }), TypeError);
  assert.throws(() => api.listAssistantBatches({ page_size: 101 }), TypeError);
  assert.throws(() => api.listAssistantBatches({ url: "https://example.invalid" }), TypeError);
  assert.equal(calls, 0);
});

test("100 explicit targets and a typed shared DAG use one prepare request without per-target POST loops", async (t) => {
  const targets = Array.from({ length: 100 }, (_, i) => ({ instance_id: i + 1, params: {} }));
  const steps = [step(), step({ seq: 2, capability_id: "inspection.template.read", depends_on: [1] })];
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; return preparation(targets.map((target) => preparedItem(target.instance_id))); });
  const result = await api.prepareAssistantBatch(payload({ targets, steps, valid_for_seconds: 86400 }), { idempotencyKey: "prepare:100" });
  assert.equal(result.total, 100); assert.equal(calls, 1); assert.equal(result.accepted, false);
});

test("prepare responses strictly correlate explicit target count/order, identity, revision, hash and TTL", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  for (response of [null, {}, "<html>private</html>", preparation([preparedItem(2)]),
    { ...preparation(), accepted: true }, { ...preparation(), total: 2 }, { ...preparation(), status: "queued" },
    preparation([preparedItem(1, { revision: 0 })]), preparation([preparedItem(1, { revision: "1" })]),
    preparation([preparedItem(1, { binding_hash: "x".repeat(64) })]), preparation([preparedItem(1, { expires_at: "2026-02-30T00:00:00Z" })]),
    preparation([preparedItem(1, { expires_at: "2026-10-20T10:00:00+08:00" })]), preparation([preparedItem(1, { reused: "false" })]),
    preparation([preparedItem(1, { status: "settled", outcome: null })])]) {
    await assert.rejects(api.prepareAssistantBatch(payload(), { idempotencyKey: "prepare:a" }), { code: "assistant_batch_response_invalid" });
  }
});

test("ACK alone, malformed counters/statuses, wrong IDs and unsafe result refs never become batch success", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  const malformed = [null, [], {}, { accepted: true, batch_id: bid() }, batch({ batch_id: bid(2) }), batch({ total: 101 }),
    batch({ status: "succeeded" }), batch({ outcome: "succeeded" }), batch({ accepted: false }), batch({ queue_counts: { queued: 2 } }),
    batch({ settled_plans: 1 }), batch({ requires_attention: true }), batch({ outcomes: { unknown: 1 } }),
    batch({ items: [] }), batch({ items: [member(1, { seq: 2 })] }), batch({ items: [member(1, { queue_state: "future" })] }),
    batch({ items: [member(1, { bound_revision: 0 })] }), batch({ items: [member(1, { binding_current: false })] }),
    batch({ items: [member(1, { result_ref: { url: "https://example.invalid" } })] }),
    batch({ items: [member(1, { attempt: 0.5 })] }), batch({ deadline_at: "2026-10-20T08:00:00Z" })];
  for (response of malformed) await assert.rejects(api.getAssistantBatch(bid()), { code: "assistant_batch_response_invalid" });
  response = batch({ batch_id: bid(3) });
  assert.equal((await api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: "enqueue:a" })).batch_id, bid(3));
  response = batch({ items: [member(2, { seq: 1 })] });
  await assert.rejects(api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: "enqueue:a" }), { code: "assistant_batch_response_invalid" });
});

test("cancelled delivery can be settled without falsely settling an unstarted plan", async (t) => {
  const value = batch({ status: "settled", outcome: "cancelled", queue_counts: { cancelled: 1 }, cancel_requested: true,
    cancel_requested_at: "2026-10-20T09:30:00Z", items: [member(1, { queue_state: "cancelled", binding_current: false })] });
  t.mock.method(shared.api, "request", async () => value);
  const result = await api.cancelAssistantBatch(bid());
  assert.equal(result.items[0].status, "validated"); assert.equal(result.settled_plans, 0); assert.equal(result.outcome, "cancelled");
});

test("history is bounded summaries only; nested member arrays and another page cannot slip through", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  const valid = { schema: "capability.batch_list/v1", total: 1, page: 1, page_size: 10, items: [summary(batch())] };
  for (response of [{ ...valid, items: [batch()] }, { ...valid, page: 2 }, { ...valid, page_size: 101 }, { ...valid, items: [null] }, { ...valid, total: 0 }]) {
    await assert.rejects(api.listAssistantBatches({ page: 1, page_size: 10 }), { code: "assistant_batch_response_invalid" });
  }
  response = valid; assert.equal(await api.listAssistantBatches({ page: 1, page_size: 10 }), valid);
});

for (const status of [401, 403, 404, 409, 422, 500, 503, 504]) {
  test(`HTTP ${status} is propagated once with no fallback, implicit approve or replay`, async (t) => {
    const failure = Object.assign(new Error("private-body"), { status }); let calls = 0;
    t.mock.method(shared.api, "request", async () => { calls += 1; throw failure; });
    for (const invoke of [() => api.prepareAssistantBatch(payload(), { idempotencyKey: "p:a" }),
      () => api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: "e:b" }), () => api.cancelAssistantBatch(bid())]) {
      await assert.rejects(invoke(), (error) => error === failure);
    }
    assert.equal(calls, 3);
  });
}

test("actual shared HTTP rejects HTML/empty/non-JSON success without inventing accepted data", async (t) => {
  let response;
  t.mock.method(globalThis, "fetch", async () => response);
  for (response of [new Response("<html>private</html>", { status: 200 }), new Response(null, { status: 204 }), new Response("null", { status: 202 })]) {
    await assert.rejects(api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: "e:a" }), { code: "assistant_batch_response_invalid" });
  }
  response = new Response(JSON.stringify(batch()), { status: 202 });
  assert.equal((await api.enqueueAssistantBatch({ plan_ids: [pid()] }, { idempotencyKey: "e:a" })).status, "queued");
});

test("receipt recovery uses only explicit original-key GETs, strict v1, and accepts admission reused:false", async (t) => {
  const calls = [], bindings = [(({ plan_id, instance_id, revision, binding_hash }) => ({ plan_id, instance_id, revision, binding_hash }))(preparedItem())];
  t.mock.method(shared.api, "request", async (path, options) => {
    calls.push([path, options]); return path.endsWith("preparation") ? preparation([preparedItem(1, { reused: true })]) : batch();
  });
  await api.getAssistantBatchPreparation({ idempotencyKey: "prepare:original", payload: payload(), bindings });
  assert.equal((await api.getAssistantBatchAdmission({ idempotencyKey: "enqueue:original", payload: { plan_ids: [pid()], deadline_seconds: 3600 }, bindings, ownerId: 7 })).reused, false);
  assert.deepEqual(calls.map(([path]) => path), ["/assistant/batches/preparation", "/assistant/batches/admission"]);
  assert.equal(calls[0][1].headers["Idempotency-Key"], "prepare:original");
  assert.equal(calls[1][1].headers["Idempotency-Key"], "enqueue:original");
  assert.ok(calls.every(([, options]) => options.headers["X-Assistant-Batch-Version"] === "1" && !options.method && !options.body));
});

test("receipt GET rejects changed targets, IDs, revisions, hashes, owner and version/control DTO fields", async (t) => {
  const bindings = [(({ plan_id, instance_id, revision, binding_hash }) => ({ plan_id, instance_id, revision, binding_hash }))(preparedItem())];
  let response; t.mock.method(shared.api, "request", async () => response);
  for (response of [preparation([preparedItem(2)]), preparation([preparedItem(1, { plan_id: pid(2) })]),
    preparation([preparedItem(1, { revision: 2 })]), preparation([preparedItem(1, { binding_hash: hash(2) })]),
    { ...preparation(), receipt: "not-a-v1-field" }]) {
    await assert.rejects(api.getAssistantBatchPreparation({ idempotencyKey: "p:a", payload: payload(), bindings }), { code: "assistant_batch_response_invalid" });
  }
  for (response of [batch({ requested_by: 8 }), batch({ items: [member(1, { bound_revision: 2 })] }), batch({ items: [member(1, { binding_hash: hash(2) })] }),
    batch({ batch_id: bid(2) }), batch({ status: "paused" }), { ...batch(), schema_version: 2 }, { ...batch(), controls: {} }]) {
    await assert.rejects(api.getAssistantBatchAdmission({ idempotencyKey: "e:a", payload: { plan_ids: [pid()] }, bindings, ownerId: 7, batchId: bid() }), { code: "assistant_batch_response_invalid" });
  }
});

test("controlled batch v1 conflict is safely typed and never opts into v2 or retries", async (t) => {
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; throw Object.assign(new Error("private"), { status: 409, responseData: { detail: { code: "batch_control_requires_v2" } } }); });
  await assert.rejects(api.getAssistantBatchAdmission({ idempotencyKey: "e:a" }), { status: 409, code: "batch_control_requires_v2" });
  assert.equal(calls, 1);
});

test("recovery identity is a live owner read plus irreversible SHA-256, never the token itself", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (...args) => { calls.push(args); return { id: 7, username: "reader" }; });
  const value = await api.getAssistantBatchRecoveryIdentity("receipt-test-only-token");
  assert.deepEqual(value, { user_id: 7, session_fingerprint: createHash("sha256").update("receipt-test-only-token").digest("hex") });
  assert.deepEqual(calls, [["/auth/current-user", {}]]); assert.equal(JSON.stringify(value).includes("receipt-test-only-token"), false);
});

const source = readFileSync(new URL("../src/AssistantBatchWorkspace.jsx", import.meta.url), "utf8");
const sourceAst = parse(source, { sourceType: "module", plugins: ["jsx"] });
const component = sourceAst.program.body.find((node) => node.declaration?.id?.name === "AssistantBatchWorkspace").declaration;
function hasJsx(node) {
  if (!node || typeof node !== "object") return false;
  if (["JSXElement", "JSXFragment"].includes(node.type)) return true;
  return Object.values(node).some((value) => (Array.isArray(value) ? value : [value]).some(hasJsx));
}
const statements = component.body.body.filter((node) => node.type !== "ReturnStatement" && !hasJsx(node));
const names = statements.filter((node) => node.type === "VariableDeclaration").flatMap((node) => node.declarations.flatMap((value) => value.id.type === "ArrayPattern" ? value.id.elements.filter(Boolean).map((item) => item.name) : [value.id.name]));
const helpers = sourceAst.program.body.filter((node) => node.type === "VariableDeclaration" || node.type === "FunctionDeclaration").map((node) => source.slice(node.start, node.end)).join("\n");
const callbackBody = `${helpers}\nconst ${source.slice(component.params[0].start, component.params[0].end)} = props;
  ${statements.map((node) => source.slice(node.start, node.end)).join("\n")}\nreturn {${names.join(",")}, readyForAdmission, sameBatchBinding, batchView, batchError, batchMemberTarget};`;

// Source-extracted production callbacks and hooks, not a DOM or browser simulation.
function harness(overrides = {}, props = {}, options = {}) {
  const slots = [], effects = [], timers = new Map(), calls = [];
  const storage = options.storage || new Map();
  const storageFailures = options.storageFailures || {};
  let cursor = 0, dirty = true, now = 0, timerId = 0, generation = options.generation ?? 1, value;
  let loginEpoch = options.loginEpoch || "login-epoch-00000001";
  let properties = { open: true, onClose() {}, onOpenPlan() {}, instances: [{ id: 1, db_type: "mysql", host: "example1", port: 3306 }, { id: 2, db_type: "mysql", host: "example2", port: 3306 }], ...props };
  const defaults = {
    listAssistantCapabilities: async () => ({ items: [definition(), definition("inspection.template.read"), definition("operations.capability_matrix.read")] }),
    listAssistantBatches: async ({ page, page_size }) => ({ schema: "capability.batch_list/v1", items: [], total: 0, page, page_size }),
    prepareAssistantBatch: async (input) => preparation(input.targets.map((target) => preparedItem(target.instance_id, input.request_approval ? { approval_state: "pending", approval_available: true } : {}))),
    enqueueAssistantBatch: async (input) => batch({ total: input.plan_ids.length, queue_counts: { queued: input.plan_ids.length }, items: input.plan_ids.map((id, i) => member(i + 1, { plan_id: id })) }),
    getAssistantControlBatch: async (id) => batch({ batch_id: id }), cancelAssistantBatch: async (id) => batch({ batch_id: id, cancel_requested: true }),
    getAssistantBatchPlanSnapshot: async (id) => preparedItem(Number.parseInt(id.slice(2), 16)),
    getAssistantBatchRecoveryIdentity: async () => ({ user_id: 7, session_fingerprint: hash(777) }),
    getAssistantBatchPreparation: async (input) => preparation((input.payload?.targets || [{ instance_id: 1 }]).map((target) => preparedItem(target.instance_id))),
    getAssistantBatchAdmission: async (input) => batch({ total: input.payload?.plan_ids.length || 1, queue_counts: { queued: input.payload?.plan_ids.length || 1 },
      items: (input.payload?.plan_ids || [pid()]).map((id, i) => member(i + 1, { plan_id: id, bound_revision: input.bindings?.[i]?.revision || 1, binding_hash: input.bindings?.[i]?.binding_hash || hash(i + 1) })) }),
  };
  const wrapped = Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name, (...args) => { calls.push({ name, args }); return (overrides[name] || fallback)(...args); }]));
  const dependencies = { ...wrapped, ASSISTANT_BATCH_READERS: api.ASSISTANT_BATCH_READERS, isAssistantBatchId: api.isAssistantBatchId,
    validateAssistantBatchRecovery: api.validateAssistantBatchRecovery,
    globalThis: { crypto: globalThis.crypto, performance: { timeOrigin: options.navigation ?? 1000 }, sessionStorage: {
      getItem: (key) => { if (storageFailures.get) throw new Error("storage denied"); return storage.get(key) ?? null; },
      setItem: (key, text) => { if (storageFailures.set) throw new Error("storage denied"); storage.set(key, text); },
      removeItem: (key) => { if (storageFailures.remove) throw new Error("storage denied"); storage.delete(key); },
    } },
    changeAssistantSteps: state.changeAssistantSteps,
    getAuthSession: options.auth?.getAuthSession || (() => ({ token: "batch-test-only", generation, loginEpoch })),
    isAuthSessionCurrent: options.auth?.isAuthSessionCurrent || ((session) => session.generation === generation && session.loginEpoch === loginEpoch),
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { at: now + delay, callback }); return id; }, clearTimeout: (id) => timers.delete(id),
    useState: (initial) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (update) => { const next = typeof update === "function" ? update(slots[i].value) : update; if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true; } }]; },
    useRef: (initial) => { const i = cursor++; slots[i] ??= { ref: { current: initial } }; return slots[i].ref; },
    useEffect: (callback, deps) => { const i = cursor++, old = slots[i]; if (!old || deps.some((dep, index) => !Object.is(dep, old.deps[index]))) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: callback() }; }); },
  };
  const render = new Function(...Object.keys(dependencies), "props", callbackBody);
  const settle = async () => { for (let pass = 0; pass < 40; pass += 1) { if (dirty) { dirty = false; cursor = 0; value = render(...Object.values(dependencies), properties); while (effects.length) effects.shift()(); } await Promise.resolve(); } assert.equal(dirty, false); };
  return { get value() { return value; }, calls, storage, count: (name) => calls.filter((call) => call.name === name).length, settle,
    update: async (patch) => { properties = { ...properties, ...patch }; dirty = true; await settle(); },
    session: async () => { generation += 1; loginEpoch = `login-epoch-${generation.toString().padStart(8, "0")}`; dirty = true; await settle(); },
    advance: async (duration) => { const target = now + duration; for (;;) { const next = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0]; if (!next) break; timers.delete(next[0]); now = next[1].at; next[1].callback(); await settle(); } now = target; await settle(); },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}

async function choose(h, targets = [1, 2]) {
  await h.settle(); h.value.editDraft(() => h.value.setTargetIds(targets)); await h.settle();
  h.value.setConfirmPrepare(true); await h.settle();
}
async function freeze(h, targets = [1]) { await choose(h, targets); await h.value.mutate("prepare"); await h.settle(); }

test("mount/open/history queries never choose default targets, freeze, enqueue or cancel", async () => {
  const h = harness(); await h.settle();
  assert.deepEqual(h.value.targetIds, []); assert.equal(h.value.ready, false);
  await h.value.mutate("prepare"); await h.value.mutate("enqueue");
  h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true });
  assert.equal(h.count("prepareAssistantBatch"), 0); assert.equal(h.count("enqueueAssistantBatch"), 0); assert.equal(h.count("cancelAssistantBatch"), 0); h.unmount();
});

test("explicit targets freeze once; separate confirmation admits once with an independent key", async () => {
  const h = harness(); await freeze(h, [1, 2]);
  assert.equal(h.count("prepareAssistantBatch"), 1); assert.equal(h.count("enqueueAssistantBatch"), 0);
  const prepareCall = h.calls.find((call) => call.name === "prepareAssistantBatch");
  assert.deepEqual(prepareCall.args[0].targets, [{ instance_id: 1, params: {} }, { instance_id: 2, params: {} }]);
  assert.equal(prepareCall.args[0].steps.length, 1); assert.equal(h.value.prepared.accepted, false);
  await h.value.mutate("enqueue"); assert.equal(h.count("enqueueAssistantBatch"), 0);
  h.value.setConfirmEnqueue(true); await h.settle(); const operation = h.value.mutate("enqueue"); await h.value.mutate("enqueue"); await operation; await h.settle();
  const enqueueCall = h.calls.find((call) => call.name === "enqueueAssistantBatch");
  assert.notEqual(prepareCall.args[1].idempotencyKey, enqueueCall.args[1].idempotencyKey);
  assert.deepEqual(enqueueCall.args[0].plan_ids, [pid(1), pid(2)]); assert.equal(h.count("enqueueAssistantBatch"), 1);
  assert.equal(h.value.batch.status, "queued"); assert.equal(h.value.batch.outcome, null); h.unmount();
});

test("history browsing does not consume an unadmitted preparation or allow replay of an admitted one", async () => {
  const h = harness(); await freeze(h); await h.value.readBatch(bid(8)); await h.settle();
  h.value.changeTab("new"); await h.settle(); assert.equal(h.value.canEnqueue, true);
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  assert.equal(h.value.admitted, true); await h.value.readBatch(bid(9)); await h.settle(); h.value.changeTab("new"); await h.settle();
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); assert.equal(h.count("enqueueAssistantBatch"), 1); h.unmount();
});

test("target metadata change during submitted preparation preserves its original key and unknown lock", async () => {
  const late = deferred(), h = harness({ prepareAssistantBatch: () => late.promise }); await choose(h, [1]);
  const operation = h.value.mutate("prepare"); await h.settle(); const key = h.value.keyPair.current.prepare;
  await h.update({ instances: [{ id: 1, db_type: "mysql", host: "replacement", port: 3306 }] });
  assert.equal(h.value.keyPair.current.prepare, key); assert.equal(h.value.unknown.kind, "prepare");
  late.resolve(preparation()); await operation; await h.settle(); assert.equal(h.value.prepared, null);
  await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
});

test("shared DAG is typed and preserves prior-only dependencies for every target", async () => {
  const h = harness(); await choose(h);
  h.value.editSteps({ type: "add", step: { key: "second", capability_id: "inspection.template.read", params: { page: 2, page_size: 30 } } }); await h.settle();
  h.value.editDraft(() => h.value.setSteps(h.value.steps.map((item, index) => index ? { ...item, depends_on: ["step-1"] } : item))); await h.settle();
  h.value.setConfirmPrepare(true); await h.settle(); await h.value.mutate("prepare"); await h.settle();
  const input = h.calls.find((call) => call.name === "prepareAssistantBatch").args[0];
  assert.deepEqual(input.steps[1].depends_on, [1]); assert.deepEqual(input.steps[1].params, { page: 2, page_size: 30 });
  assert.ok(input.targets.every((target) => Object.keys(target.params).length === 0)); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
});

test("unapproved, blocked, needs_input, unsupported, expired and unknown plans cannot be admitted", async () => {
  const h = harness(); await h.settle();
  for (const patch of [{ approval_state: "pending", approval_available: true }, { status: "awaiting_approval", approval_state: "pending" },
    { status: "settled", outcome: "blocked" }, { status: "settled", outcome: "needs_input" }, { status: "settled", outcome: "unsupported" },
    { status: "settled", outcome: "unknown" }, { expires_at: "2020-01-01T00:00:00Z" }, { status: "approved", approval_state: "revoked" }]) {
    assert.equal(h.value.readyForAdmission(preparedItem(1, patch)), false);
  }
  assert.equal(h.value.readyForAdmission(preparedItem(1, { status: "approved", approval_state: "approved", approval_available: true })), true); h.unmount();
});

test("manual owner refresh permits an unchanged approved binding but rejects revision/target changes", async () => {
  let response = preparedItem(1, { status: "approved", approval_state: "approved", approval_available: true });
  const h = harness({ prepareAssistantBatch: async () => preparation([preparedItem(1, { approval_state: "pending", approval_available: true })]), getAssistantBatchPlanSnapshot: async () => response });
  await freeze(h); assert.equal(h.value.canEnqueue, false);
  await h.value.refreshPlan(h.value.prepared.items[0]); await h.settle(); assert.equal(h.value.canEnqueue, true);
  response = { ...response, revision: 2 }; await h.value.refreshPlan(h.value.prepared.items[0]); await h.settle();
  assert.equal(h.value.canEnqueue, false); assert.equal(h.value.prepared.items[0].revision, 1);
  assert.equal(h.value.reviews[pid()].conflict, true); assert.equal(h.count("enqueueAssistantBatch"), 0); h.unmount();
});

for (const kind of ["prepare", "enqueue", "cancel"]) {
  for (const mode of ["network", "5xx", "protocol", "timeout"]) {
    test(`${kind} ${mode} is unknown, blocks double clicks/new keys and ignores late success`, async () => {
      const late = deferred(), method = { prepare: "prepareAssistantBatch", enqueue: "enqueueAssistantBatch", cancel: "cancelAssistantBatch" }[kind];
      const h = harness({ [method]: () => late.promise });
      if (kind === "prepare") await choose(h, [1]);
      if (kind === "enqueue") { await freeze(h); h.value.setConfirmEnqueue(true); await h.settle(); }
      if (kind === "cancel") { await h.settle(); await h.value.readBatch(bid()); await h.settle(); h.value.setConfirmCancel(true); await h.settle(); }
      const operation = h.value.mutate(kind); await h.settle(); await h.value.mutate(kind); assert.equal(h.count(method), 1);
      if (mode === "timeout") await h.advance(90000);
      else late.reject(mode === "5xx" ? Object.assign(new Error("sensitive=not-for-ui"), { status: 503 })
        : mode === "protocol" ? Object.assign(new Error("sensitive=not-for-ui"), { code: "assistant_batch_response_invalid" }) : new TypeError("sensitive=not-for-ui"));
      await operation; await h.settle(); assert.equal(h.value.unknown.kind, kind);
      assert.equal(h.value.error.includes("sensitive="), false);
      h.value.newDraft(); await h.value.mutate(kind); assert.equal(h.count(method), 1);
      late.resolve(kind === "prepare" ? preparation() : batch({ cancel_requested: kind === "cancel" })); await h.settle();
      assert.equal(h.value.unknown.kind, kind); h.unmount();
    });
  }
}

for (const change of ["close", "targets", "session", "unmount"]) {
  test(`${change} invalidates pending preparation publication, without enqueue/cancel`, async () => {
    const late = deferred(), h = harness({ prepareAssistantBatch: () => late.promise }); await choose(h, [1]);
    const operation = h.value.mutate("prepare"); await h.settle();
    if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true }); }
    if (change === "targets") await h.update({ instances: [{ id: 1, db_type: "mysql", host: "replacement", port: 3306 }] });
    if (change === "session") await h.session();
    if (change === "unmount") h.unmount();
    late.resolve(preparation()); await operation; await h.settle();
    assert.equal(h.value.prepared, null); assert.equal(h.count("enqueueAssistantBatch"), 0); assert.equal(h.count("cancelAssistantBatch"), 0); h.unmount();
  });
}

test("late detail A cannot replace B, a newly edited draft, or a closed/reopened view", async () => {
  for (const change of ["newer", "draft", "close"]) {
    const late = deferred(); const h = harness({ getAssistantControlBatch: (id) => id === bid(1) ? late.promise : Promise.resolve(batch({ batch_id: id })) }); await h.settle();
    const old = h.value.readBatch(bid(1)); await h.settle();
    if (change === "newer") { await h.value.readBatch(bid(2)); await h.settle(); }
    if (change === "draft") { h.value.changeTab("new"); h.value.editDraft(() => h.value.setTargetIds([2])); await h.settle(); }
    if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true }); }
    late.resolve(batch()); await old; await h.settle();
    if (change === "newer") assert.equal(h.value.batch.batch_id, bid(2)); else assert.equal(h.value.batch, null);
    if (change === "draft") { assert.equal(h.value.tab, "new"); assert.deepEqual(h.value.targetIds, [2]); }
    h.unmount();
  }
});

test("late plan approval refresh cannot repopulate a changed draft or permit admission", async () => {
  const late = deferred(), h = harness({ getAssistantBatchPlanSnapshot: () => late.promise }); await freeze(h);
  const operation = h.value.refreshPlan(h.value.prepared.items[0]); await h.settle();
  h.value.editDraft(() => h.value.setTargetIds([2])); await h.settle();
  late.resolve(preparedItem(1, { status: "approved", approval_state: "approved", approval_available: true })); await operation; await h.settle();
  assert.equal(h.value.prepared, null); assert.equal(h.value.canEnqueue, false); assert.deepEqual(h.value.targetIds, [2]); h.unmount();
});

test("v2 detail projects persisted target identity and never borrows the current instance header", async () => {
  const target = { instance_id: 2, db_type: "oracle", capability_id: "inspection.run.query" };
  const h = harness({ getAssistantControlBatch: async (id) => batch({ batch_id: id, items: [member(1, target)] }) }, {
    instances: [{ id: 1, instance_name: "控制库", db_type: "mysql" }, { id: 2, instance_name: "Oracle 生产一库", db_type: "oracle" }],
  });
  await h.settle(); await h.value.readBatch(bid()); await h.settle();
  assert.equal(h.count("getAssistantControlBatch"), 1);
  assert.equal(h.calls.find((call) => call.name === "getAssistantControlBatch").args[1].authSession.token, "batch-test-only");
  assert.deepEqual(h.value.batchMemberTarget(h.value.batch.items[0], [{ id: 1, instance_name: "控制库", db_type: "mysql" }, { id: 2, instance_name: "Oracle 生产一库", db_type: "oracle" }]),
    { name: "Oracle 生产一库", engine: "ORACLE" });
  assert.deepEqual(h.value.batchMemberTarget(member(), [{ id: 1, instance_name: "控制库", db_type: "mysql" }]), { name: "待回读", engine: "" });
  assert.deepEqual(h.value.batchMemberTarget({ ...member(), ...target }, [{ id: 1, instance_name: "控制库", db_type: "mysql" }]), { name: "实例 2", engine: "ORACLE" });
  h.unmount();
});

test("cancellation preserves API in-flight state and closing never sends cancel", async () => {
  const running = batch({ total: 2, status: "running", queue_counts: { queued: 1, running: 1 }, items: [member(), member(2, { queue_state: "running", status: "running", attempt: 1 })] });
  const cancelled = { ...running, cancel_requested: true, queue_counts: { cancelled: 1, running: 1 }, items: [{ ...running.items[0], queue_state: "cancelled" }, running.items[1]] };
  const h = harness({ getAssistantControlBatch: async () => running, cancelAssistantBatch: async () => cancelled }); await h.settle();
  await h.value.readBatch(bid()); await h.settle(); h.value.setConfirmCancel(true); await h.settle();
  await h.value.mutate("cancel"); await h.settle(); assert.equal(h.value.batch.status, "running"); assert.equal(h.value.batch.outcome, null);
  assert.equal(h.value.batch.settled_plans, 0); h.value.closeWorkspace(); await h.update({ open: false }); assert.equal(h.count("cancelAssistantBatch"), 1); h.unmount();
});

test("unknown enqueue requires the original admission-key GET; even matching ordinary history cannot clear it", async () => {
  const h = harness({ enqueueAssistantBatch: async () => { throw new TypeError("network"); }, getAssistantControlBatch: async (id) => id === bid(2)
    ? batch({ batch_id: id, items: [member(2, { seq: 1 })] }) : batch() });
  await freeze(h); h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  assert.equal(h.value.unknown.kind, "enqueue"); await h.value.readBatch(bid(2)); await h.settle(); assert.equal(h.value.unknown.kind, "enqueue");
  await h.value.readBatch(bid()); await h.settle(); assert.equal(h.value.unknown.kind, "enqueue");
  await h.value.checkOriginal("enqueue"); await h.settle(); assert.equal(h.value.unknown, null);
  assert.equal(h.count("getAssistantBatchAdmission"), 1); assert.equal(h.count("enqueueAssistantBatch"), 1); h.unmount();
});

test("prepare unknown cannot be cleared by history/new draft or a changed key", async () => {
  const h = harness({ prepareAssistantBatch: async () => { throw Object.assign(new Error("lost"), { status: 504 }); } }); await choose(h, [1]);
  await h.value.mutate("prepare"); await h.settle(); const key = h.value.keyPair.current.prepare;
  await h.value.loadHistory(); await h.value.readBatch(bid()); await h.settle(); h.value.newDraft(); await h.settle();
  assert.equal(h.value.unknown.kind, "prepare"); assert.equal(h.value.keyPair.current.prepare, key);
  await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
});

const recoveryStorageKey = "mesguard.assistant.batch-recovery.v1";
const lostResponse = () => Promise.reject(Object.assign(new Error("lost response"), { status: 504 }));
const storedRecovery = (h) => JSON.parse(h.storage.get(recoveryStorageKey));

test("lost preparation is recovered only by an explicit original-key GET, then requires admission confirmation", async () => {
  const h = harness({ prepareAssistantBatch: lostResponse }); await freeze(h, [1, 2]);
  const original = h.calls.find((call) => call.name === "prepareAssistantBatch");
  assert.equal(h.count("getAssistantBatchPreparation"), 0); assert.equal(h.value.unknown.kind, "prepare");
  const checking = h.value.checkOriginal("prepare"); await h.value.checkOriginal("prepare"); await checking; await h.settle();
  assert.equal(h.count("getAssistantBatchPreparation"), 1); assert.equal(h.value.unknown, null);
  const read = h.calls.find((call) => call.name === "getAssistantBatchPreparation").args[0];
  assert.equal(read.idempotencyKey, original.args[1].idempotencyKey); assert.deepEqual(read.payload, original.args[0]);
  assert.deepEqual(h.value.prepared.items.map((item) => item.instance_id), [1, 2]); assert.equal(h.value.confirmEnqueue, false);
  await h.value.mutate("enqueue"); assert.equal(h.count("enqueueAssistantBatch"), 0);
  assert.equal(h.count("prepareAssistantBatch"), 1); assert.equal(storedRecovery(h).bindings.length, 2); h.unmount();
});

for (const patch of [{ status: "awaiting_approval", approval_state: "pending", approval_available: true },
  { status: "settled", outcome: "succeeded" }, { status: "settled", outcome: "blocked" }]) {
  test(`recovered preparation ${patch.status}/${patch.outcome || patch.approval_state} cannot enqueue`, async () => {
    const h = harness({ prepareAssistantBatch: lostResponse, getAssistantBatchPreparation: async () => preparation([preparedItem(1, patch)]) });
    await freeze(h); await h.value.checkOriginal("prepare"); await h.settle();
    assert.equal(h.value.unknown, null); assert.equal(h.value.canEnqueue, false);
    h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue");
    assert.equal(h.count("enqueueAssistantBatch"), 0); h.unmount();
  });
}

test("lost admission preserves distinct original keys/payloads and preparation GET cannot clear its lock", async () => {
  const h = harness({ enqueueAssistantBatch: lostResponse }); await freeze(h);
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  const before = storedRecovery(h);
  assert.notEqual(before.keys.prepare, before.keys.enqueue); assert.equal(before.phase, "enqueue");
  await h.value.checkOriginal("prepare"); await h.settle(); assert.equal(h.value.unknown.kind, "enqueue");
  assert.deepEqual(storedRecovery(h).enqueue_payload, before.enqueue_payload);
  await h.value.checkOriginal("enqueue"); await h.settle();
  const read = h.calls.find((call) => call.name === "getAssistantBatchAdmission").args[0];
  assert.equal(read.idempotencyKey, before.keys.enqueue); assert.deepEqual(read.payload, before.enqueue_payload); assert.deepEqual(read.bindings, before.bindings);
  assert.equal(h.value.unknown, null); assert.equal(h.value.batch.reused, false); assert.equal(h.value.admitted, true);
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue");
  assert.equal(h.count("enqueueAssistantBatch"), 1); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
});

for (const kind of ["prepare", "enqueue"]) {
  for (const failure of [Object.assign(new Error("missing"), { status: 404 }), Object.assign(new Error("changed"), { status: 409 }),
    Object.assign(new Error("unavailable"), { status: 503 }), Object.assign(new Error("protocol"), { code: "assistant_batch_response_invalid" }),
    Object.assign(new Error("controlled"), { status: 409, code: "batch_control_requires_v2" })]) {
    test(`${kind} receipt ${failure.status || failure.code}/${failure.message} keeps original unknown and context`, async () => {
      const method = kind === "prepare" ? "getAssistantBatchPreparation" : "getAssistantBatchAdmission";
      const h = harness({ [kind === "prepare" ? "prepareAssistantBatch" : "enqueueAssistantBatch"]: lostResponse,
        [method]: async () => { throw failure; } });
      await freeze(h); if (kind === "enqueue") { h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle(); }
      const before = h.storage.get(recoveryStorageKey);
      await h.value.checkOriginal(kind); await h.settle();
      assert.equal(h.value.unknown.kind, kind); assert.equal(h.storage.get(recoveryStorageKey), before);
      h.value.newDraft(); await h.value.mutate(kind); assert.equal(h.count(kind === "prepare" ? "prepareAssistantBatch" : "enqueueAssistantBatch"), 1);
      assert.match(h.value.error, failure.code === "batch_control_requires_v2" ? /v2 控制视图/ : /unknown/); h.unmount();
    });
  }
}

for (const kind of ["prepare", "enqueue"]) {
  for (const change of ["close", "session", "target", "unmount"]) {
    test(`${kind} receipt late after ${change} cannot publish or replace saved bindings`, async () => {
      const late = deferred(), method = kind === "prepare" ? "getAssistantBatchPreparation" : "getAssistantBatchAdmission";
      const h = harness({ [kind === "prepare" ? "prepareAssistantBatch" : "enqueueAssistantBatch"]: lostResponse, [method]: () => late.promise });
      await freeze(h); if (kind === "enqueue") { h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle(); }
      const reading = h.value.checkOriginal(kind); await h.settle();
      if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true }); }
      if (change === "session") await h.session();
      if (change === "target") await h.update({ instances: [{ id: 1, host: "changed", db_type: "mysql", port: 3306 }] });
      if (change === "unmount") h.unmount();
      const before = h.storage.get(recoveryStorageKey);
      late.resolve(kind === "prepare" ? preparation() : batch()); await reading; await h.settle();
      assert.equal(h.storage.get(recoveryStorageKey), before);
      if (kind === "prepare" || change === "session") assert.equal(h.value.prepared, null);
      assert.equal(h.value.batch, null); if (change !== "session") assert.equal(h.value.unknown.kind, kind);
      assert.equal(h.count(method), 1); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
    });
  }
}

test("same persistent login epoch across full reload restores only locators, with no automatic GET receipt or POST", async () => {
  const first = harness({ prepareAssistantBatch: lostResponse }); await freeze(first, [1, 2]);
  const storage = first.storage, before = storedRecovery(first); first.unmount();
  const h = harness({}, {}, { storage, generation: 0, navigation: 2000 }); await h.settle();
  assert.equal(h.value.unknown.kind, "prepare"); assert.deepEqual(h.value.targetIds, [1, 2]); assert.equal(h.value.prepared, null);
  assert.equal(storedRecovery(h).generation, 0); assert.equal(storedRecovery(h).navigation_epoch, 2000);
  assert.equal(h.count("getAssistantBatchPreparation"), 0); assert.equal(h.count("prepareAssistantBatch"), 0);
  await h.value.checkOriginal("prepare"); await h.settle(); assert.equal(h.value.unknown, null);
  assert.deepEqual(storedRecovery(h).keys, before.keys); assert.equal(h.count("prepareAssistantBatch"), 0); h.unmount();
});

test("admission reload recovers by original GET with known bindings, never duplicate admission", async () => {
  const first = harness({ enqueueAssistantBatch: lostResponse }); await freeze(first);
  first.value.setConfirmEnqueue(true); await first.settle(); await first.value.mutate("enqueue"); await first.settle();
  const storage = first.storage, before = storedRecovery(first); first.unmount();
  const h = harness({}, {}, { storage, generation: 0, navigation: 2000 }); await h.settle();
  assert.equal(h.value.unknown.kind, "enqueue"); assert.equal(h.value.prepared, null);
  await h.value.checkOriginal("enqueue"); await h.settle(); assert.equal(h.value.unknown, null); assert.equal(h.value.batch.batch_id, bid());
  assert.deepEqual(storedRecovery(h).keys, before.keys); assert.equal(h.count("enqueueAssistantBatch"), 0);
  await h.value.checkOriginal("prepare"); await h.settle(); assert.equal(h.value.admitted, true); assert.equal(h.value.canEnqueue, false); h.unmount();
});

for (const change of ["epoch", "owner", "fingerprint", "generation"]) {
  test(`${change} change rejects old receipt even with generation reset or identical token fingerprint`, async () => {
    const first = harness({ prepareAssistantBatch: lostResponse }); await freeze(first); const storage = first.storage; first.unmount();
    const h = harness({ getAssistantBatchRecoveryIdentity: async () => ({ user_id: change === "owner" ? 8 : 7,
      session_fingerprint: change === "fingerprint" ? hash(888) : hash(777) }) }, {},
    { storage, generation: change === "generation" ? 2 : 0, navigation: change === "generation" ? 1000 : 2000,
      loginEpoch: change === "epoch" ? "login-epoch-00000002" : "login-epoch-00000001" });
    await h.settle(); assert.equal(h.value.recovery, null); assert.equal(h.value.prepared, null); assert.deepEqual(h.value.targetIds, []);
    assert.equal(storage.has(recoveryStorageKey), false); assert.equal(h.count("getAssistantBatchPreparation"), 0); h.unmount();
  });
}

function authStorageHarness(persistent = new Map(), failures = {}) {
  const authSource = readFileSync(new URL("../src/modules/authStorage.js", import.meta.url), "utf8");
  const authCode = parse(authSource, { sourceType: "module" }).program.body.map((node) => {
    const declaration = node.declaration || node; return authSource.slice(declaration.start, declaration.end);
  }).join("\n");
  const boot = () => vm.runInNewContext(`${authCode}\n({getAuthSession,isAuthSessionCurrent,saveAuthToken,clearAuthToken})`, {
    crypto: globalThis.crypto, localStorage: {
      getItem: (key) => { if (failures.get) throw new Error("storage denied"); return persistent.get(key) ?? null; },
      setItem: (key, value) => { if (failures.set) throw new Error("storage denied"); persistent.set(key, value); },
      removeItem: (key) => { if (failures.remove) throw new Error("storage denied"); persistent.delete(key); },
    },
  });
  return { boot, persistent };
}

test("actual authStorage normal full reload retains login epoch and recovers only through explicit original-key GET", async () => {
  const { boot } = authStorageHarness(), original = boot(); original.saveAuthToken("batch-test-only");
  const session = original.getAuthSession(); assert.match(session.loginEpoch, /^[a-f0-9]{32}$/);
  const first = harness({ prepareAssistantBatch: lostResponse }, {}, { auth: original }); await freeze(first);
  const storage = first.storage, saved = storedRecovery(first); first.unmount();
  assert.equal(saved.login_epoch, session.loginEpoch);
  const reloaded = boot(); assert.equal(reloaded.getAuthSession().generation, 0); assert.equal(reloaded.getAuthSession().loginEpoch, session.loginEpoch);
  const h = harness({}, {}, { storage, navigation: 2000, auth: reloaded }); await h.settle();
  assert.equal(h.value.unknown.kind, "prepare"); assert.equal(h.value.recoveryGate, false);
  assert.equal(h.count("getAssistantBatchPreparation"), 0); assert.equal(h.count("prepareAssistantBatch"), 0);
  await h.value.checkOriginal("prepare"); await h.settle(); assert.equal(h.value.unknown, null);
  assert.equal(h.calls.find((call) => call.name === "getAssistantBatchPreparation").args[0].idempotencyKey, saved.keys.prepare);
  assert.equal(h.count("prepareAssistantBatch"), 0); h.unmount();
});

for (const login of ["save", "clear-save"]) {
test(`actual authStorage same-token ${login} then reload rejects old receipt with no identity fallback`, async () => {
  const { boot } = authStorageHarness();
  const original = boot(); original.saveAuthToken("batch-test-only");
  const previous = original.getAuthSession(); assert.match(previous.loginEpoch, /^[a-f0-9]{32}$/);
  const first = harness({ prepareAssistantBatch: lostResponse }, {}, { auth: original }); await freeze(first);
  const storage = first.storage; first.unmount();
  if (login === "clear-save") {
    original.clearAuthToken();
    assert.match(original.getAuthSession().loginEpoch, /^[a-f0-9]{32}$/);
    assert.notEqual(original.getAuthSession().loginEpoch, previous.loginEpoch);
  }
  original.saveAuthToken("batch-test-only"); assert.notEqual(original.getAuthSession().loginEpoch, previous.loginEpoch);
  const reloaded = boot(); assert.equal(reloaded.getAuthSession().generation, 0);
  assert.equal(reloaded.getAuthSession().token, previous.token); assert.notEqual(reloaded.getAuthSession().loginEpoch, previous.loginEpoch);
  const h = harness({}, {}, { storage, navigation: 2000, auth: reloaded }); await h.settle();
  assert.equal(h.value.recovery, null); assert.equal(h.value.prepared, null); assert.deepEqual(h.value.targetIds, []);
  assert.equal(h.count("getAssistantBatchPreparation"), 0); assert.equal(h.count("prepareAssistantBatch"), 0);
  assert.equal(storage.has(recoveryStorageKey), false); assert.equal(h.value.recoveryGate, false);
  h.unmount();
});
}

test("actual authStorage persistent identity failure returns null and keeps receipt recovery locked", async () => {
  const { boot } = authStorageHarness(new Map(), { get: true, set: true, remove: true });
  const auth = boot(); auth.saveAuthToken("batch-test-only"); assert.equal(auth.getAuthSession().loginEpoch, null);
  const h = harness({}, {}, { auth }); await h.settle();
  assert.ok(h.value.unknown); assert.equal(h.value.recoveryGate, true); assert.equal(h.value.draftLocked, true);
  await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantBatch"), 0);
  h.value.setManualPrepareKey("prepare:known-original"); await h.settle(); await h.value.checkOriginal("prepare"); await h.settle();
  assert.equal(h.value.manual.kind, "prepare"); assert.ok(h.value.unknown); assert.equal(h.value.recoveryGate, true); h.unmount();
});

test("actual authStorage failed same-token login cannot revive an old receipt after storage recovers and reloads", async () => {
  const failures = {}, { boot } = authStorageHarness(new Map(), failures), original = boot();
  original.saveAuthToken("batch-test-only"); const previous = original.getAuthSession(); assert.match(previous.loginEpoch, /^[a-f0-9]{32}$/);
  const first = harness({ prepareAssistantBatch: lostResponse }, {}, { auth: original }); await freeze(first);
  const storage = first.storage; first.unmount();
  failures.set = true; original.saveAuthToken("batch-test-only"); assert.equal(original.getAuthSession().loginEpoch, null);
  failures.set = false; const reloaded = boot();
  const h = harness({}, {}, { storage, navigation: 2000, auth: reloaded }); await h.settle();
  assert.equal(h.value.recovery, null); assert.equal(h.value.prepared, null); assert.deepEqual(h.value.targetIds, []);
  assert.equal(h.count("getAssistantBatchPreparation"), 0); assert.equal(h.count("prepareAssistantBatch"), 0); h.unmount();
});

for (const kind of ["prepare", "enqueue", "cancel"]) {
  for (const sameToken of [false, true]) {
    test(`old confirmed ${kind} cannot submit after cross-tab ${sameToken ? "same-token login" : "identity replacement"} before rerender`, async () => {
      const { boot } = authStorageHarness(), original = boot(); original.saveAuthToken("batch-test-only");
      const h = harness({}, {}, { auth: original });
      if (kind === "prepare") await choose(h, [1]);
      else {
        await freeze(h);
        if (kind === "enqueue") h.value.setConfirmEnqueue(true);
        else { await h.value.readBatch(bid()); await h.settle(); h.value.setConfirmCancel(true); }
        await h.settle();
      }
      const method = { prepare: "prepareAssistantBatch", enqueue: "enqueueAssistantBatch", cancel: "cancelAssistantBatch" }[kind];
      const count = h.count(method);
      boot().saveAuthToken(sameToken ? "batch-test-only" : "different-test-session");
      // Deliberately invoke the previously rendered callback before React observes storage.
      await h.value.mutate(kind);
      assert.equal(h.count(method), count); await h.settle(); h.unmount();
    });
  }
}

test("identity lookup failure and close/session races never expose stored receipt or submit", async () => {
  for (const change of ["error", "close", "session"]) {
    const late = deferred(), h = harness({ getAssistantBatchRecoveryIdentity: () => late.promise }); await h.settle();
    if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); }
    if (change === "session") await h.session();
    if (change === "error") late.reject(new Error("private identity body"));
    else late.resolve({ user_id: 7, session_fingerprint: hash(777) });
    await h.settle(); assert.equal(h.value.recovery, null); assert.equal(h.value.prepared, null);
    assert.equal(h.count("prepareAssistantBatch"), 0);
    if (change === "close") assert.equal(h.value.owner, null);
    assert.doesNotMatch(h.value.error, /private identity body/); h.unmount();
  }
});

test("receipt timeout does not accept late success or start a second query automatically", async () => {
  const late = deferred(), h = harness({ prepareAssistantBatch: lostResponse, getAssistantBatchPreparation: () => late.promise }); await freeze(h);
  const reading = h.value.checkOriginal("prepare"); await h.settle(); await h.advance(90000); await reading; await h.settle();
  assert.equal(h.value.unknown.kind, "prepare"); assert.equal(h.value.prepared, null);
  late.resolve(preparation()); await h.settle(); assert.equal(h.value.prepared, null);
  assert.equal(h.count("getAssistantBatchPreparation"), 1); assert.equal(h.count("prepareAssistantBatch"), 1); h.unmount();
});

test("persistent context is minimal typed input with bounded schema, never token, raw prompt, result or arbitrary fields", async () => {
  const h = harness(); await freeze(h); const value = storedRecovery(h);
  assert.equal(api.validateAssistantBatchRecovery(value), value);
  assert.doesNotMatch(JSON.stringify(value), /batch-test-only|result_ref|password|token|prompt/);
  for (const bad of [{ ...value, token: "private" }, { ...value, owner: { ...value.owner, token: "private" } },
    { ...value, expires_at: value.created_at + 86400001 }, { ...value, login_epoch: "short" }, { ...value, schema: "future" },
    { ...value, prepare_payload: { ...value.prepare_payload, steps: [step({ title: "arbitrary prompt" })] } },
    { ...value, bindings: [{ plan_id: pid(), instance_id: 2, revision: 1, binding_hash: hash() }] }]) assert.throws(() => api.validateAssistantBatchRecovery(bad), TypeError);
  h.unmount();
});

for (const broken of ["json", "get", "set", "expired"]) {
  test(`storage ${broken} remains unknown, manual original-key GET is read-only without unlocking`, async () => {
    const first = harness({ prepareAssistantBatch: lostResponse }); await freeze(first); const storage = first.storage;
    const saved = storedRecovery(first); first.unmount();
    if (broken === "json") storage.set(recoveryStorageKey, "{broken");
    if (broken === "expired") storage.set(recoveryStorageKey, JSON.stringify({ ...saved, created_at: Date.now() - 10000, expires_at: Date.now() - 1000 }));
    const h = harness({}, {}, { storage, navigation: 2000, generation: 0, storageFailures: { [broken]: true } }); await h.settle();
    assert.ok(h.value.unknown); assert.equal(h.value.draftLocked, true);
    h.value.setManualPrepareKey(saved.keys.prepare); await h.settle(); await h.value.checkOriginal("prepare"); await h.settle();
    assert.equal(h.value.manual.kind, "prepare"); assert.ok(h.value.unknown); assert.equal(h.value.prepared, null);
    h.value.newDraft(); await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantBatch"), 0);
    assert.equal(h.calls.find((call) => call.name === "getAssistantBatchPreparation").args[0].payload, undefined); h.unmount();
  });
}

test("storage failure before prepare POST retains the original in-memory key and never sends", async () => {
  const failures = {}, h = harness({}, {}, { storageFailures: failures }); await choose(h, [1]); failures.set = true;
  await h.value.mutate("prepare"); await h.settle(); const key = h.value.keyPair.current.prepare;
  assert.equal(h.count("prepareAssistantBatch"), 0); assert.equal(h.value.unknown.kind, "prepare");
  h.value.newDraft(); await h.value.mutate("prepare"); assert.equal(h.value.keyPair.current.prepare, key);
  assert.equal(h.count("prepareAssistantBatch"), 0); h.unmount();
});

test("v2 detail and cancel failures keep unknown and point only to the control view", async () => {
  const failure = Object.assign(new Error("controlled"), { status: 409, code: "batch_control_requires_v2" });
  const h = harness({ getAssistantControlBatch: async () => { throw failure; } }); await h.settle();
  await h.value.readBatch(bid()); await h.settle(); assert.equal(h.value.unknown.kind, "control"); assert.match(h.value.error, /v2 控制视图/);
  h.value.newDraft(); assert.equal(h.value.unknown.kind, "control"); h.unmount();
  const cancel = harness({ cancelAssistantBatch: async () => { throw failure; } }); await cancel.settle();
  await cancel.value.readBatch(bid()); await cancel.settle(); cancel.value.setConfirmCancel(true); await cancel.settle();
  await cancel.value.mutate("cancel"); await cancel.settle(); assert.equal(cancel.value.unknown.kind, "cancel"); assert.match(cancel.value.error, /v2 控制视图/); cancel.unmount();
});

test("original admission receipt cannot erase an unknown cancellation without server confirmation", async () => {
  let result = batch();
  const h = harness({ cancelAssistantBatch: lostResponse, getAssistantBatchAdmission: async () => result }); await freeze(h);
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  h.value.setConfirmCancel(true); await h.settle(); await h.value.mutate("cancel"); await h.settle(); assert.equal(h.value.unknown.kind, "cancel");
  await h.value.checkOriginal("enqueue"); await h.settle(); assert.equal(h.value.unknown.kind, "cancel");
  h.value.setConfirmCancel(true); await h.settle(); await h.value.mutate("cancel"); assert.equal(h.count("cancelAssistantBatch"), 1);
  result = batch({ cancel_requested: true }); await h.value.checkOriginal("enqueue"); await h.settle(); assert.equal(h.value.unknown, null); h.unmount();
});

test("actual receipt validators cannot overwrite a known frozen binding or clear unknown on a mismatched response", async (t) => {
  let response = preparation([preparedItem(1, { revision: 2 })]);
  t.mock.method(shared.api, "request", async () => response);
  const h = harness({ enqueueAssistantBatch: lostResponse, getAssistantBatchPreparation: api.getAssistantBatchPreparation,
    getAssistantBatchAdmission: api.getAssistantBatchAdmission }); await freeze(h);
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  const before = h.storage.get(recoveryStorageKey);
  await h.value.checkOriginal("prepare"); await h.settle(); assert.equal(h.value.unknown.kind, "enqueue");
  assert.equal(h.value.prepared.items[0].revision, 1); assert.equal(h.storage.get(recoveryStorageKey), before);
  response = batch({ items: [member(1, { binding_hash: hash(2) })] }); await h.value.checkOriginal("enqueue"); await h.settle();
  assert.equal(h.value.batch, null); assert.equal(h.value.unknown.kind, "enqueue"); assert.equal(h.storage.get(recoveryStorageKey), before);
  assert.equal(h.count("enqueueAssistantBatch"), 1); h.unmount();
});

test("session generation hides old batches/preparations/history and rejects in-flight read responses", async () => {
  const late = deferred(), h = harness({ getAssistantControlBatch: () => late.promise }); await freeze(h);
  const operation = h.value.readBatch(bid()); await h.settle(); await h.session();
  late.resolve(batch()); await operation; await h.settle(); assert.equal(h.value.batch, null); assert.equal(h.value.prepared, null);
  assert.deepEqual(h.value.targetIds, []); h.unmount();
});

test("transport and UI stay within the assigned read-only capability and integration boundaries", () => {
  const transport = readFileSync(new URL("../src/modules/assistantBatchApi.js", import.meta.url), "utf8");
  assert.doesNotMatch(transport, /\bfetch\s*\(|api\.raw|console\.|setTimeout|\/execute|\/approval/);
  assert.doesNotMatch(source, /Input\.TextArea|dangerouslySetInnerHTML|\bfetch\s*\(|inspection\.run\.execute|approveAssistant/);
  assert.match(source, /<Modal open=\{open\} centered/); assert.match(source, /onOpenPlan\(id\)/);
  assert.match(source, /取消投递/); assert.match(source, /在途步骤未确认停止/);
});
