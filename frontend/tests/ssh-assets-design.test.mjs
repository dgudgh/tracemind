import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const pageSource = fs.readFileSync(
  path.join(frontendRoot, "src", "SystemSettingsPages.jsx"),
  "utf8",
);
const stylesSource = fs.readFileSync(
  path.join(frontendRoot, "src", "styles.css"),
  "utf8",
);

const sshPageStart = pageSource.indexOf("function SshAssetsPage");
const sshPageEnd = pageSource.indexOf("function AlertReportsPage");
const sshPageSource = pageSource.slice(sshPageStart, sshPageEnd);

test("SSH assets use one compact overview and one flat inventory surface", () => {
  assert.match(sshPageSource, /className="ssh-assets-page"/);
  assert.match(sshPageSource, /className="ssh-assets-overview"/);
  assert.match(sshPageSource, /className="ssh-assets-table-shell"/);
  assert.doesNotMatch(sshPageSource, /<Alert\b/);
  assert.doesNotMatch(sshPageSource, /<Card\b/);
});

test("SSH asset columns group related connection facts", () => {
  for (const title of ["资产", "连接目标", "登录身份", "SSH 状态", "操作"]) {
    assert.match(sshPageSource, new RegExp(`title: "${title}"`));
  }

  for (const obsoleteTitle of ["实例", "数据库", "业务主机", "SSH主机", "端口", "账号", "环境", "状态", "只读操作"]) {
    assert.doesNotMatch(sshPageSource, new RegExp(`title: "${obsoleteTitle}"`));
  }

  assert.match(sshPageSource, /ssh-assets-endpoint-primary/);
  assert.match(sshPageSource, /ssh-assets-engine/);
  assert.doesNotMatch(sshPageSource, />unknown</);
});

test("SSH inventory styling keeps rows stable on desktop and narrow screens", () => {
  const requiredSelectors = [
    ".ssh-assets-page",
    ".ssh-assets-overview",
    ".ssh-assets-table-shell",
    ".ssh-assets-identity",
    ".ssh-assets-endpoint-primary",
    ".ssh-assets-actions",
    "@media (max-width: 720px)",
  ];

  for (const selector of requiredSelectors) {
    assert.ok(stylesSource.includes(selector), `missing SSH asset design rule: ${selector}`);
  }
});

test("row actions keep their own progress state instead of blanking the table", () => {
  const runActionStart = sshPageSource.indexOf("const runAction");
  const sshReadyStart = sshPageSource.indexOf("const sshReady");
  const runActionSource = sshPageSource.slice(runActionStart, sshReadyStart);

  assert.match(sshPageSource, /const \[actionLoading, setActionLoading\]/);
  assert.match(runActionSource, /setActionLoading/);
  assert.doesNotMatch(runActionSource, /setLoading/);
});

test("connection test output drives a truthful Chinese SSH status", () => {
  assert.match(sshPageSource, /const \[connectionStates, setConnectionStates\]/);
  assert.match(sshPageSource, /连接正常/);
  assert.match(sshPageSource, /连接失败/);
  assert.match(sshPageSource, /待检测/);
  assert.match(sshPageSource, /data\?\.ok/);
  assert.doesNotMatch(sshPageSource, /\{v \|\| "unknown"\}/);
});
