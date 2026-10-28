import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("login keeps local administrator fallback and exposes enterprise password login", () => {
  const clients = read("../src/apiClients.js");
  const state = read("../src/AppRootState.js");
  const entry = read("../src/SystemEntryPages.jsx");
  const root = read("../src/AppRoot.jsx");

  assert.match(clients, /externalLogin/);
  assert.match(clients, /\/auth\/external-login/);
  assert.match(clients, /oidcProviders/);
  assert.match(clients, /\/auth\/oidc\/exchange/);
  assert.match(state, /doExternalLogin/);
  assert.match(state, /external_login_code/);
  assert.match(entry, /await onLogin\(values\.username, values\.password\)/);
  assert.match(entry, /企业账号登录/);
  assert.match(entry, /open=\{enterpriseLoginOpen\}/);
  assert.match(root, /onExternalLogin/);
  assert.match(entry, /单点登录/);
});
