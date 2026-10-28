import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const apiSource = readFileSync(new URL("../src/modules/userManagementApi.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "")
  .replace(/^export /gm, "");
const page = readFileSync(new URL("../src/UserManagementPages.jsx", import.meta.url), "utf8");

test("reset API sends the operator current password and only the required fields", async () => {
  const calls = [];
  const api = vm.runInNewContext(`${apiSource}\n({ resetUserPassword, getUserManagementBundle });`, {
    api: {
      put: (path, body) => { calls.push({ path, body }); return { detail: "changed" }; },
      get: (path) => path,
    },
    buildQuery: () => "?scope=all",
  });
  await api.resetUserPassword(12, { current_password: "Operator1", new_password: "NewSecret2", confirm_password: "NewSecret2", role: "admin" });
  assert.equal(calls[0].path, "/users/12/reset-password");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), { current_password: "Operator1", new_password: "NewSecret2" });
  assert.ok((await api.getUserManagementBundle()).includes("/auth/current-user"));
});

test("password modal requires current password and distinguishes self from administrator reset", () => {
  assert.match(page, /currentUser\?\.id === resetModal\.userId/);
  assert.match(page, /name="current_password"[\s\S]*?required: true/);
  assert.match(page, /操作管理员当前密码/);
  assert.match(page, /autoComplete="current-password"/);
  assert.match(page, /dependencies=\{\["new_password"\]\}/);
});

test("password form prevents duplicate requests, clears secrets and exits after self change", () => {
  assert.match(page, /if \(resetInFlight\.current\) return/);
  assert.match(page, /resetUserPassword\(resetModal\.userId, values\)/);
  assert.match(page, /resetForm\.resetFields\(\);\s*setResetModal/);
  assert.match(page, /if \(result\.reauthenticate\) \{\s*emitAuthExpired\(\);\s*return;/);
  assert.match(page, /confirmLoading=\{resetSaving\}/);
});
