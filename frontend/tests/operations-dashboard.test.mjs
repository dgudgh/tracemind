import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const pageSource = await readFile(new URL("../src/DatabaseAggregatePages.jsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/apiClients.js", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("dashboard does not infer health when stats are missing or unknown", () => {
  const tone = pageSource.slice(pageSource.indexOf("function dashboardTone("), pageSource.indexOf("function dbTypeText("));
  const decisions = pageSource.slice(pageSource.indexOf("  const healthTone ="), pageSource.indexOf("  const fallbackKpis ="));
  for (const status of [undefined, "unknown", "future", "failed"]) {
    const result = vm.runInNewContext(`${tone}\n${decisions}\n({ healthTone, healthTitle, healthAdvice })`, {
      dashStats: status ? { platform_status: status } : undefined,
      capacityCritical: 0, todayRiskCount: 0, disabledCount: 0,
    });
    assert.equal(result.healthTone, "unknown");
    assert.equal(result.healthTitle, "平台状态待确认");
    assert.match(result.healthAdvice, /无法判断/);
  }
  const healthy = vm.runInNewContext(`${tone}\n${decisions}\n({ healthTone, healthTitle })`, {
    dashStats: { platform_status: "healthy" }, capacityCritical: 0, todayRiskCount: 0, disabledCount: 0,
  });
  assert.equal(healthy.healthTone, "normal");
  assert.equal(healthy.healthTitle, "平台运行平稳");
});

test("operations dashboard renders traceable KPIs, SLO evidence, work queue and risk ranking", () => {
  assert.match(apiSource, /system\/dashboard-stats/);
  assert.match(pageSource, /platform_status/);
  assert.match(pageSource, /slo_cards/);
  assert.match(pageSource, /sample_count/);
  assert.match(pageSource, /work_queue/);
  assert.match(pageSource, /risk_ranking/);
  assert.match(pageSource, /detail_page_key/);
  assert.match(pageSource, /运行目标与证据/);
  assert.match(pageSource, /未验证/);
});

test("operations dashboard keeps six KPIs and SLO cards responsive", () => {
  assert.match(styleSource, /\.workbench-hero-metrics\s*\{[^}]*repeat\(3,/s);
  assert.match(styleSource, /\.workbench-slo-grid\s*\{[^}]*repeat\(6,/s);
  assert.match(styleSource, /@media \(max-width: 1180px\)[\s\S]*?\.workbench-slo-grid\s*\{[^}]*repeat\(3,/);
  assert.match(styleSource, /@media \(max-width: 820px\)[\s\S]*?\.workbench-slo-grid\s*\{[^}]*repeat\(2,/);
});
