import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

import { degradationBanner, manualHint } from "../src/modules/aiDegradation.js";

const root = path.resolve(import.meta.dirname, "..");
const read = (relative) => readFileSync(path.join(root, "src", relative), "utf8");

test("ok or absent degradation renders no banner", () => {
  assert.equal(degradationBanner(null), null);
  assert.equal(degradationBanner(undefined), null);
  assert.equal(degradationBanner({ status: "ok", local_result_available: true }), null);
  assert.equal(degradationBanner({ status: "something_else" }), null);
});

test("every degraded status maps to one fixed tone and text", () => {
  const cases = {
    not_configured: "info",
    model_failed: "warning",
    output_rejected: "warning",
    not_sent: "warning",
  };
  for (const [status, tone] of Object.entries(cases)) {
    const banner = degradationBanner({ status, local_result_available: true, reason_code: null });
    assert.equal(banner.tone, tone, status);
    assert.equal(typeof banner.text, "string");
    assert.ok(banner.text.includes("本地规则"), status);
    assert.ok(!banner.text.includes("not_configured"), "banner shows prose, not the raw code");
  }
});

test("diagnosis entry renders the banner and awr no longer reads ai_error", () => {
  assert.match(read("modules/diagnosisCenterApi.js"), /degradationBanner/);
  const page = read("DiagnosisCenterPage.jsx");
  assert.match(page, /setAiDegradationNotice\(\{ tone: "warning", text: result.reason \}\)/);
  assert.match(page, /aiDegradationNotice\.hint/);
  assert.match(page, /aiDegradationNotice/);
  const awr = read("OracleAwrReportPage.jsx");
  assert.match(awr, /degradationBanner\(r\.degradation\)/);
  assert.doesNotMatch(awr, /r\.ai_error/);
});

test("a degraded answer names its model-free next step, and an answered one does not", () => {
  const manual = { required: true, step: "slow_sql.raw_list", capability_id: "performance.slow_sql.read", model_free: true };
  const banner = degradationBanner({ status: "model_failed", local_result_available: false, reason_code: "timeout" }, manual);
  assert.equal(banner.hint, "可回到慢 SQL 列表按原始记录判断，不依赖模型。");
  assert.equal(degradationBanner({ status: "model_failed" }, { required: false, step: null })?.hint, null);
  assert.equal(degradationBanner({ status: "model_failed" }, { required: true, step: "unknown.step" })?.hint, null);
  assert.equal(degradationBanner({ status: "model_failed" }, null)?.hint, null);
  // An answered model never renders a manual handoff, even if one is passed by mistake.
  assert.equal(degradationBanner({ status: "ok" }, manual), null);
  assert.equal(manualHint({ required: true, step: "awr.report_read" }), "可直接阅读原始 AWR 报告，不依赖模型。");
  assert.equal(manualHint({ required: false, step: "awr.report_read" }), null);
});
