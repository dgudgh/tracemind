import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panel = readFileSync(new URL("../src/SlowSqlAIAnalysisPanel.jsx", import.meta.url), "utf8");
const page = readFileSync(new URL("../src/OracleSlowSqlPage.jsx", import.meta.url), "utf8");

test("AI tab carries exact cursor identity and requires explicit model context approval", () => {
  assert.match(page, /label: "AI 分析"/);
  for (const field of ["sql_id", "child_number", "con_id", "plan_hash_value"]) assert.ok(panel.includes(`${field}: row.${field}`));
  assert.match(panel, /provider_config_id: provider\.provider_config_id/);
  assert.match(panel, /allow_model_context: approved/);
  assert.match(panel, /disabled=\{!approved \|\| !provider\?\.configured \|\| !identityComplete\}/);
  assert.ok(!panel.includes("sql_text: row.sql_text"));
});

test("AI progress reflects persistence and result rendering escapes model text", () => {
  assert.match(panel, /result\?\.persisted \? 3 : 2/);
  assert.match(panel, /result\.error/);
  assert.match(panel, /model_result/);
  assert.match(panel, /待验证假设/);
  assert.match(panel, /后端可能仍在运行/);
  assert.ok(!panel.includes("dangerouslySetInnerHTML"));
  assert.ok(!panel.includes("percent="));
});

test("AI lifecycle reloads durable history and ignores unmounted responses", () => {
  assert.match(panel, /listSlowSqlAiReports\(instanceId, row\.sql_id\)/);
  assert.match(panel, /alive\.current = false/);
  assert.match(panel, /request\.current\?\.abort\(\)/);
  assert.match(panel, /if \(alive\.current\) \{ setLoading\(false\); await refresh\(\); \}/);
});
