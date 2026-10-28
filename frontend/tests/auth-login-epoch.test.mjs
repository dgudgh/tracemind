import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import vm from "node:vm";
import test from "node:test";
import { parse } from "@babel/parser";

const source = readFileSync(new URL("../src/modules/authStorage.js", import.meta.url), "utf8");
const code = parse(source, { sourceType: "module" }).program.body.map((node) => {
  const declaration = node.declaration || node;
  return source.slice(declaration.start, declaration.end);
}).join("\n");

function boot(data, options = {}) {
  return vm.runInNewContext(`${code}\n({ getAuthSession, getAuthToken, isAuthSessionCurrent, saveAuthToken, clearAuthToken })`, {
    crypto: options.crypto ?? webcrypto,
    localStorage: {
      getItem: (key) => { if (options.readBlocked) throw new Error("blocked"); return data.get(key) ?? null; },
      setItem: (key, value) => { if (options.writeBlocked || options.setBlocked || (options.setTokenBlocked && key === "mesguard_token")) throw new Error("blocked"); data.set(key, value); },
      removeItem: (key) => { if (options.writeBlocked || options.removeBlocked || (options.removeTokenBlocked && key === "mesguard_token")) throw new Error("blocked"); data.delete(key); },
    },
  });
}

test("normal reload keeps a durable epoch while same-token login and logout rotate it", () => {
  const data = new Map(), first = boot(data);
  first.saveAuthToken("synthetic-session");
  const before = first.getAuthSession();
  assert.match(before.loginEpoch, /^[a-f0-9]{32}$/);
  const reload = boot(data), restored = reload.getAuthSession();
  assert.equal(restored.generation, 0);
  assert.equal(restored.loginEpoch, before.loginEpoch);
  reload.saveAuthToken("synthetic-session");
  const newLogin = reload.getAuthSession();
  assert.notEqual(newLogin.loginEpoch, before.loginEpoch);
  assert.equal(first.isAuthSessionCurrent(before), false);
  reload.clearAuthToken();
  assert.equal(reload.getAuthSession().token, "");
  reload.saveAuthToken("synthetic-session");
  assert.notEqual(boot(data).getAuthSession().loginEpoch, newLogin.loginEpoch);
});

test("legacy logged-in storage bootstraps once without copying credentials into epoch metadata", () => {
  const data = new Map([["mesguard_token", "synthetic-legacy-session"]]);
  const client = boot(data), one = client.getAuthSession(), two = client.getAuthSession();
  assert.equal(one.loginEpoch, two.loginEpoch);
  assert.equal(boot(data).getAuthSession().loginEpoch, one.loginEpoch);
  assert.equal(data.size, 2);
  assert.equal(data.get("mesguard.loginEpoch.v1").includes("synthetic"), false);
});

test("unavailable storage or randomness cannot claim a durable login identity", () => {
  for (const options of [{ writeBlocked: true }, { readBlocked: true }, { crypto: {} }]) {
    const client = boot(new Map(), options);
    client.saveAuthToken("synthetic-fallback");
    const session = client.getAuthSession();
    assert.equal(session.loginEpoch, null);
    assert.equal(client.isAuthSessionCurrent(session), true);
    client.saveAuthToken("synthetic-fallback");
    assert.equal(client.isAuthSessionCurrent(session), false);
  }
});

test("a failed storage operation cannot leave a reusable prior login epoch on reload", () => {
  for (const option of ["setBlocked", "removeBlocked"]) {
    const data = new Map(), options = {}, client = boot(data, options);
    client.saveAuthToken("same-test-token");
    const old = client.getAuthSession().loginEpoch;
    options[option] = true;
    client.saveAuthToken("same-test-token");
    options[option] = false;
    assert.notEqual(boot(data).getAuthSession().loginEpoch, old);
  }
});

test("failed token replacement never silently authenticates as the prior persisted identity", () => {
  const data = new Map(), options = {}, client = boot(data, options);
  client.saveAuthToken("old-A"); options.setTokenBlocked = true;
  client.saveAuthToken("new-B");
  assert.equal(client.getAuthToken(), "new-B");
  assert.equal(data.get("mesguard_token"), "old-A");
  assert.equal(client.getAuthSession().loginEpoch, null);
  options.setTokenBlocked = false;
  const other = boot(data); other.saveAuthToken("new-C");
  assert.equal(client.getAuthToken(), "new-C");
});

test("failed logout removal and successful cross-tab logout cannot resurrect an old RAM token", () => {
  const data = new Map(), options = {}, first = boot(data, options);
  first.saveAuthToken("old-A"); options.removeTokenBlocked = true; first.clearAuthToken();
  assert.equal(first.getAuthToken(), "");
  options.removeTokenBlocked = false; first.saveAuthToken("old-A");
  boot(data).clearAuthToken();
  assert.equal(first.getAuthToken(), "");
});
