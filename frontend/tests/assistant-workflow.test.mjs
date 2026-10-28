import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import test from "node:test";
import { parse } from "@babel/parser";
import { createServer } from "vite";

const pid = (n = 1) => `pl${n.toString(16).padStart(26, "0")}`;
const hash = (n = 1) => n.toString(16).padStart(64, "0");
const copy = (value) => JSON.parse(JSON.stringify(value));
const session = { token: "offline-fixture", generation: 1, loginEpoch: "workflow-login-epoch-0001" };
const nullable = (type, rest = {}) => ({ anyOf: [{ type, ...rest }, { type: "null" }], default: null });
const integer = (value, maximum) => ({ type: "integer", minimum: 1, maximum, default: value });
const object = (properties, required) => ({ type: "object", additionalProperties: false, properties, ...(required ? { required } : {}) });
let vite, api, shared, batchApi, componentModule;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  api = await vite.ssrLoadModule("/src/modules/assistantWorkflowApi.js");
  shared = await vite.ssrLoadModule("/src/api.js");
  batchApi = await vite.ssrLoadModule("/src/modules/assistantBatchApi.js");
  componentModule = await vite.ssrLoadModule("/src/AssistantWorkflowWorkspace.jsx");
});
test.after(async () => { await vite?.close(); });
function digest(value) { return createHash("sha256").update(api.workflowCanonical(value)).digest("hex"); }
function definition(id = "inspection.jobs.list") {
  const jobs = object({ page: integer(1, 1000000), page_size: integer(50, 200),
    status: { ...nullable("string", { maxLength: 300 }), "x-csv-values": ["queued", "running", "success", "partial", "failed", "cancelled", "timed_out", "interrupted", "expired"] },
    outcome: { ...nullable("string", { maxLength: 300 }), "x-csv-values": ["success", "partial", "failed", "cancelled", "expired", "unknown"] }, trigger_type: nullable("string", { maxLength: 20 }) });
  const templates = object({ db_type: nullable("string"), enabled: nullable("boolean"), page: integer(1, 1000000), page_size: integer(50, 200) });
  const matrix = object({ engine: nullable("string") });
  const mappings = {
    "inspection.jobs.list": [jobs, ["inspection.run.query"]], "inspection.templates.list": [templates, ["inspection.template.read"]],
    "operations.capabilities.list": [matrix, ["operations.capability_matrix.read"]],
    "inspection.context.read": [object({ matrix, templates, jobs }), ["operations.capability_matrix.read", "inspection.template.read", "inspection.run.query"]],
    "inspection.collect": [object({ policy_id: { type: "integer", exclusiveMinimum: 0 }, timeout_seconds: { type: "integer", minimum: 30, maximum: 3600, default: 900 } }, ["policy_id"]), ["inspection.run.execute"]],
  };
  const [schema, caps] = mappings[id];
  const result = { schema: "capability.workflow/v1", workflow_id: id, workflow_version: 1, title: id, slots_schema: schema,
    scope_modes: ["inspection.collect", "inspection.context.read"].includes(id) ? ["explicit"] : ["global", "explicit"],
    max_targets: 100, failure_policy: "stop", valid_for_seconds: 3600, request_approval: id === "inspection.collect", runtime_checks_required: true,
    release_gate: id === "inspection.collect" ? "inspection_plan" : null,
    steps: caps.map((capability_id, i) => ({ seq: i + 1, capability_id, definition_version: capability_id === "inspection.run.execute" ? 2 : 1,
      slot_group: caps.length > 1 ? ["matrix", "templates", "jobs"][i] : null, depends_on: i ? [i] : [], input_refs: {}, fanout: 1, retries: 0 })) };
  return { ...result, definition_hash: digest(result) };
}
const summary = (n = 1, overrides = {}) => ({ plan_id: pid(n), instance_id: n, revision: 1, binding_hash: hash(n),
  status: "validated", outcome: null, approval_state: "not_required", approval_available: false, expires_at: "2099-10-20T10:00:00Z", reused: false, ...overrides });
const input = (scope = { mode: "explicit", targets: [{ instance_id: 1 }] }) => ({ operation: "prepare", slots: { page: 1, page_size: 50 }, scope });
function receipt(def = definition(), payload = input()) {
  const targets = payload.scope.mode === "global" ? [null] : payload.scope.targets.map((target) => target.instance_id);
  return { schema: "capability.workflow_preparation/v1", accepted: false, status: "prepared", preparation_id: `CP${"a".repeat(28)}`,
    workflow: api.workflowReference(def), operation: payload.operation, source_plan_id: payload.source_plan_id ?? null, total: targets.length,
    items: targets.map((id, i) => summary(i + 1, { instance_id: id })), notice: "Prepared, not executed" };
}
async function planRead(def = definition(), slots = api.workflowDefaults(def.slots_schema), scope = { mode: "explicit", targets: [{ instance_id: 1 }] }, overrides = {}) {
  const params = def.steps.map((step) => step.slot_group ? slots[step.slot_group] : slots);
  return { schema: "capability.plan_read/v1", ...summary(), capability_id: def.steps[0].capability_id, definition_version: def.steps[0].definition_version,
    steps: def.steps.map(({ slot_group, ...step }, i) => ({ ...step, status: "pending", params: params[i] })),
    workflow_binding: { ...api.workflowReference(def), recipe_snapshot: def, input: { slots, scope }, candidate_hash: await api.workflowCandidateHash(def, slots, scope) }, ...overrides };
}
function recovery(overrides = {}) {
  const now = Date.now();
  return { schema: "capability.workflow_recovery/v1", owner: { user_id: 7, session_fingerprint: hash(7) }, login_epoch: session.loginEpoch,
    generation: 1, navigation_epoch: 1000, created_at: now, expires_at: now + 86400000, phase: "prepare", workflow: api.workflowReference(definition()),
    operation: "prepare", source_plan_id: null, target_ids: [1], keys: { prepare: `prepare:${"a".repeat(32)}`, enqueue: null }, preparation_id: null, bindings: null, batch_id: null, ...overrides };
}
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

test("pure catalog/definition/match and atomic prepare use exact shared routes with captured POST session", async (t) => {
  const calls = [], def = definition("inspection.collect"), payload = { operation: "prepare", slots: { policy_id: 7, timeout_seconds: 900 }, scope: { mode: "explicit", targets: [{ instance_id: 1 }] } };
  t.mock.method(shared.api, "request", async (path, options) => {
    calls.push([path, options]);
    if (path.endsWith("/match")) return { schema: "capability.workflow_match/v1", matcher_version: 1, accepted: false, normalized_text: "prepare inspection", decision: "matched", operation: "prepare", workflow: api.workflowReference(def), missing_fields: ["policy_id"], reason_code: "workflow_selected" };
    if (path.endsWith("/prepare") || path.endsWith("/preparation")) return receipt(def, payload);
    return path === "/assistant/workflows" ? { schema: "capability.workflow_catalog/v1", items: [def] } : def;
  });
  await api.listAssistantWorkflows(); await api.getAssistantWorkflow("inspection.collect", 1);
  await api.matchAssistantWorkflow("prepare inspection", { authSession: session });
  await api.prepareAssistantWorkflow(def, payload, { idempotencyKey: "prepare:original", authSession: session });
  await api.getAssistantWorkflowPreparation({ idempotencyKey: "prepare:original" });
  assert.deepEqual(calls.map(([path]) => path), ["/assistant/workflows", "/assistant/workflows/inspection.collect/versions/1", "/assistant/workflows/match", "/assistant/workflows/inspection.collect/versions/1/prepare", "/assistant/workflows/preparation"]);
  for (const index of [2, 3]) assert.equal(calls[index][1].authSession, session);
  assert.equal(calls[3][1].headers["Idempotency-Key"], calls[4][1].headers["Idempotency-Key"]);
  assert.equal(calls[4][1].method, undefined);
});

test("all five actual recipe forms have explicit defaults, no default target/template, typed filters and nested context", () => {
  for (const id of Object.keys(api.WORKFLOW_NAMES)) assert.equal(api.validateWorkflowDefinition(definition(id)).workflow_id, id);
  assert.deepEqual(api.workflowDefaults(definition("inspection.collect").slots_schema), { timeout_seconds: 900 });
  const context = definition("inspection.context.read");
  const defaults = api.workflowDefaults(context.slots_schema);
  assert.equal(defaults.jobs.page_size, 50); assert.equal(defaults.templates.page_size, 50); assert.equal(defaults.matrix.engine, null);
  api.validateWorkflowInput(context, { operation: "prepare", slots: { jobs: { page: 2 } }, scope: { mode: "explicit", targets: [{ instance_id: 1, slots: { jobs: { page_size: 200 } } }] } });
  assert.throws(() => api.validateWorkflowInput(context, { operation: "prepare", slots: { jobs: { page: 2 } }, scope: { mode: "explicit", targets: [{ instance_id: 1, slots: { jobs: { page: 3 } } }] } }), TypeError);
});

test("global has no targets key; invalid IDs, slots, scope, source, keys and sessions reject before I/O", (t) => {
  let calls = 0; t.mock.method(shared.api, "request", () => { calls += 1; });
  for (const id of ["inspection", "unknown.collect", "inspection.collect\n", "inspection.collect/../plans", "inspection.collect%2f"]) assert.throws(() => api.getAssistantWorkflow(id, 1), TypeError);
  const def = definition();
  for (const payload of [input({ mode: "global", targets: null }), input({ mode: "global", targets: [] }), input({ mode: "explicit", targets: [] }),
    input({ mode: "explicit", targets: [{ instance_id: true }] }), input({ mode: "explicit", targets: [{ instance_id: 1 }, { instance_id: 1 }] }),
    { ...input(), slots: { sql: "select 1" } }, { ...input(), slots: { page: "1" } }, { ...input(), slots: { page_size: 201 } },
    { ...input(), slots: { status: "succeeded" } }, { ...input(), source_plan_id: pid() }, { ...input(), operation: "execute" }]) {
    assert.throws(() => api.prepareAssistantWorkflow(def, payload, { idempotencyKey: "prepare:a", authSession: session }), TypeError);
  }
  for (const key of [undefined, "", "bad\n", "bad/key", "x".repeat(129)]) assert.throws(() => api.prepareAssistantWorkflow(def, input(), { idempotencyKey: key, authSession: session }), TypeError);
  assert.throws(() => api.matchAssistantWorkflow("prepare", {}), TypeError);
  assert.throws(() => api.prepareAssistantWorkflow(definition("inspection.collect"), { operation: "prepare", slots: {}, scope: { mode: "global" } }, { idempotencyKey: "p", authSession: session }), TypeError);
  assert.equal(calls, 0);
});

test("strict ten-field receipts correlate global/null, target order, source, workflow, revision and true prepared state", async (t) => {
  let response; t.mock.method(shared.api, "request", async () => response);
  const def = definition(), payload = input({ mode: "global" });
  response = receipt(def, payload); assert.equal((await api.prepareAssistantWorkflow(def, payload, { idempotencyKey: "p", authSession: session })).items[0].instance_id, null);
  const bad = [null, {}, { ...response, accepted: true }, { ...response, status: "queued" }, { ...response, preparation_id: "CPbad" },
    { ...response, workflow: { ...response.workflow, definition_hash: hash(9) } }, { ...response, source_plan_id: pid() },
    { ...response, items: [{ ...response.items[0], instance_id: 1 }] }, { ...response, items: [{ ...response.items[0], status: "settled", outcome: null }] },
    { ...response, items: [{ ...response.items[0], binding_hash: "unsafe" }] }, { ...response, items: [{ ...response.items[0], result_ref: { markdown: "private" } }] }];
  for (response of bad) await assert.rejects(api.prepareAssistantWorkflow(def, payload, { idempotencyKey: "p", authSession: session }), { code: "assistant_workflow_response_invalid" });
});

for (const status of [403, 404, 409, 503]) test(`HTTP ${status} propagates without empty catalog, false receipt or retry`, async (t) => {
  let calls = 0; const error = Object.assign(new Error("private server detail"), { status });
  t.mock.method(shared.api, "request", async () => { calls += 1; throw error; });
  for (const invoke of [() => api.listAssistantWorkflows(), () => api.getAssistantWorkflowPreparation({ idempotencyKey: "original" }),
    () => api.prepareAssistantWorkflow(definition(), input(), { idempotencyKey: "original", authSession: session })]) await assert.rejects(invoke(), (value) => value === error);
  assert.equal(calls, 3);
});

test("complete binding verifies recipe/candidate hashes, fixed actual steps and frozen-only digests before source prefill", async () => {
  const def = definition("inspection.collect"), slots = { policy_id: 7, timeout_seconds: 900 };
  const value = await planRead(def, slots);
  // Frozen fields belong only to the actual step, never the editable binding input.
  value.workflow_binding.input.slots = { ...slots }; value.steps[0].params = { ...slots, policy_revision: hash(2), runtime_digest: hash(3), target_fingerprint: hash(4) };
  assert.equal((await api.validateWorkflowBoundPlan(value)).slots.policy_id, 7);
  for (const mutate of [(v) => { v.workflow_binding.candidate_hash = hash(9); }, (v) => { v.workflow_binding.recipe_snapshot.title = "changed"; },
    (v) => { v.steps[0].params.policy_id = 8; }, (v) => { v.steps[0].params.runtime_digest = "file.sql"; }, (v) => { v.instance_id = 2; },
    (v) => { delete v.workflow_binding; }]) {
    const invalid = copy(value); mutate(invalid); await assert.rejects(api.validateWorkflowBoundPlan(invalid));
  }
});

test("fixed revision posts only CAS/typed slots/scope; lost reply recovery is original-plan GET, never generic revision/execute", async (t) => {
  const def = definition(), original = await planRead(def), source = await api.validateWorkflowBoundPlan(original);
  const changed = { ...api.workflowDefaults(def.slots_schema), page: 2 };
  const next = await planRead(def, changed, source.scope, { revision: 2, binding_hash: hash(22) });
  const calls = [];
  t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]); return path.includes("/versions/") ? def : next; });
  const result = await api.reviseAssistantWorkflow(source, { expected_revision: 1, slots: changed, scope: source.scope }, { authSession: session });
  assert.equal(result.summary.revision, 2);
  assert.deepEqual(Object.keys(JSON.parse(calls[0][1].body)).sort(), ["expected_revision", "scope", "slots"]);
  assert.equal(calls[0][0], `/assistant/workflows/plans/${pid()}/revisions`); assert.equal(calls[0][1].authSession, session);
  await api.getAssistantWorkflowRevision({ plan_id: pid(), expected_revision: 1, candidate_hash: result.candidate_hash });
  assert.equal(calls.filter(([, options]) => options?.method === "POST").length, 1);
  assert.equal(calls[1][0], `/assistant/plans/${pid()}`);
  await assert.rejects(api.getAssistantWorkflowRevision({ plan_id: pid(), expected_revision: 2, candidate_hash: result.candidate_hash }), { code: "assistant_workflow_response_invalid" });
});

test("recovery stores only bounded locators, rejects credentials/slots, and isolates identity/login epoch/navigation", () => {
  const value = recovery(); assert.equal(api.validateWorkflowRecovery(value), value);
  for (const patch of [{ token: "secret" }, { slots: {} }, { owner: { user_id: 7, session_fingerprint: "bad" } }, { expires_at: Date.now() - 1 },
    { target_ids: [null, 1] }, { phase: "enqueue" }]) assert.throws(() => api.validateWorkflowRecovery({ ...value, ...patch }), TypeError);
  assert.equal(api.workflowRecoveryMatches(value, value.owner, session, 1000), true);
  assert.equal(api.workflowRecoveryMatches(value, value.owner, { ...session, generation: 2 }, 1000), false);
  assert.equal(api.workflowRecoveryMatches(value, value.owner, { ...session, generation: 2 }, 2000), true);
  assert.equal(api.workflowRecoveryMatches(value, value.owner, { ...session, loginEpoch: "another-login-epoch-0002" }, 2000), false);
  assert.equal(api.workflowRecoveryMatches(value, { ...value.owner, user_id: 8 }, session, 2000), false);
});

const sourceText = readFileSync(new URL("../src/AssistantWorkflowWorkspace.jsx", import.meta.url), "utf8");
const ast = parse(sourceText, { sourceType: "module", plugins: ["jsx"] });
const component = ast.program.body.find((node) => node.type === "ExportNamedDeclaration" && node.declaration?.id?.name === "AssistantWorkflowWorkspace").declaration;
const statements = component.body.body.filter((node) => node.type !== "ReturnStatement");
const names = statements.filter((node) => node.type === "VariableDeclaration").flatMap((node) => node.declarations.flatMap((d) => d.id.type === "Identifier" ? [d.id.name] : d.id.type === "ArrayPattern" ? d.id.elements.filter(Boolean).map((id) => id.name) : []));
const helpers = ast.program.body.filter((node) => node.type === "VariableDeclaration" || (node.type === "FunctionDeclaration" && node.id.name !== "SlotFields")).map((node) => sourceText.slice(node.start, node.end)).join("\n");
const callbackCode = `${helpers}\nconst ${sourceText.slice(component.params[0].start, component.params[0].end)}=props;\n${statements.map((node) => sourceText.slice(node.start, node.end)).join("\n")}\nreturn {${names.join(",")}};`;
function harness(overrides = {}, props = {}, options = {}) {
  const slots = [], effects = [], calls = [], timers = new Map(), storage = options.storage || new Map();
  let cursor = 0, dirty = true, value, auth = { ...session }, timerId = 0;
  let properties = { open: true, onClose() {}, onOpenPlan() {}, instances: [{ id: 1, db_type: "mysql", host: "fixture" }, { id: 2, db_type: "oracle" }], ...props };
  const fallback = {
    listAssistantWorkflows: async () => ({ schema: "capability.workflow_catalog/v1", items: Object.keys(api.WORKFLOW_NAMES).map((id) => definition(id)) }),
    getAssistantWorkflow: async (id) => definition(id), getAssistantBatchRecoveryIdentity: async () => ({ user_id: 7, session_fingerprint: hash(7) }),
    prepareAssistantWorkflow: async (def, payload) => receipt(def, payload), getAssistantWorkflowPreparation: async () => receipt(),
    getAssistantWorkflowPlan: async (item) => ({ ...item }),
    enqueueAssistantBatch: async (payload) => ({ schema: "capability.batch_read/v1", batch_id: `CB${"b".repeat(28)}`, total: payload.plan_ids.length, requested_by: 7,
      status: "queued", outcome: null, requires_attention: false, items: payload.plan_ids.map((plan_id, i) => ({ plan_id, bound_revision: 1, binding_hash: hash(i + 1), binding_current: true, queue_state: "queued", outcome: null })) }),
    getAssistantBatchAdmission: async () => fallback.enqueueAssistantBatch({ plan_ids: [pid()] }), getAssistantBatch: async () => fallback.enqueueAssistantBatch({ plan_ids: [pid()] }),
    matchAssistantWorkflow: async () => ({ decision: "matched", operation: "prepare", workflow: api.workflowReference(definition()), reason_code: "workflow_selected", missing_fields: [] }),
    getAssistantWorkflowSource: async () => api.validateWorkflowBoundPlan(await planRead()),
    reviseAssistantWorkflow: async (_source, payload) => api.validateWorkflowBoundPlan(await planRead(definition(), payload.slots, payload.scope, { revision: 2, binding_hash: hash(22) })),
    getAssistantWorkflowRevision: async () => api.validateWorkflowBoundPlan(await planRead(definition(), { ...api.workflowDefaults(definition().slots_schema), page: 2 }, undefined, { revision: 2, binding_hash: hash(22) })),
  };
  const wrapped = Object.fromEntries(Object.entries(fallback).map(([name, fn]) => [name, (...args) => { calls.push({ name, args }); return (overrides[name] || fn)(...args); }]));
  const deps = { ...api, ...wrapped, onAuthExpired: () => () => {}, getAuthSession: () => ({ ...auth }), isAuthSessionCurrent: (saved) => saved.token === auth.token && saved.generation === auth.generation && saved.loginEpoch === auth.loginEpoch,
    globalThis: { crypto: globalThis.crypto, performance: { timeOrigin: options.navigation ?? 1000 }, addEventListener() {}, removeEventListener() {}, sessionStorage: {
      getItem: (key) => { if (options.storageGetFails) throw new Error("blocked"); return storage.get(key) ?? null; },
      setItem: (key, text) => { if (options.storageSetFails) throw new Error("blocked"); storage.set(key, text); }, removeItem: (key) => storage.delete(key),
    } },
    useState: (initial) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (update) => { const next = typeof update === "function" ? update(slots[i].value) : update; if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true; } }]; },
    useRef: (initial) => { const i = cursor++; slots[i] ??= { ref: { current: initial } }; return slots[i].ref; },
    useEffect: (fn, dependencies) => { const i = cursor++, old = slots[i]; if (!old || dependencies.some((item, index) => !Object.is(item, old.dependencies[index]))) effects.push(() => { old?.cleanup?.(); slots[i] = { dependencies, cleanup: fn() }; }); },
    setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id),
  };
  const render = new Function(...Object.keys(deps), "props", callbackCode);
  const settle = async () => { for (let n = 0; n < 100; n += 1) { if (dirty) { dirty = false; cursor = 0; value = render(...Object.values(deps), properties); while (effects.length) effects.shift()(); } await new Promise(setImmediate); } assert.equal(dirty, false); };
  return { get value() { return value; }, calls, storage, settle, count: (name) => calls.filter((call) => call.name === name).length,
    update: async (patch) => { properties = { ...properties, ...patch }; dirty = true; await settle(); },
    changeSession: async (renderNow = true) => { auth = { ...auth, generation: auth.generation + 1, loginEpoch: `new-login-epoch-${auth.generation + 1}` }; if (renderNow) { dirty = true; await settle(); } },
    timeout: async () => { for (const fn of [...timers.values()]) fn(); timers.clear(); await settle(); },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}
async function choose(h, id = "inspection.jobs.list", targets = [1]) {
  await h.settle(); await h.value.chooseWorkflow(definition(id)); await h.settle();
  h.value.editDraft(() => { h.value.setScopeMode("explicit"); h.value.setTargetIds(targets); }); await h.settle();
  h.value.setConfirmPrepare(true); await h.settle();
}
async function freeze(h) { await choose(h); await h.value.mutate("prepare"); await h.settle(); }

test("real component transforms; closed view is entirely null and no browser executor or arbitrary JSON editor exists", async () => {
  assert.equal(typeof componentModule.AssistantWorkflowWorkspace, "function");
  assert.ok((await vite.transformRequest("/src/AssistantWorkflowWorkspace.jsx")).code.includes("AssistantWorkflowWorkspace"));
  const h = harness({}, { open: false }); await h.settle(); assert.equal(h.value, null); assert.equal(h.calls.length, 0);
  assert.doesNotMatch(sourceText, /executeAssistantPlan|\/execute["`]|Input\.TextArea|JSON\.parse\(slots|changeAssistantSteps/);
});
test("manual selection/freeze/read statuses/explicit batch admission reuse one server queue, not automatic work", async () => {
  const h = harness(); await choose(h, "inspection.jobs.list", [1, 2]);
  assert.equal(h.count("prepareAssistantWorkflow"), 0); assert.equal(h.value.payload.scope.targets.length, 2);
  await h.value.mutate("prepare"); await h.settle(); assert.equal(h.value.prepared.total, 2); assert.equal(h.value.canEnqueue, false);
  assert.equal(h.count("enqueueAssistantBatch"), 0); await h.value.refreshPlans(); await h.settle();
  h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  assert.equal(h.count("enqueueAssistantBatch"), 1); assert.equal(h.value.batch.status, "queued");
  const post = h.calls.find((call) => call.name === "enqueueAssistantBatch"); assert.deepEqual(post.args[0], { plan_ids: [pid(), pid(2)] });
  assert.deepEqual(post.args[1].authSession, session);
});
test("no default scope/target/template; collect and per-target policies remain typed, no implicit global collect", async () => {
  const h = harness(); await h.settle(); await h.value.chooseWorkflow(definition("inspection.collect")); await h.settle();
  assert.equal(h.value.scopeMode, null); assert.deepEqual(h.value.targetIds, []); assert.equal(h.value.payload, null); assert.equal(h.value.slots.timeout_seconds, 900);
  h.value.editDraft(() => { h.value.setScopeMode("explicit"); h.value.setTargetIds([1, 2]); h.value.setPerTargetPolicy(true); h.value.setPolicies({ 1: 7, 2: 8 }); }); await h.settle();
  assert.deepEqual(h.value.payload.scope.targets[1], { instance_id: 2, slots: { policy_id: 8 } }); assert.equal(h.value.payload.slots.policy_id, undefined);
});
test("match requires an explicit suggestion click and never prepares or admits", async () => {
  const h = harness(); await h.settle(); h.value.setMatchText("prepare jobs"); await h.settle(); await h.value.suggestWorkflow(); await h.settle();
  assert.equal(h.count("matchAssistantWorkflow"), 1); assert.equal(h.value.definition, null); assert.equal(h.count("prepareAssistantWorkflow"), 0); assert.equal(h.count("enqueueAssistantBatch"), 0);
  assert.deepEqual(h.calls.find((call) => call.name === "matchAssistantWorkflow").args[1].authSession, session);
});
test("lost prepare reply locks draft, persists no input/results, and recovers only original-key GET", async () => {
  const h = harness({ prepareAssistantWorkflow: async () => { throw Object.assign(new Error("unavailable"), { status: 503 }); } });
  await choose(h); await h.value.mutate("prepare"); await h.settle();
  assert.equal(h.value.pending, true); const stored = JSON.parse([...h.storage.values()][0]);
  assert.equal(stored.phase, "prepare"); assert.equal(stored.slots, undefined); assert.equal(stored.token, undefined);
  await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantWorkflow"), 1);
  await h.value.recoverOriginal(); await h.settle(); assert.equal(h.value.pending, false); assert.equal(h.count("enqueueAssistantBatch"), 0);
  assert.equal(h.calls.find((call) => call.name === "getAssistantWorkflowPreparation").args[0].idempotencyKey, stored.keys.prepare);
});
test("lost admission reply uses original admission key, no second prepare/admission POST", async () => {
  const h = harness({ enqueueAssistantBatch: async () => { throw new TypeError("network"); } }); await freeze(h);
  await h.value.refreshPlans(); await h.settle(); h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  const stored = JSON.parse([...h.storage.values()][0]); assert.equal(stored.phase, "enqueue");
  await h.value.mutate("enqueue"); await h.value.recoverOriginal(); await h.settle();
  assert.equal(h.count("prepareAssistantWorkflow"), 1); assert.equal(h.count("enqueueAssistantBatch"), 1);
  assert.equal(h.calls.find((call) => call.name === "getAssistantBatchAdmission").args[0].idempotencyKey, stored.keys.enqueue);
});
test("close/reopen and late POST responses cannot publish old UI, reuse old confirmation or autoexecute", async () => {
  const reply = deferred(), h = harness({ prepareAssistantWorkflow: () => reply.promise }); await choose(h);
  const oldMutate = h.value.mutate, inflight = h.value.mutate("prepare"); await h.settle(); h.value.closeWorkspace(); await h.update({ open: false }); assert.equal(h.value, null);
  await h.update({ open: true }); await oldMutate("prepare"); assert.equal(h.count("prepareAssistantWorkflow"), 1);
  reply.resolve(receipt()); await inflight; await h.settle(); assert.equal(h.value.prepared, null); assert.equal(h.value.pending, true);
  await h.value.recoverOriginal(); await h.settle(); assert.equal(h.value.prepared.total, 1); assert.equal(h.value.confirmEnqueue, false);
});
test("same-token login epoch change before React rerender prevents every old confirmed POST", async () => {
  const h = harness(); await choose(h); const old = h.value.mutate;
  await h.changeSession(false); await old("prepare"); assert.equal(h.count("prepareAssistantWorkflow"), 0);
  await h.changeSession(); assert.equal(h.value.prepared, null); assert.equal(h.value.confirmPrepare, false);
});
test("storage failure blocks POST; catalog error stays error, not an empty authorized catalog", async () => {
  const h = harness({}, {}, { storageSetFails: true }); await choose(h); await h.value.mutate("prepare"); await h.settle();
  assert.equal(h.count("prepareAssistantWorkflow"), 0); assert.equal(h.value.pending, true);
  const failed = harness({ listAssistantWorkflows: async () => { throw Object.assign(new Error("denied"), { status: 403 }); } }); await failed.settle();
  assert.equal(failed.value.catalog, null); assert.ok(failed.value.catalogError);
});
test("fixed revise launch preloads verified source without POST and reconciles lost reply only via plan GET", async () => {
  const h = harness({ reviseAssistantWorkflow: async () => { throw Object.assign(new Error("lost"), { status: 503 }); } }, { initialPlan: { plan_id: pid() }, initialAction: "revise" });
  await h.settle(); assert.equal(h.value.source.summary.plan_id, pid()); assert.equal(h.value.slots.page_size, 50); assert.equal(h.count("reviseAssistantWorkflow"), 0);
  h.value.editDraft(() => h.value.setSlots({ ...h.value.slots, page: 2 })); await h.settle(); h.value.setConfirmPrepare(true); await h.settle();
  await h.value.mutate("revise"); await h.settle(); assert.equal(h.value.pending, true); assert.equal(h.count("reviseAssistantWorkflow"), 1);
  const stored = JSON.parse([...h.storage.values()][0]); assert.equal(stored.phase, "revise"); assert.equal(stored.revision.expected_revision, 1); assert.equal(stored.slots, undefined);
  await h.value.recoverOriginal(); await h.settle(); assert.equal(h.value.revised.summary.revision, 2); assert.equal(h.count("reviseAssistantWorkflow"), 1); assert.equal(h.count("prepareAssistantWorkflow"), 0);
});

test("saved revision recovery reads only the original plan after current workflow withdrawal", async (t) => {
  const def = definition(), slots = { ...api.workflowDefaults(def.slots_schema), page: 2 };
  const next = await planRead(def, slots, undefined, { revision: 2, binding_hash: hash(22) });
  const expected = { plan_id: pid(), expected_revision: 1, candidate_hash: next.workflow_binding.candidate_hash }, calls = [];
  t.mock.method(shared.api, "request", async (path, options) => {
    calls.push([path, options]);
    if (path.startsWith("/assistant/workflows")) throw Object.assign(new Error("withdrawn"), { status: 409 });
    return next;
  });
  const result = await api.getAssistantWorkflowRevision(expected, { authSession: session });
  assert.equal(result.summary.revision, 2); assert.deepEqual(calls.map(([path]) => path), [`/assistant/plans/${pid()}`]);
  assert.equal(calls[0][1].authSession, session); assert.equal(calls[0][1].method, undefined);
  await assert.rejects(api.getAssistantWorkflowSource(pid()), { status: 409 });
  const wrong = copy(next); wrong.workflow_binding.candidate_hash = hash(99);
  t.mock.method(shared.api, "request", async () => wrong);
  await assert.rejects(api.getAssistantWorkflowRevision(expected), { code: "assistant_workflow_response_invalid" });
});

test("successful fixed revision requires renewed plan read before original batch admission", async () => {
  const h = harness({ enqueueAssistantBatch: async () => ({ batch_id: `CB${"b".repeat(28)}`, status: "queued",
    items: [{ plan_id: pid(), bound_revision: 2, binding_hash: hash(22), binding_current: true }] }) }, { initialPlan: pid(), initialAction: "revise" });
  await h.settle(); h.value.editDraft(() => h.value.setSlots({ ...h.value.slots, page: 2 })); await h.settle();
  h.value.setConfirmPrepare(true); await h.settle(); await h.value.mutate("revise"); await h.settle();
  assert.equal(h.value.revised.summary.revision, 2); assert.equal(h.value.canEnqueue, false); assert.equal(h.count("enqueueAssistantBatch"), 0);
  await h.value.refreshPlans(); await h.settle(); h.value.setConfirmEnqueue(true); await h.settle(); await h.value.mutate("enqueue"); await h.settle();
  assert.equal(h.count("enqueueAssistantBatch"), 1); assert.equal(h.count("prepareAssistantWorkflow"), 0);
  const stored = JSON.parse([...h.storage.values()][0]); assert.equal(stored.keys.prepare, null); assert.equal(stored.preparation_id, null);
  assert.equal(stored.bindings[0].revision, 2); assert.equal(stored.phase, "enqueue"); assert.equal(h.value.pending, false);
});

test("rerun launch prefills verified same inputs, cannot edit them or POST without fresh confirmation", async () => {
  const original = await planRead(definition(), { ...api.workflowDefaults(definition().slots_schema), page: 7 }, undefined, { status: "settled", outcome: "succeeded" });
  const source = await api.validateWorkflowBoundPlan(original);
  const h = harness({ getAssistantWorkflowSource: async () => source }, { initialPlan: original, initialAction: "rerun" });
  await h.settle(); assert.equal(h.count("prepareAssistantWorkflow"), 0); assert.equal(h.value.sourceLocked, true); assert.equal(h.value.slots.page, 7);
  h.value.editDraft(() => h.value.setSlots({ page: 99 })); await h.settle(); assert.equal(h.value.slots.page, 7);
  await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantWorkflow"), 0);
  h.value.setConfirmPrepare(true); await h.settle(); await h.value.mutate("prepare"); await h.settle();
  const post = h.calls.find((call) => call.name === "prepareAssistantWorkflow");
  assert.equal(post.args[1].operation, "rerun"); assert.equal(post.args[1].source_plan_id, pid());
  assert.deepEqual(post.args[1].slots, source.slots); assert.deepEqual(post.args[1].scope, source.scope); assert.equal(post.args[2].authSession.token, session.token);
});

test("unknown source, settled revision and unverified source cannot be posted as valid source actions", async () => {
  const unknown = await api.validateWorkflowBoundPlan(await planRead(undefined, undefined, undefined, { status: "settled", outcome: "unknown" }));
  const h = harness({ getAssistantWorkflowSource: async () => unknown }, { initialPlan: pid(), initialAction: "rerun" });
  await h.settle(); h.value.setConfirmPrepare(true); await h.settle(); await h.value.mutate("prepare"); assert.equal(h.count("prepareAssistantWorkflow"), 0);
  await h.update({ initialAction: "revise" }); assert.equal(h.value.revisable, false); h.value.setConfirmPrepare(true); await h.settle();
  await h.value.mutate("revise"); assert.equal(h.count("reviseAssistantWorkflow"), 0);
  const bad = harness({ getAssistantWorkflowSource: async () => { throw new Error("bad binding"); } }, { initialPlan: pid(), initialAction: "prepare" });
  await bad.settle(); assert.equal(bad.value.source, null); assert.equal(bad.value.locked, true); await bad.value.mutate("prepare"); assert.equal(bad.count("prepareAssistantWorkflow"), 0);
});

test("lost revision candidate mismatch stays pending; only explicit independent new draft releases source editing", async () => {
  const h = harness({ reviseAssistantWorkflow: async () => { throw new Error("lost"); },
    getAssistantWorkflowRevision: async () => { throw Object.assign(new Error("other revision"), { code: "assistant_workflow_response_invalid" }); } }, { initialPlan: pid(), initialAction: "revise" });
  await h.settle(); h.value.editDraft(() => h.value.setSlots({ ...h.value.slots, page: 2 })); await h.settle(); h.value.setConfirmPrepare(true); await h.settle();
  await h.value.mutate("revise"); await h.settle(); await h.value.recoverOriginal(); await h.settle();
  assert.equal(h.value.pending, true); assert.equal(h.value.revised, null); assert.equal(h.count("reviseAssistantWorkflow"), 1);
  h.value.setConfirmNewDraft(true); await h.settle(); h.value.newDraft(); await h.settle(); assert.equal(h.value.source.summary.plan_id, pid());
  const fresh = harness({}, { initialPlan: pid(), initialAction: "revise" }); await fresh.settle(); fresh.value.setConfirmNewDraft(true); await fresh.settle(); fresh.value.newDraft(); await fresh.settle();
  assert.equal(fresh.value.actionMode, "prepare"); assert.equal(fresh.value.source, null); assert.equal(fresh.value.sourceRequired, false);
  await choose(fresh); await fresh.value.mutate("prepare"); await fresh.settle(); assert.equal(fresh.count("prepareAssistantWorkflow"), 1); assert.equal(fresh.count("reviseAssistantWorkflow"), 0);
});

test("timeout and late revision replies never repost; owner/epoch reload retains only original-key recovery", async () => {
  const reply = deferred(), h = harness({ reviseAssistantWorkflow: () => reply.promise }, { initialPlan: pid(), initialAction: "revise" });
  await h.settle(); h.value.editDraft(() => h.value.setSlots({ ...h.value.slots, page: 2 })); await h.settle(); h.value.setConfirmPrepare(true); await h.settle();
  const inflight = h.value.mutate("revise"); await h.settle(); await h.timeout(); await inflight; await h.settle();
  assert.equal(h.value.pending, true); assert.equal(h.value.revised, null); await h.value.mutate("revise"); assert.equal(h.count("reviseAssistantWorkflow"), 1);
  reply.resolve(await api.validateWorkflowBoundPlan(await planRead(undefined, { ...api.workflowDefaults(definition().slots_schema), page: 2 }, undefined, { revision: 2, binding_hash: hash(22) })));
  await h.settle(); assert.equal(h.value.revised, null);
  const restored = harness({}, {}, { storage: h.storage, navigation: 2000 }); await restored.settle();
  assert.equal(restored.value.pending, true); assert.equal(restored.count("reviseAssistantWorkflow"), 0);
  await restored.value.recoverOriginal(); await restored.settle(); assert.equal(restored.value.revised.summary.revision, 2);
  const other = harness({ getAssistantBatchRecoveryIdentity: async () => ({ user_id: 8, session_fingerprint: hash(8) }) }, {}, { storage: h.storage, navigation: 3000 }); await other.settle();
  assert.equal(other.value.recovery, null); assert.equal(other.storage.size, 0); assert.equal(other.count("getAssistantWorkflowRevision"), 0);
});

test("invalid fields clear a previously valid payload and a parent launch change drops old source and confirmations", async () => {
  const h = harness({}, { initialPlan: pid(), initialAction: "revise" }); await h.settle(); assert.ok(h.value.payload);
  h.value.editDraft(() => h.value.setSlots({ ...h.value.slots, page: "2" })); await h.settle(); assert.equal(h.value.payload, null);
  h.value.setConfirmPrepare(true); await h.settle(); const old = h.value.mutate;
  await h.update({ initialPlan: null, initialAction: "prepare" }); assert.equal(h.value.source, null); assert.equal(h.value.definition, null); assert.equal(h.value.confirmPrepare, false);
  await old("revise"); assert.equal(h.count("reviseAssistantWorkflow"), 0);
});
