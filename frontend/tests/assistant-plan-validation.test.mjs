import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createServer } from "vite";

const planId = `pl${"a".repeat(26)}`;
const validation = (overrides = {}) => ({ schema: "capability.plan_validation/v1", plan_id: planId,
  revision: 1, checked_at: "2026-10-20T08:30:00.123456+00:00", plan_status: "validated", can_execute: true,
  issues: [], approval_available: false, requires_recheck: true, ...overrides });
let vite, transport, shared;
test.before(async () => {
  vite = await createServer({ root: new URL("..", import.meta.url).pathname,
    appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  transport = await vite.ssrLoadModule("/src/modules/assistantApi.js");
  shared = await vite.ssrLoadModule("/src/api.js");
});
test.after(async () => { await vite?.close(); });

test("validation uses exactly one shared GET with no input, POST or implicit follow-up", async (t) => {
  const calls = [], response = validation();
  t.mock.method(shared.api, "request", async (...args) => { calls.push(args); return response; });
  assert.equal(await transport.getAssistantPlanValidation(planId), response);
  assert.deepEqual(calls, [[`/assistant/plans/${planId}/validation`, {}]]);
  const source = readFileSync(new URL("../src/modules/assistantApi.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\bfetch\s*\(|\baxios\b|setTimeout|console\./);
});

test("validation IDs are checked before I/O, including encodings, path suffixes and line terminators", (t) => {
  let calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; });
  for (const id of [undefined, null, false, 7, {}, [], "", "../plans", `${planId}/execute`, `${planId}?role=admin`,
    `${planId}%2f`, `${planId}\n`, `${planId}\r`, `${planId}\u2028`, `PL${"a".repeat(26)}`,
    `pl${"a".repeat(25)}`, `pl${"a".repeat(27)}`, `pl${"g".repeat(26)}`]) {
    assert.throws(() => transport.getAssistantPlanValidation(id), { name: "TypeError", message: "Invalid assistant plan ID" });
  }
  assert.equal(calls, 0);
});

test("strict v1 validation rejects malformed types, envelope fields, issue structures and unsafe codes", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  const invalid = [undefined, null, [], {}, "<html>password=not-for-display</html>",
    validation({ schema: "capability.plan_read/v1" }), validation({ schema: "capability.plan_validation/v2" }),
    validation({ plan_id: `pl${"b".repeat(26)}` }), validation({ plan_id: `${planId}\n` }), validation({ plan_id: null }),
    validation({ revision: "1" }), validation({ revision: true }), validation({ revision: 0 }),
    validation({ revision: -1 }), validation({ revision: 1.5 }), validation({ revision: Number.MAX_SAFE_INTEGER + 1 }),
    validation({ plan_status: null }), validation({ plan_status: "future" }), validation({ plan_status: "constructor" }),
    validation({ plan_status: ["validated"] }), validation({ can_execute: "true" }), validation({ can_execute: 1 }),
    validation({ can_execute: null }), validation({ approval_available: "true" }), validation({ approval_available: 0 }),
    validation({ requires_recheck: false }), validation({ requires_recheck: "true" }),
    validation({ issues: null }), validation({ issues: {} }), validation({ issues: "capability_disabled" }),
    validation({ can_execute: false, issues: [null] }), validation({ can_execute: false, issues: [[]] }),
    validation({ can_execute: false, issues: ["capability_disabled"] }), validation({ can_execute: false, issues: [{}] }),
    validation({ can_execute: false, issues: [{ code: 1 }] }), validation({ can_execute: false, issues: [{ code: null }] }),
    validation({ can_execute: false, issues: [{ code: "" }] }), validation({ can_execute: false, issues: [{ code: "X" }] }),
    validation({ can_execute: false, issues: [{ code: "plan_disabled\n" }] }),
    validation({ can_execute: false, issues: [{ code: "password=not-for-display" }] }),
    validation({ can_execute: false, issues: [{ code: "a".repeat(65) }] }),
    validation({ can_execute: false, issues: [{ code: "capability_disabled", detail: "password=not-for-display" }] }),
    validation({ can_execute: false, issues: Array(101).fill({ code: "capability_disabled" }) }),
    validation({ extra: "password=not-for-display" }),
    validation({ issues: [{ code: "capability_disabled" }] }), validation({ plan_status: "settled" })];
  for (const field of Object.keys(validation())) {
    const missing = validation();
    delete missing[field];
    invalid.push(missing);
  }
  for (response of invalid) {
    await assert.rejects(transport.getAssistantPlanValidation(planId), (error) => error.code === "assistant_response_invalid"
      && error.message === "Invalid assistant API response" && !Object.hasOwn(error, "responseData"));
  }
});

test("current revisions and approval-aware reviews preserve the strict v1 envelope", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (const value of [validation({ revision: 2 }), validation({ revision: 2, approval_available: true }),
    validation({ revision: 3, approval_available: true, plan_status: "approved" })]) {
    response = value;
    assert.equal(await transport.getAssistantPlanValidation(planId), value);
  }
  response = validation({ revision: 2, approval_available: true, plan_status: "awaiting_approval", can_execute: true });
  await assert.rejects(transport.getAssistantPlanValidation(planId), { code: "assistant_response_invalid" });
});

test("checked_at must be a real UTC ISO timestamp, not a local date or a normalized impossible date", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (const checked_at of [null, 123, {}, "", "2026-10-20", "2026-10-20T08:30:00", "2026-10-20T08:30:00+08:00",
    "2026-02-29T00:00:00Z", "2026-02-30T00:00:00Z", "2026-10-20T24:00:00Z", "2026-10-20T08:30:60Z",
    "2026-10-20T08:30:00Z\n", "not-a-time", "2026-10-20T08:30:00.1234567Z"]) {
    response = validation({ checked_at });
    await assert.rejects(transport.getAssistantPlanValidation(planId), { code: "assistant_response_invalid" });
  }
  for (const checked_at of ["2026-10-20T08:30:00Z", "2026-10-20T08:30:00.1Z", "2026-10-20T08:30:00.123456+00:00",
    "2024-02-29T08:30:00+00:00"]) {
    response = validation({ checked_at });
    assert.equal(await transport.getAssistantPlanValidation(planId), response);
  }
});

test("all known blocked plan states and bounded issue codes are retained without invented approvals", async (t) => {
  let response;
  t.mock.method(shared.api, "request", async () => response);
  for (const plan_status of ["draft", "validated", "awaiting_approval", "approved", "running", "settled"]) {
    response = validation({ plan_status, can_execute: false, issues: [{ code: "capability_disabled" }, { code: "a".repeat(64) }] });
    const before = structuredClone(response);
    assert.equal(await transport.getAssistantPlanValidation(planId), response);
    assert.deepEqual(response, before);
  }
  response = validation({ can_execute: false });
  assert.equal(await transport.getAssistantPlanValidation(planId), response);
});

for (const status of [401, 403, 404, 409, 422, 500, 503, 504]) {
  test(`validation HTTP ${status} propagates unchanged with no fallback or retry`, async (t) => {
    const error = Object.assign(new Error("controlled failure"), { status, responseData: validation() });
    let calls = 0;
    t.mock.method(shared.api, "request", async () => { calls += 1; throw error; });
    await assert.rejects(transport.getAssistantPlanValidation(planId), (actual) => actual === error);
    assert.equal(calls, 1);
  });
}

test("network and timeout rejections propagate unchanged without implicit retry", async (t) => {
  let failure, calls = 0;
  t.mock.method(shared.api, "request", async () => { calls += 1; throw failure; });
  for (failure of [new TypeError("network unavailable"), new DOMException("timed out", "TimeoutError"),
    new DOMException("aborted", "AbortError")]) {
    await assert.rejects(transport.getAssistantPlanValidation(planId), (actual) => actual === failure);
  }
  assert.equal(calls, 3);
});

test("actual shared transport rejects non-JSON/empty success and emits only the exact validation GET", async (t) => {
  const calls = [];
  let response;
  t.mock.method(globalThis, "fetch", async (...args) => { calls.push(args); return response; });
  for (response of [new Response("<html>private-body</html>", { status: 200 }), new Response(null, { status: 204 }),
    new Response("null", { status: 200 })]) {
    await assert.rejects(transport.getAssistantPlanValidation(planId), { code: "assistant_response_invalid" });
  }
  response = new Response(JSON.stringify(validation()), { status: 200 });
  assert.deepEqual(await transport.getAssistantPlanValidation(planId), validation());
  assert.equal(calls.length, 4);
  assert.ok(calls.every(([url, options]) => url === `/api/v1/assistant/plans/${planId}/validation`
    && options.method === undefined && options.body === undefined));
});
