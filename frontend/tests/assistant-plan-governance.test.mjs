import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "@babel/parser";
import { createServer } from "vite";
import { assistantPlanApprovalExpired } from "../src/modules/assistantPlanCustomer.js";

const id = `pl${"a".repeat(26)}`;
const utc = "2026-10-20T10:00:00.123456Z";
const reader = "inspection.run.query";
const template = "inspection.template.read";
const definition = (capability_id = reader) => ({ capability_id, title: capability_id, kind: "read", version: 1, definition_version: 1,
  definition_hash: capability_id, plan_executable: true, execution: "server", availability: "available", authorized_engines: ["mysql"] });
const inspectionEffect = () => ({ capability_id: "inspection.run.execute", title: "inspection.run.execute", kind: "execute",
  version: 2, definition_version: 2, definition_hash: "inspection-run-v2", plan_executable: true,
  execution: "server", handler_status: "wired", availability: "available", requires_instance: true,
  authorized_engines: ["mysql"], output_schema: "inspection.plan_result/v1" });
const step = (overrides = {}) => ({ seq: 1, title: "Read", capability_id: reader, definition_version: 1,
  params: { page: 1, page_size: 20 }, depends_on: [], input_refs: {}, status: "pending", ...overrides });
const saved = (overrides = {}) => ({ schema: "capability.plan_read/v1", plan_id: id, capability_id: reader,
  definition_version: 1, instance_id: 1, requested_by: 7, status: "validated", outcome: null, revision: 3,
  binding_hash: "a".repeat(64), expires_at: utc, approval_available: true, approval_state: "pending",
  approved_by: null, approval_expires_at: null, steps: [step()], ...overrides });
const approvalRead = (plan = saved()) => ({ schema: "capability.plan_approval/v1", plan, required_approvals: 1, decisions: [] });
const input = (overrides = {}) => ({ capability_id: reader, definition_version: 1, instance_id: 1,
  steps: [step()], valid_for_seconds: 3600, request_approval: true, ...overrides });
const decision = { expected_revision: 3 };
let vite, api, shared, state;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent",
    server: { middlewareMode: true, hmr: false } });
  api = await vite.ssrLoadModule("/src/modules/assistantApi.js");
  shared = await vite.ssrLoadModule("/src/api.js");
  state = await vite.ssrLoadModule("/src/modules/assistantPlanState.js");
});
test.after(async () => { await vite?.close(); });

test("validation accepts approved/current revisions while retaining the exact nine-field strict contract", async (t) => {
  let response = { schema: "capability.plan_validation/v1", plan_id: id, revision: 3, checked_at: utc,
    plan_status: "approved", can_execute: true, issues: [], approval_available: true, requires_recheck: true };
  t.mock.method(shared.api, "request", async () => response);
  assert.equal(await api.getAssistantPlanValidation(id), response);
  for (const patch of [{ revision: 0 }, { revision: 1.5 }, { revision: "3" }, { revision: Number.MAX_SAFE_INTEGER + 1 },
    { approval_available: 1 }, { can_execute: true, plan_status: "awaiting_approval" }, { requires_recheck: false },
    { issues: [{ code: "private=body" }] }, { unexpected: true }]) {
    const valid = response;
    response = { ...valid, ...patch };
    await assert.rejects(api.getAssistantPlanValidation(id), { code: "assistant_response_invalid" });
    response = valid;
  }
});

test("governance uses only explicit shared routes and frozen revision/hash bodies", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (path, options) => {
    calls.push([path, options]);
    return path.endsWith("/approval") ? approvalRead() : saved({ revision: path.endsWith("/revisions") ? 4 : 3 });
  });
  await api.getAssistantPlanApproval(id);
  await api.reviseAssistantPlan(id, input({ steps: [(({ status, ...value }) => value)(step())], ...decision }));
  await api.requestAssistantPlanApproval(id, decision);
  const approve = { ...decision, expected_binding_hash: "a".repeat(64), valid_for_seconds: 900 };
  await api.approveAssistantPlan(id, approve);
  await api.revokeAssistantPlanApproval(id, decision);
  assert.deepEqual(calls.map(([path]) => path), [`/assistant/plans/${id}/approval`, `/assistant/plans/${id}/revisions`,
    `/assistant/plans/${id}/approval/request`, `/assistant/plans/${id}/approval/approve`, `/assistant/plans/${id}/approval/revoke`]);
  assert.deepEqual(calls[0][1], {});
  assert.deepEqual(JSON.parse(calls[3][1].body), approve);
  assert.ok(calls.slice(1).every(([, options]) => options.method === "POST"));
  assert.equal(calls.length, 5, "No implicit execution or follow-up requests in transport");
});

test("invalid IDs, forged identities, TTLs and binding hashes never reach I/O", async (t) => {
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const value of [`${id}\n`, `${id}/execute`, `PL${"a".repeat(26)}`, "../plan", `${id}?actor=admin`]) {
    for (const invoke of [() => api.getAssistantPlanApproval(value), () => api.requestAssistantPlanApproval(value, decision),
      () => api.reviseAssistantPlan(value, input(decision)), () => api.approveAssistantPlan(value, decision),
      () => api.revokeAssistantPlanApproval(value, decision)]) assert.throws(invoke, TypeError);
  }
  for (const payload of [{}, { expected_revision: "3" }, { expected_revision: 0 }, { ...decision, actor_id: 8 }]) {
    assert.throws(() => api.requestAssistantPlanApproval(id, payload), TypeError);
    assert.throws(() => api.revokeAssistantPlanApproval(id, payload), TypeError);
  }
  const valid = { ...decision, expected_binding_hash: "a".repeat(64) };
  for (const patch of [{ expected_binding_hash: "A".repeat(64) }, { expected_binding_hash: `${"a".repeat(64)}\n` },
    { valid_for_seconds: 59 }, { valid_for_seconds: 3601 }, { valid_for_seconds: "900" }, { approved_by: 8 }]) {
    assert.throws(() => api.approveAssistantPlan(id, { ...valid, ...patch }), TypeError);
  }
  assert.equal(calls, 0);
});

test("plan input is a bounded static reader graph, not a JSON/SQL/command or field-expression executor", async (t) => {
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; });
  const clean = ({ status, ...value }) => value;
  const valid = input({ steps: [clean(step())] });
  const badSteps = [[], Array.from({ length: 21 }, (_, i) => clean(step({ seq: i + 1 }))),
    [clean(step({ capability_id: "inspection.run.execute" }))], [clean(step({ definition_version: 0 }))], [clean(step({ definition_version: 1.5 }))],
    [clean(step({ params: { sql: "select 1" } }))], [clean(step({ params: { page: "1+1" } }))],
    [clean(step({ depends_on: [1] }))], [clean(step({ input_refs: { page: "steps[1].id" } }))],
    [clean(step({ fanout: 2 }))], [clean(step({ retries: 1 }))], [clean(step({ url: "https://example.invalid" }))]];
  for (const patch of [...badSteps.map((steps) => ({ steps })), { valid_for_seconds: 59 }, { valid_for_seconds: 86401 },
    { request_approval: "true" }, { capability_id: template }, { actor_id: 8 }, { params: { command: "uname" } }]) {
    await assert.rejects(api.createAssistantPlan({ ...valid, ...patch }), TypeError);
    assert.throws(() => api.reviseAssistantPlan(id, { ...valid, ...decision, ...patch }), TypeError);
  }
  assert.equal(calls, 0);
});

test("approval reads and mutations reject malformed envelopes, wrong plans, revisions and hashes", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (response of [null, "<html>private</html>", {}, approvalRead(saved({ plan_id: `pl${"b".repeat(26)}` })),
    approvalRead(saved({ revision: 0 })), approvalRead(saved({ binding_hash: "g".repeat(64) })),
    approvalRead(saved({ expires_at: "2026-02-30T00:00:00Z" })), approvalRead(saved({ approved_by: "8" })),
    approvalRead(saved({ approval_available: "true" })), approvalRead(saved({ steps: [] })),
    approvalRead(saved({ steps: [step({ seq: 2 })] })), { ...approvalRead(), required_approvals: "1" },
    { ...approvalRead(), decisions: [{ actor_id: 8, approved_at: utc, expires_at: "local" }] },
    { ...approvalRead(), decisions: [{ actor_id: 8, approved_at: utc, expires_at: utc, secret: "private" }] },
    { ...approvalRead(), decisions: Array(21).fill({ actor_id: 8, approved_at: utc, expires_at: utc }) }]) {
    await assert.rejects(api.getAssistantPlanApproval(id), { code: "assistant_response_invalid" });
  }
  for (response of [saved({ revision: 2 }), saved({ binding_hash: "b".repeat(64) })]) {
    await assert.rejects(api.approveAssistantPlan(id, { ...decision, expected_binding_hash: "a".repeat(64) }), { code: "assistant_response_invalid" });
  }
  response = saved({ revision: 3 });
  assert.throws(() => api.reviseAssistantPlan(id, input(decision)), TypeError, "Status is not an input field");
  await assert.rejects(api.reviseAssistantPlan(id, input({ ...decision, steps: [(({ status, ...value }) => value)(step())] })), { code: "assistant_response_invalid" });
});

for (const status of [401, 403, 409, 422, 503, 504]) {
  test(`governance propagates HTTP ${status} without retry, fallback or automatic approval`, async (t) => {
    const failure = Object.assign(new Error("denied"), { status });
    let calls = 0;
    t.mock.method(shared.api, "request", async () => { calls += 1; throw failure; });
    for (const invoke of [() => api.getAssistantPlanApproval(id), () => api.requestAssistantPlanApproval(id, decision),
      () => api.approveAssistantPlan(id, { ...decision, expected_binding_hash: "a".repeat(64) }),
      () => api.revokeAssistantPlanApproval(id, decision)]) await assert.rejects(invoke(), (error) => error === failure);
    assert.equal(calls, 4);
  });
}

test("stable step editing respects count, prior-only edges, deletion and primary step reordering", () => {
  const original = [{ key: "a", capability_id: reader, params: { page: 1 }, depends_on: [] },
    { key: "b", capability_id: template, params: {}, depends_on: [] }];
  const before = structuredClone(original);
  const moved = state.changeAssistantSteps(original, { type: "move", key: "b", direction: -1 });
  assert.equal(moved[0].capability_id, template);
  assert.deepEqual(original, before);
  original[1].depends_on = ["a"];
  assert.throws(() => state.changeAssistantSteps(original, { type: "move", key: "b", direction: -1 }), /依赖/);
  const removed = state.changeAssistantSteps(original, { type: "remove", key: "a" });
  assert.deepEqual(removed.map((item) => item.depends_on), [[]]);
  assert.throws(() => state.changeAssistantSteps(removed, { type: "remove", key: "b" }), TypeError);
  let steps = before;
  for (let i = 2; i < 20; i += 1) steps = state.changeAssistantSteps(steps, { type: "add", step: { key: `${i}`, capability_id: reader, params: {} } });
  assert.equal(steps.length, 20);
  assert.throws(() => state.changeAssistantSteps(steps, { type: "add", step: { key: "overflow", params: {} } }), TypeError);
});

test("frozen-to-editor conversion refuses unsupported versions, arbitrary params and field expressions", () => {
  assert.equal(state.assistantPlanDraft(saved())[0].key, "saved-1");
  for (const patch of [{ definition_version: 2 }, { capability_id: "inspection.run.execute" }, { params: { sql: "select 1" } },
    { params: { page: "${steps[1].id}" } }, { input_refs: { page: "steps[1].id" } }, { depends_on: [1] }]) {
    assert.throws(() => state.assistantPlanDraft(saved({ steps: [step(patch)] })), TypeError);
  }
});

// Execute production callback bodies with deterministic hooks/timers. This is not DOM/browser coverage.
const source = readFileSync(new URL("../src/AssistantPlanWorkspace.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const component = ast.program.body.find((node) => node.declaration?.id?.name === "AssistantPlanWorkspace").declaration;
const statements = component.body.body.filter((node) => node.type !== "ReturnStatement"
  && !(node.type === "VariableDeclaration" && node.declarations.some((value) => value.id.name === "historyColumns")));
const names = statements.filter((node) => node.type === "VariableDeclaration").flatMap((node) => node.declarations.flatMap((value) =>
  value.id.type === "ArrayPattern" ? value.id.elements.filter(Boolean).map((item) => item.name) : [value.id.name]));
const helpers = ast.program.body.filter((node) => node.type === "VariableDeclaration"
  || (node.type === "FunctionDeclaration" && node.id.name !== "InlineResult")).map((node) => source.slice(node.start, node.end)).join("\n");
const callbackBody = `${helpers}\nconst ${source.slice(component.params[0].start, component.params[0].end)} = props;
  ${statements.map((node) => source.slice(node.start, node.end)).join("\n")}\nreturn {${names.join(",")}};`;
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function harness(overrides = {}, props = {}) {
  const slots = [], calls = [], timers = new Map();
  let cursor = 0, dirty = true, now = 0, sequence = 0, snapshot, generation = 1;
  let properties = { open: true, onClose() {}, instances: [{ id: 1, db_type: "mysql" }], instanceId: 1, request: { capability: definition(), instanceId: 1 }, ...props };
  const effects = [];
  const defaults = {
    listAssistantCapabilities: async () => ({ items: [definition(), definition(template), definition("operations.capability_matrix.read")] }),
    listAssistantPlans: async () => ({ items: [], total: 0, page: 1 }), getAssistantCapability: async (capability) => definition(capability),
    createAssistantPlan: async () => saved(), getAssistantPlan: async () => saved(), getAssistantPlanApproval: async () => approvalRead(),
    reviseAssistantPlan: async () => saved({ revision: 4 }), requestAssistantPlanApproval: async () => saved({ status: "awaiting_approval" }),
    approveAssistantPlan: async () => saved({ status: "approved", approval_state: "approved" }),
    revokeAssistantPlanApproval: async () => saved({ approval_state: "revoked" }), executeAssistantPlan: async () => saved({ status: "running" }),
    getAssistantPlanValidation: async () => ({ can_execute: true }), currentUser: async () => ({ id: 7 }),
  };
  const wrapped = Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name, (...args) => { calls.push({ name, args }); return (overrides[name] || fallback)(...args); }]));
  const dependencies = { ...wrapped, ...state, assistantPlanApprovalExpired, authApi: { currentUser: wrapped.currentUser },
    onAuthExpired: () => () => {},
    getAuthSession: () => ({ token: "test-only", generation }), isAuthSessionCurrent: (session) => session.generation === generation,
    setTimeout: (callback, delay) => { const timer = ++sequence; timers.set(timer, { callback, at: now + delay }); return timer; },
    clearTimeout: (timer) => timers.delete(timer), crypto: { randomUUID: () => `draft-${++sequence}` },
    useState: (initial) => { const i = cursor++; slots[i] ??= { value: initial }; return [slots[i].value, (value) => {
      const next = typeof value === "function" ? value(slots[i].value) : value;
      if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true; }
    }]; },
    useRef: (initial) => { const i = cursor++; slots[i] ??= { ref: { current: initial } }; return slots[i].ref; },
    useEffect: (callback, deps) => { const i = cursor++, previous = slots[i];
      if (!previous || deps.some((dep, index) => !Object.is(dep, previous.deps[index]))) effects.push(() => { previous?.cleanup?.(); slots[i] = { deps, cleanup: callback() }; });
    },
  };
  const render = new Function(...Object.keys(dependencies), "props", callbackBody);
  const settle = async () => {
    for (let pass = 0; pass < 30; pass += 1) {
      if (dirty) { dirty = false; cursor = 0; snapshot = render(...Object.values(dependencies), properties); while (effects.length) effects.shift()(); }
      await Promise.resolve();
    }
    assert.equal(dirty, false, "Hooks settle");
  };
  return { get value() { return snapshot; }, calls, count: (name) => calls.filter((call) => call.name === name).length, settle,
    update: async (patch) => { properties = { ...properties, ...patch }; dirty = true; await settle(); },
    session: async () => { generation += 1; dirty = true; await settle(); },
    sessionBeforeRender: () => { generation += 1; },
    advance: async (duration) => { const target = now + duration;
      for (;;) { const next = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break; timers.delete(next[0]); now = next[1].at; next[1].callback(); await settle(); }
      now = target; await settle();
    },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test("explicit save freezes a typed multi-step graph and reads it back without execution or approval", async () => {
  const h = harness(); await h.settle();
  assert.equal(h.count("createAssistantPlan"), 0);
  h.value.changeSteps({ type: "add", step: { key: "second", capability_id: template, params: { page: 2, page_size: 30 } } });
  await h.settle();
  h.value.setDraftSteps([{ ...h.value.draftSteps[0], depends_on: [h.value.firstKey] }]);
  h.value.setRequestApproval(true); h.value.setValidForSeconds(120); await h.settle();
  await h.value.saveDraft(); await h.settle();
  const payload = h.calls.find((call) => call.name === "createAssistantPlan").args[0];
  assert.equal(payload.steps.length, 2); assert.equal(payload.steps[0].capability_id, payload.capability_id);
  assert.deepEqual(payload.steps[1].depends_on, [1]); assert.deepEqual(payload.steps[1].params, { page: 2, page_size: 30 });
  assert.equal(payload.instance_id, 1); assert.equal(payload.valid_for_seconds, 120); assert.equal(payload.request_approval, true);
  assert.equal(h.count("getAssistantPlan"), 1); assert.equal(h.count("executeAssistantPlan"), 0);
  assert.equal(h.count("requestAssistantPlanApproval"), 0); assert.equal(h.count("approveAssistantPlan"), 0);
  h.unmount();
});

for (const action of ["save", "run"]) {
  test(`old ${action} callback cannot create a plan using a newly sampled session before rerender`, async () => {
    const h = harness(); await h.settle(); assert.equal(h.value.ready, true);
    h.sessionBeforeRender();
    await (action === "save" ? h.value.saveDraft() : h.value.run());
    assert.equal(h.count("createAssistantPlan"), 0); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
  });
}

test("approval confirmation is bound to the rendered identity, not the identity at click time", async () => {
  const h = harness({ currentUser: async () => ({ id: 8 }), getAssistantPlanApproval: async () => approvalRead(saved({ status: "awaiting_approval" })) });
  await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
  assert.equal(h.value.approvalMode, true); assert.equal(h.value.actorId, 8);
  h.sessionBeforeRender(); await h.value.govern("approve");
  assert.equal(h.count("approveAssistantPlan"), 0); h.unmount();
});

test("revision save binds expected revision and verifies the new revision via GET, not autoexecution", async () => {
  const h = harness({ getAssistantPlan: async () => saved({ revision: 4 }) }); await h.settle();
  h.value.publish(saved()); await h.settle(); h.value.beginEdit(true); await h.settle();
  assert.equal(h.value.revisionBase.revision, 3);
  await h.value.saveDraft(); await h.settle();
  assert.equal(h.calls.find((call) => call.name === "reviseAssistantPlan").args[1].expected_revision, 3);
  assert.equal(h.value.plan.revision, 4); assert.equal(h.value.uncertain, false); assert.equal(h.value.revisionBase, null);
  assert.equal(h.count("createAssistantPlan"), 0); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("post-save readback failure or different revision stays pending reconciliation", async () => {
  for (const getAssistantPlan of [async () => saved({ revision: 5 }), async () => { throw Object.assign(new Error("read denied"), { status: 403 }); }]) {
    const h = harness({ getAssistantPlan }); await h.settle();
    await h.value.saveDraft(); await h.settle(); assert.equal(h.value.uncertain, true);
    await h.value.saveDraft(); await h.value.run(h.value.plan); await h.value.govern("request");
    assert.equal(h.count("createAssistantPlan"), 1); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
  }
});

test("approver uses dedicated approval reads, explicit decision binding, and cannot impersonate owner execution", async () => {
  let plan = saved({ status: "awaiting_approval" });
  const h = harness({ currentUser: async () => ({ id: 8 }), getAssistantPlanApproval: async () => approvalRead(plan),
    approveAssistantPlan: async () => { plan = saved({ status: "approved", approval_state: "approved" }); return plan; } });
  await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
  assert.equal(h.count("getAssistantPlan"), 0); assert.equal(h.count("approveAssistantPlan"), 0);
  await h.value.run(h.value.plan); await h.value.govern("request"); h.value.beginEdit(true);
  await h.value.validatePlan(); assert.equal(h.count("getAssistantPlanValidation"), 0);
  assert.equal(h.count("executeAssistantPlan"), 0); assert.equal(h.value.revisionBase, null);
  await h.value.govern("approve"); await h.settle();
  assert.deepEqual(h.calls.find((call) => call.name === "approveAssistantPlan").args, [id, { ...decision, expected_binding_hash: "a".repeat(64), valid_for_seconds: 900 },
    { authSession: { token: "test-only", generation: 1 } }]);
  assert.equal(h.count("getAssistantPlanApproval"), 2); assert.equal(h.count("getAssistantPlan"), 0);
  assert.equal(h.value.plan.status, "approved"); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("owner cannot self-approve; request and revoke are explicit separate commands", async () => {
  const h = harness(); await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
  await h.value.govern("approve"); assert.equal(h.count("approveAssistantPlan"), 0);
  await h.value.readPlan(id); await h.settle();
  await h.value.govern("request"); await h.settle(); await h.value.govern("revoke"); await h.settle();
  assert.equal(h.count("requestAssistantPlanApproval"), 1); assert.equal(h.count("revokeAssistantPlanApproval"), 1);
  assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("fixed workflow edits and reruns open the typed recipe workspace without generic plan writes", async () => {
  const original = saved({ workflow_binding: { workflow_id: "inspection.jobs.list", workflow_version: 1 } });
  const h = harness(); await h.settle(); h.value.publish(original); await h.settle();
  h.value.beginEdit(true); await h.settle();
  assert.equal(h.value.workflowOpen, true); assert.equal(h.value.workflowAction, "revise");
  assert.equal(h.value.workflowSource, original); assert.equal(h.value.revisionBase, null);
  assert.equal(h.count("reviseAssistantPlan"), 0); assert.equal(h.count("createAssistantPlan"), 0);
  const completed = { ...original, status: "settled", outcome: "failed", steps: [step({ status: "failed" })] };
  h.value.publish(completed); await h.settle(); h.value.beginEdit(false); await h.settle();
  assert.equal(h.value.workflowAction, "rerun"); assert.equal(h.value.workflowSource, completed);
  h.value.closeWorkspace(); await h.settle(); assert.equal(h.value.workflowOpen, false);
  assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("session change immediately hides old cached products and a denied fresh read cannot restore them", async () => {
  let denied = false;
  const h = harness({
    getAssistantPlan: async () => { if (denied) throw Object.assign(new Error("denied"), { status: 403 }); return saved(); },
    listAssistantPlans: async () => { if (denied) throw Object.assign(new Error("denied"), { status: 403 }); return { items: [], total: 0, page: 1 }; },
    listAssistantCapabilities: async () => { if (denied) throw Object.assign(new Error("denied"), { status: 403 }); return { items: [definition()] }; },
  }); await h.settle();
  h.value.publish(saved()); h.value.setHistory({ items: [{ plan_id: id }], total: 1, page: 1 });
  h.value.setResultStep(step()); await h.settle();
  assert.equal(h.value.plan.plan_id, id); assert.equal(h.value.history.total, 1); assert.ok(h.value.resultStep);
  denied = true; await h.session(); await h.value.readPlan(id); await h.settle();
  assert.equal(h.value.plan, null); assert.equal(h.value.resultStep, null);
  assert.equal(h.value.history.total, 0); assert.equal(h.value.revisionBase, null);
  assert.equal(h.value.workflowSource, null); assert.equal(h.value.approval, null);
  assert.equal(h.value.capabilities.length, 0); assert.ok(h.value.historyError); assert.ok(h.value.actionError);
  assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("a late mutation from the old session cannot clear the new session's in-flight lock", async () => {
  const first = deferred(), second = deferred(); let submissions = 0;
  const h = harness({ createAssistantPlan: () => (++submissions === 1 ? first.promise : second.promise) });
  await h.settle(); const oldOperation = h.value.saveDraft(); await h.settle();
  assert.equal(h.count("createAssistantPlan"), 1); assert.equal(h.value.busy, true);
  await h.session(); const newOperation = h.value.saveDraft(); await h.settle();
  assert.equal(h.count("createAssistantPlan"), 2); assert.equal(h.value.busy, true);
  first.resolve(saved()); await oldOperation; await h.settle();
  assert.equal(h.value.plan, null); assert.equal(h.value.busy, true);
  await h.value.saveDraft(); assert.equal(h.count("createAssistantPlan"), 2);
  second.resolve(saved()); await newOperation; await h.settle();
  assert.equal(h.value.busy, false); assert.equal(h.value.plan.plan_id, id);
  assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("frozen inspection v2 may be independently approved but never directly executed in the reader editor", async () => {
  let plan = saved({ capability_id: "inspection.run.execute", definition_version: 2, status: "awaiting_approval",
    db_type: "mysql",
    steps: [step({ capability_id: "inspection.run.execute", definition_version: 2, params: { policy_id: 9 } })] });
  const h = harness({ currentUser: async () => ({ id: 8 }), listAssistantCapabilities: async () => ({ items: [inspectionEffect()] }),
    getAssistantPlanApproval: async () => approvalRead(plan),
    approveAssistantPlan: async () => { plan = { ...plan, status: "approved", approval_state: "approved" }; return plan; } });
  await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
  await h.value.govern("approve"); await h.settle();
  assert.equal(h.count("approveAssistantPlan"), 1); assert.equal(h.count("executeAssistantPlan"), 0);
  await h.value.run(plan); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("approved owner can execute the frozen wired readers, but approval mode and unknown results never can", async () => {
  const plan = saved({ status: "approved", approval_state: "approved" });
  const h = harness(); await h.settle(); h.value.publish(plan); await h.settle();
  await h.value.run(plan); await h.settle(); assert.equal(h.count("executeAssistantPlan"), 1);
  const unknown = saved({ status: "settled", outcome: "unknown" }); h.value.publish(unknown); await h.settle();
  h.value.beginEdit(true); await h.value.saveDraft(); await h.value.run(unknown);
  assert.equal(h.count("reviseAssistantPlan"), 0); assert.equal(h.count("createAssistantPlan"), 0);
  assert.equal(h.count("executeAssistantPlan"), 1); h.unmount();
});

for (const change of ["close", "request", "instance", "session", "unmount"]) {
  test(`${change} invalidates late save publication and never triggers approval/execute`, async () => {
    const late = deferred(), h = harness({ createAssistantPlan: () => late.promise }); await h.settle();
    const operation = h.value.saveDraft(); await h.settle();
    assert.equal(h.count("createAssistantPlan"), 1);
    if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true }); }
    if (change === "request") await h.update({ request: { capability: definition(template), instanceId: 1 } });
    if (change === "instance") await h.update({ instanceId: 2 });
    if (change === "session") await h.session();
    if (change === "unmount") h.unmount();
    late.resolve(saved()); await operation; await h.settle();
    assert.equal(h.value.plan, null); assert.equal(h.count("executeAssistantPlan"), 0); assert.equal(h.count("approveAssistantPlan"), 0); h.unmount();
  });
}

test("save timeout and duplicate clicks remain uncertain without retry or late success", async () => {
  const late = deferred(), h = harness({ createAssistantPlan: () => late.promise }); await h.settle();
  const operation = h.value.saveDraft(); await h.settle(); await h.value.saveDraft(); await h.value.govern("approve");
  assert.equal(h.count("createAssistantPlan"), 1);
  await h.advance(90000); await operation; await h.settle(); assert.equal(h.value.uncertain, true);
  late.resolve(saved()); await h.settle(); assert.equal(h.value.plan, null);
  await h.value.saveDraft(); assert.equal(h.count("createAssistantPlan"), 1); h.unmount();
});

for (const change of ["close", "request", "instance", "session", "unmount"]) {
  test(`${change} discards a late approval response and cannot reuse an old approval snapshot`, async () => {
    const late = deferred();
    const h = harness({ currentUser: async () => ({ id: 8 }), getAssistantPlanApproval: async () => approvalRead(saved({ status: "awaiting_approval" })),
      approveAssistantPlan: () => late.promise });
    await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
    const operation = h.value.govern("approve"); await h.settle();
    await h.value.govern("approve"); assert.equal(h.count("approveAssistantPlan"), 1);
    if (change === "close") { h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true }); }
    if (change === "request") await h.update({ request: { capability: definition(template), instanceId: 1 } });
    if (change === "instance") await h.update({ instanceId: 2 });
    if (change === "session") await h.session();
    if (change === "unmount") h.unmount();
    late.resolve(saved({ status: "approved", approval_state: "approved" })); await operation; await h.settle();
    if (change === "session") assert.equal(h.value.plan, null);
    else assert.equal(h.value.plan.status, "awaiting_approval");
    assert.equal(h.count("getAssistantPlanApproval"), 1);
    assert.equal(h.count("executeAssistantPlan"), 0);
    if (change !== "unmount") assert.equal(h.value.busy, false);
    h.unmount();
  });
}

test("approval timeout stays unknown and a fresh read is required before another decision", async () => {
  const late = deferred();
  const h = harness({ currentUser: async () => ({ id: 8 }), getAssistantPlanApproval: async () => approvalRead(saved({ status: "awaiting_approval" })),
    approveAssistantPlan: () => late.promise });
  await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
  const operation = h.value.govern("approve"); await h.settle(); await h.advance(90000); await operation; await h.settle();
  assert.equal(h.value.uncertain, true); await h.value.govern("approve"); assert.equal(h.count("approveAssistantPlan"), 1);
  late.resolve(saved({ status: "approved", approval_state: "approved" })); await h.settle(); assert.equal(h.value.plan.status, "awaiting_approval");
  await h.value.readPlan(id, { forApproval: true }); await h.settle(); assert.equal(h.value.uncertain, false); h.unmount();
});

test("closing during explicit creation never automatically executes the late saved plan", async () => {
  const late = deferred(), h = harness({ createAssistantPlan: () => late.promise }); await h.settle();
  const operation = h.value.run(); await h.settle(); assert.equal(h.count("createAssistantPlan"), 1);
  h.value.closeWorkspace(); await h.update({ open: false }); await h.update({ open: true });
  late.resolve(saved({ approval_state: "not_required", approval_available: false })); await operation; await h.settle();
  assert.equal(h.count("executeAssistantPlan"), 0); assert.equal(h.value.plan, null); assert.equal(h.value.uncertain, true); h.unmount();
});

test("unsupported approval definitions and unavailable approval never acquire a client-side decision path", async () => {
  for (const plan of [saved({ approval_available: false }), saved({ steps: [step({ capability_id: "inspection.run.execute" })] })]) {
    const h = harness({ currentUser: async () => ({ id: 8 }), getAssistantPlanApproval: async () => approvalRead(plan) });
    await h.settle(); await h.value.readPlan(id, { forApproval: true }); await h.settle();
    await h.value.govern("approve"); assert.equal(h.count("approveAssistantPlan"), 0); h.unmount();
  }
});

test("a revoked approval requires revision and cannot be requested again from the frozen plan", async () => {
  const h = harness(); await h.settle();
  h.value.publish(saved({ status: "validated", approval_state: "revoked" })); await h.settle();
  await h.value.govern("request");
  assert.equal(h.count("requestAssistantPlanApproval"), 0);
  assert.match(source, /plan\.status === "validated" && plan\.approval_state !== "revoked"/);
  h.unmount();
});

test("an approved plan past its persisted approval deadline cannot execute", async () => {
  const expired = saved({ status: "approved", approval_state: "approved",
    approval_expires_at: "2000-01-01T00:00:00Z" });
  const h = harness(); await h.settle();
  h.value.publish(expired); await h.settle();
  await h.value.executeSaved(expired, h.value.authSession);
  await h.value.run(expired); await h.settle();
  assert.equal(h.count("executeAssistantPlan"), 0);
  assert.match(source, /!assistantPlanApprovalExpired\(plan\)/);
  h.unmount();
});

test("running and unknown plans cannot be edited or retried; executed plans use a new explicit idempotency key", async () => {
  const h = harness(); await h.settle();
  for (const plan of [saved({ status: "running" }), saved({ status: "settled", outcome: "unknown" })]) {
    h.value.publish(plan); await h.settle(); h.value.beginEdit(true); h.value.beginEdit(false);
    await h.value.saveDraft(); await h.value.run(plan); assert.equal(h.value.revisionBase, null);
  }
  h.value.publish(saved({ status: "settled", outcome: "failed", started_at: utc })); await h.settle();
  h.value.beginEdit(true); assert.equal(h.value.revisionBase, null);
  h.value.beginEdit(false); await h.settle(); await h.value.saveDraft(); await h.settle();
  assert.match(h.calls.find((call) => call.name === "createAssistantPlan").args[1].idempotencyKey, /^manual:/);
  assert.equal(h.count("reviseAssistantPlan"), 0); assert.equal(h.count("executeAssistantPlan"), 0); h.unmount();
});

test("workspace preserves centered modal, bounded typed controls, complete frozen hash and separate approval surface", () => {
  assert.match(source, /<Modal open=\{open\} centered/);
  assert.match(source, /完整冻结摘要/); assert.match(source, /清单有效期至/); assert.match(source, /批准有效期至/);
  assert.match(source, /审批读取，不提供代执行/); assert.match(source, /max=\{86400\}/);
  assert.doesNotMatch(source, /Input\.TextArea|dangerouslySetInnerHTML|eval\(|new Function/);
});
