import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const pageSource = await readFile(new URL("../src/StateInvestigationPage.jsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/modules/stateInvestigationApi.js", import.meta.url), "utf8");
const routerSource = await readFile(new URL("../src/AppPageRouter.jsx", import.meta.url), "utf8");
const menuSource = await readFile(new URL("../src/menuConfig.jsx", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/stateInvestigation.css", import.meta.url), "utf8");

test("state investigation workbench covers state scene probe and kernel domains", () => {
  assert.match(pageSource, /状态时间线/);
  assert.match(pageSource, /现场采集/);
  assert.match(pageSource, /动态探针/);
  assert.match(pageSource, /内核观测/);
  assert.match(pageSource, /Scene Context Pack/);
  assert.match(pageSource, /Kill Switch/);
});

test("state investigation APIs expose the complete control and evidence workflow", () => {
  assert.match(apiSource, /\/states\/timeline/);
  assert.match(apiSource, /\/fault-patterns\/matches/);
  assert.match(apiSource, /\/scene-captures/);
  assert.match(apiSource, /captures\/\$\{captureType === "thread_trace" \? "thread-trace" : captureType\}/);
  assert.match(apiSource, /\/probe-templates/);
  assert.match(apiSource, /\/probe-leases/);
  assert.match(apiSource, /\/kernel-observability\/capabilities/);
  assert.match(apiSource, /\/kernel-observability\/kill-switch/);
  assert.match(apiSource, /\/kernel-events/);
  assert.match(apiSource, /\/stacks/);
});

test("state investigation lives under intelligent diagnosis and uses adaptive centered modals", () => {
  assert.match(menuSource, /diagnose\.investigation/);
  assert.match(menuSource, /label: "状态与现场"/);
  assert.match(routerSource, /"diagnose\.investigation": \{ c: StateInvestigationPage/);
  assert.match(pageSource, /function AdaptiveModal/);
  assert.match(pageSource, /\bcentered\b/);
  assert.match(pageSource, /maxHeight: "calc\(100vh - 180px\)"/);
  assert.doesNotMatch(pageSource, /<Drawer/);
  assert.match(styleSource, /@media \(max-width: 1180px\)/);
  assert.match(styleSource, /@media \(max-width: 720px\)/);
});

test("background polling only runs while capture or probe work remains active", () => {
  assert.match(pageSource, /if \(!activeCount\) return undefined;/);
  assert.match(pageSource, /ACTIVE_CAPTURE_STATES/);
  assert.match(pageSource, /ACTIVE_PROBE_STATES/);
  assert.match(pageSource, /window\.clearInterval/);
  assert.doesNotMatch(pageSource, /setInterval\([^)]*,\s*[0-9]+\);\s*return undefined/);
});

test("high risk collection is gated and arbitrary probe code is not accepted", () => {
  assert.match(pageSource, /深度内存与 Dump 默认进入审批/);
  assert.match(pageSource, /仅可选择平台签名的内置模板/);
  assert.match(pageSource, /不接受任意代码、命令或脚本/);
  assert.match(pageSource, /PID、进程启动时间/);
  assert.match(pageSource, /TTL/);
});

test("native runtime probes require an explicit Linux target and dispatch sensor config", () => {
  assert.match(pageSource, /sensors_json\?\.includes\("runtime\.uprobe"\)/);
  assert.match(pageSource, /Linux 二进制绝对路径/);
  assert.match(pageSource, /必须填写 Linux 绝对路径/);
  assert.match(pageSource, /"runtime\.uprobe": \{/);
  assert.match(pageSource, /sensor_config: requiresUprobe/);
});
