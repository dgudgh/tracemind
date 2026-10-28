import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../src/UnifiedReportCenterPage.jsx", import.meta.url), "utf8");

test("report totals are read after source synchronization commits", () => {
  const load = source.slice(source.indexOf("const loadCatalog"), source.indexOf("const loadSchedules"));
  assert.match(load, /await getReportCatalog\([\s\S]*await getReportCatalogStats\(\)/);
  assert.ok(!load.includes("Promise.all"));
});

test("initial schedule query cannot hide the catalog loading state", () => {
  const schedules = source.slice(source.indexOf("const loadSchedules"), source.indexOf("const loadRuns"));
  assert.ok(!schedules.includes("setLoading("));
  assert.match(schedules, /setSchedulesLoading\(false\)/);
  assert.match(source, /if \(activeTab === "schedules" \|\| activeTab === "subscriptions"\) loadSchedules\(\)/);
  assert.match(source, /if \(activeTab === "catalog"\) await loadCatalog\(false\)/);
});

test("report switching discards stale responses and remounts the hash field", () => {
  assert.match(source, /const token = \+\+detailRequest\.current/);
  assert.match(source, /token === detailRequest\.current\) setDetail\(response\)/);
  assert.match(source, /<Modal key=\{detail\?\.id \?\? "closed"\}/);
  assert.match(source, /<Text key=\{detail\.content_sha256\}/);
  assert.match(source, /const closeDetail = \(\) => \{\s*detailRequest\.current \+= 1/);
});

test("unloaded or failed statistics are not displayed as zero reports", () => {
  assert.match(source, /\[stats, setStats\] = useState\(null\)/);
  assert.match(source, /stats\?\.total \?\? "待加载"/);
  assert.match(source, /stats\?\.signed \?\? "待加载"/);
  assert.match(source, /报告目录加载失败/);
  assert.ok(!source.includes("stats.total || catalog.length"));
});

test("loaded archive totals distinguish absent zero-count categories from pending data", () => {
  for (const [label, field] of [["在线", "active"], ["已归档", "archived"]]) {
    const expression = source.match(new RegExp(`<Metric label="${label}" value=\\{([^}]+)\\}`))?.[1];
    assert.ok(expression);
    const value = (stats) => runInNewContext(expression, { stats });
    assert.equal(value(null), "待加载");
    assert.equal(value({}), "待加载");
    assert.equal(value({ by_archive_state: {} }), 0);
    assert.equal(value({ by_archive_state: { [field]: 0 } }), 0);
    assert.equal(value({ by_archive_state: { [field]: 78 } }), 78);
  }
});
