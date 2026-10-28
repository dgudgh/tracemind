import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const hash = (n = 1) => n.toString(16).padStart(64, "0");
const session = { token: "offline-fixture", generation: 1, loginEpoch: "proposal-login-epoch-01" };
const key = `proposal:${"a".repeat(32)}`;
let vite, api, shared, panel;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  api = await vite.ssrLoadModule("/src/modules/assistantProposalApi.js");
  shared = await vite.ssrLoadModule("/src/api.js");
  panel = await vite.ssrLoadModule("/src/AssistantProposalPanel.jsx");
});
test.after(async () => { await vite?.close(); });

const definition = (overrides = {}) => ({ workflow_id: "inspection.jobs.list", workflow_version: 1, definition_hash: hash(1), ...overrides });
const candidate = (overrides = {}) => ({ schema: "capability.workflow_candidate/v1", accepted: false,
  workflow_binding: { ...definition(), recipe_snapshot: {} }, missing_fields: [],
  scope: { mode: "explicit", targets: [{ instance_id: 1 }] }, slots: {},
  plan_inputs: [{ capability_id: "inspection.run.query", instance_id: 1, steps: [] }],
  effective_inputs: [{ instance_id: 1, step_params: [] }], candidate_hash: hash(2), ...overrides });
const proposal = (overrides = {}) => ({ schema: "capability.workflow_proposal/v1", validator_version: 1, accepted: false,
  model_decision: "matched", decision: "matched", workflow: definition(), missing_fields: [], candidate: candidate(),
  catalog_hash: hash(3), proposal_hash: hash(4), validation_scope: "structure_only", requires_confirmation: true,
  proposal_id: `WP${"b".repeat(28)}`, provider: { config_id: 1, revision: hash(5), model: "fixture" }, ...overrides });
const providers = (items = [{ config_id: 1, name: "fixture", provider_type: "ollama", model: "m", revision: hash(5), is_default: true }]) =>
  ({ schema: "capability.workflow_providers/v1", items, notice: "consent" });

test("providers are bounded consent material and never carry endpoints or keys", () => {
  assert.doesNotThrow(() => api.validateProviders(providers()));
  for (const value of [null, {}, { ...providers(), schema: "x" }, { ...providers(), items: [{}] },
    { ...providers(), items: [{ ...providers().items[0], base_url: "http://10.0.0.9" }] },
    { ...providers(), items: [{ ...providers().items[0], revision: "not-a-hash" }] },
    { ...providers(), items: [providers().items[0], providers().items[0]] },
    { ...providers(), notice: "x".repeat(501) }]) {
    assert.throws(() => api.validateProviders(value), { code: "assistant_proposal_response_invalid" });
  }
});

test("a proposal must stay unaccepted, structure-only and consistent with its decision", () => {
  assert.equal(api.validateProposal(proposal()).accepted, false);
  for (const value of [null, {}, { ...proposal(), accepted: true }, { ...proposal(), requires_confirmation: false },
    { ...proposal(), validation_scope: "authorized" }, { ...proposal(), proposal_id: "WPbad" },
    { ...proposal(), decision: "matched", candidate: null }, { ...proposal(), decision: "needs_input" },
    { ...proposal(), candidate: candidate({ accepted: true }) }, { ...proposal(), candidate: candidate({ candidate_hash: "x" }) },
    { ...proposal(), provider: { config_id: 1, revision: "x", model: "m" } },
    { ...proposal(), extra: 1 }]) {
    assert.throws(() => api.validateProposal(value), { code: "assistant_proposal_response_invalid" });
  }
});

test("input validation requires explicit consent, a strict provider revision and a confirmed scope", () => {
  const good = { text: "查看已保存巡检作业", scope: { mode: "explicit", targets: [{ instance_id: 1 }] },
    provider_config_id: 1, provider_revision: hash(5), allow_model_context: true, confirmed_policy_id: null };
  assert.doesNotThrow(() => api.validateProposalInput(good));
  for (const value of [{ ...good, allow_model_context: false }, { ...good, allow_model_context: undefined },
    { ...good, text: "   " }, { ...good, text: "x".repeat(4001) }, { ...good, text: "a\u0001b" },
    { ...good, provider_config_id: 0 }, { ...good, provider_config_id: 1.5 }, { ...good, provider_config_id: true },
    { ...good, provider_revision: "abc" }, { ...good, scope: { mode: "explicit", targets: [] } },
    { ...good, scope: { mode: "global", targets: [] } }, { ...good, scope: { mode: "explicit", targets: [{ instance_id: 1 }, { instance_id: 1 }] } },
    { ...good, confirmed_policy_id: 0 }, { ...good, extra: true }]) {
    assert.throws(() => api.validateProposalInput(value), TypeError);
  }
});

test("POST uses the frozen route, original key and captured session, then validates the reply", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "request", async (path, options) => { calls.push([path, options]); return proposal(); });
  const payload = { text: "查看已保存巡检作业", scope: { mode: "global" }, provider_config_id: 1,
    provider_revision: hash(5), allow_model_context: true, confirmed_policy_id: null };
  const value = await api.requestProposal(payload, { authSession: session, idempotencyKey: key });
  assert.equal(value.proposal_id, proposal().proposal_id);
  assert.equal(calls[0][0], "/assistant/workflows/proposals");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].authSession, session);
  assert.equal(calls[0][1].headers["Idempotency-Key"], key);
  assert.deepEqual(JSON.parse(calls[0][1].body), payload);
  for (const bad of [undefined, "proposal:xyz", "prepare:" + "a".repeat(32)]) {
    assert.throws(() => api.requestProposal(payload, { authSession: session, idempotencyKey: bad }), TypeError);
  }
  assert.equal(calls.length, 1);
});

test("recovery reads with the same key and never re-posts", async (t) => {
  const calls = [];
  t.mock.method(shared.api, "get", async (path, options) => { calls.push([path, options]); return proposal(); });
  await api.getProposal({ authSession: session, idempotencyKey: key });
  assert.equal(calls[0][0], "/assistant/workflows/proposals");
  assert.equal(calls[0][1].headers["Idempotency-Key"], key);
  assert.equal(calls[0][1].method, undefined);
});

test("adoption is pure, catalog-authoritative and refuses a withdrawn or rehashed recipe", () => {
  const catalog = [definition()];
  const adopted = api.proposalAdoption(proposal(), catalog);
  assert.deepEqual(adopted.slots, {});
  assert.deepEqual(adopted.scope, { mode: "explicit", targets: [{ instance_id: 1 }] });
  assert.throws(() => api.proposalAdoption(proposal(), []), { code: "assistant_proposal_catalog_changed" });
  assert.throws(() => api.proposalAdoption(proposal(), [definition({ definition_hash: hash(9) })]), { code: "assistant_proposal_catalog_changed" });
  assert.throws(() => api.proposalAdoption(proposal({ decision: "needs_input", candidate: null }), catalog), TypeError);
});

test("the stored original key is bounded, typed and login-epoch scoped", () => {
  const store = new Map();
  const storage = { getItem: (name) => (store.has(name) ? store.get(name) : null), setItem: (name, value) => store.set(name, value), removeItem: (name) => store.delete(name) };
  assert.equal(api.readStoredProposalKey(storage), null);
  api.storeProposalKey(key, session.loginEpoch, storage);
  assert.deepEqual(api.readStoredProposalKey(storage), { key, login_epoch: session.loginEpoch });
  assert.throws(() => api.storeProposalKey("bad", session.loginEpoch, storage), TypeError);
  store.set(api.PROPOSAL_KEY, "x".repeat(500));
  assert.equal(api.readStoredProposalKey(storage), null);
  api.clearProposalKey(storage);
  assert.equal(api.readStoredProposalKey(storage), null);
});

test("scope builder never invents a target and drops unusable ids", () => {
  assert.equal(api.scopeFromTargets([]), null);
  assert.equal(api.scopeFromTargets([0, -1, 1.5, "2"]), null);
  assert.deepEqual(api.scopeFromTargets([2, 3]), { mode: "explicit", targets: [{ instance_id: 2 }, { instance_id: 3 }] });
});

test("the panel renders as an assistive block, never as an execution entry", () => {
  const tree = renderToStaticMarkup(React.createElement(panel.AssistantProposalPanel, {
    session, instances: [], catalog: [], onAdopt: () => {},
  }));
  assert.match(tree, /AI 候选（仅结构）/);
  assert.match(tree, /不会自动执行/);
  // No freeze/approve/enqueue control may live in this block.
  assert.doesNotMatch(tree, /冻结计划|提交批次准入|采用为手工草稿|请求结构候选/);
});
