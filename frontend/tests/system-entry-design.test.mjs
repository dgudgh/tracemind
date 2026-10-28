import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const source = fs.readFileSync(
  path.join(frontendRoot, "src", "SystemEntryPages.jsx"),
  "utf8",
);
const styles = fs.readFileSync(
  path.join(frontendRoot, "src", "styles.css"),
  "utf8",
);

test("system initialization uses the shared entry layout", () => {
  assert.match(source, /className="system-init-shell"/);
  assert.match(source, /className="system-init-card"/);
  assert.doesNotMatch(source, /maxWidth:\s*560,\s*margin:\s*"80px auto"/);
});

test("login and initialization use the same restrained surface scale", () => {
  assert.ok(styles.includes(".tracemind-login-shell"));
  assert.ok(styles.includes(".system-init-shell"));
  assert.ok(styles.includes(".system-init-card"));
});

test("login restores the original cockpit card and keeps enterprise auth secondary", () => {
  assert.doesNotMatch(source, /<Segmented/);
  assert.doesNotMatch(source, /label="登录方式"/);
  assert.match(source, /企业账号登录/);
  assert.match(source, /open=\{enterpriseLoginOpen\}/);
  assert.match(
    styles,
    /\.tracemind-login-panel\s*\{[\s\S]*?border-radius:\s*28px;[\s\S]*?backdrop-filter:\s*blur\(24px\);/,
  );
  assert.doesNotMatch(styles, /\.tracemind-login-panel,\s*\.system-init-card/);
});

test("system initialization password guidance matches validation", () => {
  assert.match(source, /placeholder="至少 8 位"/);
  assert.doesNotMatch(source, /placeholder="至少 6 位"/);
});
