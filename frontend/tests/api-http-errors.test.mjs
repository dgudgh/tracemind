import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const source = readFileSync(new URL("../src/api.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "")
  .replace(/^export const api =/m, "const api =")
  .replace(/^export async function /m, "async function ")
  .replace(/^export \{ buildQuery \};$/m, "");

function client(status, body) {
  const events = [];
  const api = vm.runInNewContext(`${source}\napi;`, {
    fetch: async () => new Response(status === 204 ? null : JSON.stringify(body), { status }),
    getAuthToken: () => "test-token",
    getAuthSession: () => ({ token: "test-token", generation: 0 }),
    isAuthSessionCurrent: () => true,
    clearAuthToken: () => events.push("clear"),
    emitAuthExpired: () => events.push("expired"),
    toApiUrl: (path) => path,
    toAuthPath: (path) => path,
    shouldAttachAuth: () => true,
  });
  return { api, events };
}

test("HTTP failures preserve status without conflating permission and network errors", async () => {
  for (const status of [403, 404, 422, 500, 503]) {
    const { api, events } = client(status, { detail: "denied" });
    await assert.rejects(api.get("/data-governance/jobs"), (error) => error.status === status && error.message === "denied");
    assert.deepEqual(events, []);
  }
});

test("status metadata preserves login expiration behavior", async () => {
  const { api, events } = client(401, { detail: "expired" });
  await assert.rejects(api.get("/instances"), (error) => error.status === 401);
  assert.deepEqual(events, ["clear", "expired"]);
  const login = client(401, { detail: "wrong password" });
  await assert.rejects(login.api.post("/auth/login", {}), (error) => error.status === 401);
  assert.deepEqual(login.events, []);
});

test("license errors keep their existing signal and session handling", async () => {
  for (const reason of ["expired", "feature_not_licensed"]) {
    const { api, events } = client(403, { reason });
    await assert.rejects(api.get("/instances"), (error) => error.licenseBlocked === true && error.reason === reason && error.status === 403);
    assert.deepEqual(events, reason === "expired" ? ["clear", "expired"] : []);
  }
});

test("downloads also expose HTTP status", async () => {
  const { api } = client(403, { detail: "export denied" });
  for (const call of [() => api.download("/export"), () => api.downloadFile("/export", "test.txt")]) {
    await assert.rejects(call(), (error) => error.status === 403 && error.message === "export denied");
  }
});

test("empty successful responses remain null", async () => {
  const { api } = client(204);
  assert.equal(await api.get("/empty"), null);
});

test("structured error data is explicit opt-in without weakening HTTP failures", async () => {
  const body = { schema: "capability.plan_read/v1", plan_id: "pl" + "a".repeat(26), outcome: "needs_input" };
  const { api } = client(422, body);
  await assert.rejects(api.post("/assistant/plans", {}), (error) => error.status === 422 && !("responseData" in error));
  await assert.rejects(api.request("/assistant/plans", { method: "POST", body: "{}", preserveErrorData: true }),
    (error) => error.status === 422 && error.responseData.plan_id === body.plan_id);
});

test("edition-locked 403 is distinguishable from an ordinary permission denial", async () => {
  // 版本能力限制必须能被前端识别：否则页面只能把 403 文案当业务错误弹出来，
  // 用户在开源版里看到「该模块需要企业版授权」却分不清是故障还是版本差异。
  const locked = client(403, {
    detail: { code: "edition_module_unavailable", message: "当前为开源版，该模块需要企业版授权" },
  });
  await assert.rejects(locked.api.get("/zabbix/problems"), (error) =>
    error.status === 403
    && error.editionLocked === true
    && error.code === "edition_module_unavailable");

  for (const code of ["edition_capability_unavailable", "edition_engine_unavailable"]) {
    const other = client(403, { detail: { code, message: "locked" } });
    await assert.rejects(other.api.get("/oracle/1/awr/compare"), (error) =>
      error.editionLocked === true && error.code === code);
  }

  // 普通 403 不能被误标成版本限制，否则真实的权限问题会被降级成「需企业版」。
  const plain = client(403, { detail: { code: "permission_denied", message: "无权限" } });
  await assert.rejects(plain.api.get("/instances"), (error) =>
    error.status === 403 && error.editionLocked === false && error.code === null);
});
