import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


const page = readFileSync(new URL("../src/StorageRescuePage.jsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/modules/recoveryApi.js", import.meta.url), "utf8");
const menu = readFileSync(new URL("../src/menuConfig.jsx", import.meta.url), "utf8");


test("storage rescue exposes cases evidence and exact tool authorization", () => {
  assert.match(page, /案件与证据/);
  assert.match(page, /独立校验源镜像/);
  assert.match(page, /SHA256 与只读校验/);
  assert.match(page, /工具授权/);
  assert.match(page, /digest_sha256/);
  assert.match(page, /授权 Agent 工具/);
});


test("storage rescue task creation is gated by case image and authorization", () => {
  assert.match(page, /case_id: values\.case_id/);
  assert.match(page, /source_image_id: values\.source_image_id/);
  assert.match(page, /tool_authorization_id: values\.tool_authorization_id/);
  assert.match(page, /该命令必须选择已完成 SHA256 与只读校验的源镜像/);
  assert.match(page, /没有匹配此命令的有效工具授权/);
});


test("storage rescue dialogs are centered and active polling stays lightweight", () => {
  assert.match(page, /className="adaptive-center-modal"/);
  assert.match(page, /\bcentered\b/);
  assert.match(page, /const FAST_POLLING_STATUSES/);
  assert.match(page, /const loadTaskState = async/);
  assert.match(page, /setInterval\(\(\) => loadTaskState\(filters\), 3000\)/);
  assert.match(page, /const loadTabData = async/);
  assert.match(page, /setInterval\(\(\) => loadTabData\(tab, filters, true\), 15000\)/);
  assert.doesNotMatch(page, /setInterval\(\(\) => load\(filters, true\), 3000\)/);
  assert.doesNotMatch(page, /setInterval\(\(\) => load\(filters, true\), 10000\)/);
});


test("storage rescue API and menu cover the governed recovery lifecycle", () => {
  for (const contract of [
    "standardCases",
    "createCase",
    "sourceImages",
    "verifySourceImage",
    "agentTools",
    "createToolAuthorization",
    "revokeToolAuthorization",
    "initiateUpload",
    "completeUpload",
  ]) {
    assert.match(api, new RegExp(contract));
  }
  assert.match(menu, /key: "rescue\.cases"/);
  assert.match(menu, /key: "rescue\.tools"/);
});
