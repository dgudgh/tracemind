import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@babel/parser";

function moduleBody(file) {
  const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
  return parse(source, { sourceType: "module" }).program.body
    .filter((node) => node.type !== "ImportDeclaration" && !(node.type === "ExportNamedDeclaration" && !node.declaration))
    .map((node) => { const declaration = node.declaration || node; return source.slice(declaration.start, declaration.end); }).join("\n");
}
const code = ["modules/authStorage.js", "modules/authEvents.js", "api.js"].map(moduleBody).join("\n");
function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}
function client({ storageBlocked = false, delayedBody = false } = {}) {
  const response = deferred(), body = deferred();
  const requests = [], events = [], storage = new Map();
  const window = new EventTarget();
  window.addEventListener("auth-expired", () => events.push("expired"));
  const context = vm.createContext({
    window, Event,
    localStorage: {
      getItem: (key) => { if (storageBlocked) throw new Error("blocked"); return storage.get(key); },
      setItem: (key, value) => { if (storageBlocked) throw new Error("blocked"); storage.set(key, value); },
      removeItem: (key) => { if (storageBlocked) throw new Error("blocked"); storage.delete(key); },
    },
    fetch: (path, options) => { requests.push({ path, options }); return response.promise; },
    toApiUrl: (path) => path, toAuthPath: (path) => path, shouldAttachAuth: () => true,
  });
  const bindings = vm.runInContext(`${code}\n({ api, getAuthSession, getAuthToken, saveAuthToken, clearAuthToken });`, context);
  const finish = (status, payload = {}) => {
    response.resolve(delayedBody ? { status, ok: false, text: () => body.promise }
      : new Response(JSON.stringify(payload), { status }));
  };
  return { ...bindings, requests, events, finish, body, storage };
}

for (const status of [401, 403]) {
  for (const sameToken of [false, true]) {
    test(`late ${status} cannot expire a new session (${sameToken ? "same" : "different"} token)`, async () => {
      const c = client();
      c.saveAuthToken("synthetic-A");
      const request = c.api.get("/instances");
      c.clearAuthToken();
      const next = sameToken ? "synthetic-A" : "synthetic-B";
      c.saveAuthToken(next);
      c.finish(status, status === 401 ? { detail: "old session" } : { reason: "expired" });
      await assert.rejects(request, (error) => error.status === status);
      assert.equal(c.requests[0].options.headers.Authorization, "Bearer synthetic-A");
      assert.equal(c.getAuthToken(), next);
      assert.deepEqual(c.events, []);
    });
  }
}

test("session changes while reading the error body are also guarded", async () => {
  const c = client({ delayedBody: true });
  c.saveAuthToken("synthetic-A");
  const request = c.api.get("/instances");
  c.finish(401);
  await Promise.resolve();
  c.saveAuthToken("synthetic-B");
  c.body.resolve('{"detail":"old session"}');
  await assert.rejects(request, (error) => error.status === 401);
  assert.equal(c.getAuthToken(), "synthetic-B");
  assert.deepEqual(c.events, []);
});

test("fallback storage still distinguishes identical-token logins", async () => {
  const c = client({ storageBlocked: true });
  c.saveAuthToken("synthetic-A");
  const request = c.api.get("/instances");
  c.saveAuthToken("synthetic-A");
  c.finish(401);
  await assert.rejects(request);
  assert.equal(c.getAuthToken(), "synthetic-A");
  assert.deepEqual(c.events, []);
});

for (const sameToken of [false, true]) {
  test(`explicit confirmed session rejects a newer ${sameToken ? "same-token epoch" : "identity"} before sending`, async () => {
    const c = client(); c.saveAuthToken("synthetic-A"); const confirmed = c.getAuthSession();
    c.saveAuthToken(sameToken ? "synthetic-A" : "synthetic-B");
    await assert.rejects(c.api.request("/assistant/batches/prepare", { method: "POST", authSession: confirmed, body: "{}" }),
      (error) => error.code === "auth_session_changed" && error.status === 409);
    assert.equal(c.requests.length, 0); assert.deepEqual(c.events, []);
  });
}

test("session is checked again after request preparation and never sampled as another user", async () => {
  const c = client(); c.saveAuthToken("synthetic-A"); const confirmed = c.getAuthSession();
  const headers = { get "X-Fixture"() { c.saveAuthToken("synthetic-B"); return "changed-during-preparation"; } };
  await assert.rejects(c.api.request("/assistant/batches/prepare", { method: "POST", authSession: confirmed, headers, body: "{}" }),
    (error) => error.code === "auth_session_changed");
  assert.equal(c.requests.length, 0);
});

test("confirmed session uses its exact token and never leaks binding metadata into fetch options", async () => {
  const c = client(); c.saveAuthToken("synthetic-A");
  const pending = c.api.request("/assistant/batches/prepare", { method: "POST", authSession: c.getAuthSession(), body: "{}" });
  c.finish(200, { accepted: false }); await pending;
  assert.equal(c.requests[0].options.headers.Authorization, "Bearer synthetic-A");
  assert.equal(Object.hasOwn(c.requests[0].options, "authSession"), false);
});

test("a token replaced in shared storage is not expired by an old request", async () => {
  const c = client();
  c.saveAuthToken("synthetic-A");
  const request = c.api.get("/instances");
  c.storage.set("mesguard_token", "synthetic-B");
  c.finish(401);
  await assert.rejects(request);
  assert.equal(c.getAuthToken(), "synthetic-B");
  assert.deepEqual(c.events, []);
});

for (const status of [401, 403]) {
  test(`current session ${status} retains blocking behavior and HTTP metadata`, async () => {
    const c = client();
    c.saveAuthToken("synthetic-current");
    const request = c.api.get("/instances");
    c.finish(status, status === 401 ? { detail: "expired" } : { reason: "expired" });
    await assert.rejects(request, (error) => error.status === status && (status === 401 || error.licenseBlocked));
    assert.equal(c.getAuthToken(), "");
    assert.deepEqual(c.events, ["expired"]);
  });
}

test("ordinary permission failure and login failure do not clear the session", async () => {
  for (const [path, status, payload] of [["/instances", 403, { detail: "forbidden" }], ["/auth/login", 401, { detail: "bad password" }],
    ["/instances", 403, { reason: "feature_not_licensed" }]]) {
    const c = client();
    c.saveAuthToken("synthetic-current");
    const request = c.api.get(path);
    c.finish(status, payload);
    await assert.rejects(request, (error) => error.status === status);
    assert.equal(c.getAuthToken(), "synthetic-current");
    assert.deepEqual(c.events, []);
  }
});
