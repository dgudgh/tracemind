const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

// Use the real registry in an in-memory database; every browser API is intercepted.
async function main() {
  const root = path.resolve(__dirname, "..");
  const catalog = JSON.parse(execFileSync(path.join(root, ".venv-mac-primary/bin/python"), ["-c", `
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection.policy_service import list_policies
engine = create_engine('sqlite://')
DBInspectionPolicy.__table__.create(engine)
with sessionmaker(bind=engine)() as db:
    print(json.dumps(list_policies(db)))
`], { cwd: root, env: { ...process.env, PYTHONPATH: "backend", APP_MODE: "test", DATABASE_URL: "sqlite://" }, encoding: "utf8" }));
  const expectedKeys = ["oracle_10g", "oracle_noncdb", "oracle_cdb", "mysql_standard", "postgresql_standard", "sqlserver_standard", "host_resource", "host_windows", "daily_standard"];
  const visible = catalog.filter((row) => !row.legacy_template && row.policy_key !== "oracle_standard");
  assert.deepEqual(visible.map((row) => row.policy_key).sort(), [...expectedKeys].sort());
  const oracleProfiles = [
    ["oracle_10g", 53, 72, "dbcheck10g.sql"],
    ["oracle_noncdb", 56, 75, "dbcheck_noncdb.sql"],
    ["oracle_cdb", 56, 75, "dbcheck_cdb.sql"],
  ];
  for (const [key, sections, total, script] of oracleProfiles) {
    const row = catalog.find((item) => item.policy_key === key);
    assert.equal(row.item_config.length, total, key);
    assert.equal(new Set(row.item_config.map((item) => item.key)).size, total, key);
    assert.equal(row.runtime_summary.by_db_type.oracle.script_section_count, sections, key);
    assert.equal(row.runtime_summary.by_db_type.oracle.health_item_count, 19, key);
    assert.equal(row.executable_item_count, total, key);
    assert.equal(row.report_config.script_name, script, key);
    const scriptItems = row.item_config.filter((item) => item.source_key?.startsWith("oracle.script:"));
    assert.equal(scriptItems.length, sections, key);
    assert.equal(row.item_config.filter((item) => item.source_key?.startsWith("oracle.query:")).length, 19, key);
    assert.ok(scriptItems.every((item) => item.sql?.trim()), `${key}: missing SQL evidence`);
    for (const item of row.item_config) {
      if (item.source_key.startsWith("oracle.query:")) {
        assert.equal(item.source_key, `oracle.query:${item.key}`, key);
      } else {
        assert.equal(item.key, item.source_key, key);
        assert.equal(item.source_key, `oracle.script:${item.script_section}`, key);
        assert.ok(Number.isInteger(item.script_line) && item.script_line > 0, key);
      }
    }
  }
  for (const [key, count] of [["mysql_standard", 26], ["postgresql_standard", 19], ["sqlserver_standard", 23]]) {
    assert.equal(catalog.find((row) => row.policy_key === key).item_config.length, count, key);
  }
  const windows = catalog.find((row) => row.policy_key === "host_windows");
  assert.equal(windows.name, "主机巡检（Windows）");
  assert.equal(windows.report_config.host_platform, "windows");
  assert.equal(windows.report_config.script_name, "oscheck.ps1");
  const seedCount = catalog.length;
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "inspection-template-qa-"));
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(12000);
  const errors = [];
  const runs = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem("mesguard_token", "isolated-ui-test"));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    let body = { items: [] };
    if (pathname === "/api/v1/system/init/status") body = { initialized: true };
    if (pathname === "/api/v1/setup/license") body = { valid: true };
    if (pathname === "/api/v1/auth/current-user") body = { id: 1, username: "qa", role: "admin", source: "db" };
    if (pathname === "/api/v1/instances") body = { items: [{ id: 7, instance_name: "Oracle QA", db_type: "oracle", host: "192.0.2.7", port: 1521, enabled: 1 }] };
    if (pathname === "/api/v1/inspection/policies") body = { items: catalog };
    const clone = pathname.match(/^\/api\/v1\/inspection\/policies\/(\d+)\/clone$/);
    if (clone) {
      const source = catalog.find((row) => row.id === Number(clone[1]));
      const payload = request.postDataJSON();
      body = { ...structuredClone(source), ...payload, id: 100, read_only: false, legacy_template: false };
      catalog.push(body);
    }
    if (pathname === "/api/v1/inspection/oracle/7/run") {
      runs.push(request.postDataJSON());
      body = { task: { task_id: "qa-task", task_status: "success", template_key: runs.at(-1).policy_key || "oracle_noncdb", template_name: "QA", overall_score: 100 }, modules: [], findings: [], report: {} };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  async function menu(name) {
    const item = page.getByRole("menuitem", { name: new RegExp(name) }).first();
    if (!await item.isVisible()) await page.locator(".admin-topbar-left button").click();
    await item.click();
  }
  async function paint() {
    await page.waitForFunction(() => {
      const modal = document.querySelector(".ant-modal");
      return modal && modal.clientWidth && modal.getAnimations({ subtree: true }).every((a) => a.playState !== "running");
    });
  }
  async function checkCards() {
    const failures = await page.locator(".inspection-template-card").evaluateAll((cards) => cards.flatMap((card) => {
      const title = card.querySelector(".inspection-template-card-title").getBoundingClientRect();
      const actions = card.querySelector(".inspection-template-card-actions").getBoundingClientRect();
      const bounds = card.getBoundingClientRect();
      const overlap = title.left < actions.right && title.right > actions.left && title.top < actions.bottom && title.bottom > actions.top;
      return overlap || bounds.left < 0 || bounds.right > innerWidth + 1 ? [card.innerText] : [];
    }));
    assert.deepEqual(failures, []);
  }
  try {
    await page.goto(process.env.INSPECTION_QA_URL || "http://127.0.0.1:3300/");
    await menu("巡检");
    await menu("策略与模板");
    await menu("巡检模板");
    await page.locator(".inspection-template-card").first().waitFor();
    assert.equal(await page.locator(".inspection-template-card").count(), 9);
    const titles = await page.locator(".inspection-template-card-title").allTextContents();
    assert.deepEqual(titles, expectedKeys.map((key) => catalog.find((row) => row.policy_key === key).name));
    await page.locator(".ant-drawer-mask").waitFor({ state: "hidden" });
    await checkCards();
    await page.screenshot({ path: path.join(artifacts, "catalog-desktop.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    await checkCards();
    await page.screenshot({ path: path.join(artifacts, "catalog-mobile.png") });
    await page.setViewportSize({ width: 1440, height: 1000 });
    for (const [key, sections, total, script] of oracleProfiles) {
      const row = catalog.find((item) => item.policy_key === key);
      const card = page.locator(".inspection-template-card").filter({ hasText: row.name });
      assert.ok((await card.innerText()).includes(`数据 SQL 章节 ${sections} · 健康指标 19`));
      await card.getByRole("button", { name: "查看详情", exact: true }).click();
      const detail = page.getByRole("dialog");
      await detail.getByText(script, { exact: true }).waitFor();
      await paint();
      const detailRows = detail.locator("tbody tr[data-row-key]");
      assert.equal(await detailRows.count(), total);
      assert.deepEqual((await detailRows.evaluateAll((rows) => rows.map((item) => item.dataset.rowKey))).sort(), row.item_config.map((item) => item.key).sort());
      const sqlEvidence = await detailRows.evaluateAll((rows) => rows.map((item) => item.querySelectorAll("td")[11].textContent));
      assert.deepEqual(sqlEvidence.sort(), row.item_config.map((item) => item.sql || "-").sort());
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
        await paint();
        const bounds = await detail.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.width <= width && bounds.y >= 0 && bounds.y + bounds.height <= (width === 390 ? 845 : 1001), JSON.stringify(bounds));
        assert.ok(await detailRows.evaluateAll((rows) => rows.every((item) => item.getBoundingClientRect().height <= 120)), `${key}: oversized detail row`);
        await page.screenshot({ path: path.join(artifacts, `${key}-${width}.png`) });
      }
      await detail.getByRole("button", { name: "Close", exact: true }).click();
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    const cdb = page.locator(".inspection-template-card").filter({ hasText: "Oracle CDB / PDB 巡检模板" });
    await cdb.getByRole("button", { name: "查看详情", exact: true }).click();
    let dialog = page.getByRole("dialog");
    await dialog.getByText("dbcheck_cdb.sql", { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await paint();
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.width <= 390 && bounds.y >= 0 && bounds.y + bounds.height <= 845, JSON.stringify(bounds));
    await page.screenshot({ path: path.join(artifacts, "profile-mobile.png") });
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await cdb.getByRole("button", { name: "更多操作", exact: true }).click();
    await page.getByRole("menuitem", { name: "复制模板", exact: true }).click();
    await page.getByText("Oracle CDB / PDB 巡检模板 副本", { exact: true }).waitFor();
    assert.equal(catalog.length, seedCount + 1);
    const cloned = catalog.at(-1);
    const original = catalog.find((row) => row.policy_key === "oracle_cdb");
    assert.deepEqual(cloned.item_config, original.item_config);
    assert.deepEqual(cloned.report_config, original.report_config);
    await menu("巡检策略");
    await page.locator(".inspection-policy-card").first().waitFor();
    for (const [key, sections, total] of oracleProfiles) {
      const row = catalog.find((item) => item.policy_key === key);
      const policy = page.locator(".inspection-policy-card").filter({ has: page.getByText(row.name, { exact: true }) });
      const metric = policy.locator(".inspection-policy-card-metric").filter({ hasText: "配置检查项" });
      assert.equal(await metric.locator("strong").innerText(), String(total));
      assert.ok((await policy.innerText()).includes(`数据 SQL 章节 ${sections} · 健康指标 19`));
    }
    await page.screenshot({ path: path.join(artifacts, "policies-desktop.png") });
    await menu("一键巡检");
    const select = page.locator(".ant-select").filter({ hasText: "自动匹配模板" });
    await select.waitFor();
    const automaticResponse = page.waitForResponse((response) => response.url().endsWith("/inspection/oracle/7/run"));
    await page.getByRole("button", { name: /开始巡检/ }).click();
    await automaticResponse;
    await page.waitForFunction(() => document.body.innerText.includes("QA"));
    assert.deepEqual(runs[0], {});
    await select.click();
    await page.locator(".ant-select-dropdown:visible").getByText("Oracle CDB / PDB 巡检模板 副本", { exact: true }).click();
    const explicitResponse = page.waitForResponse((response) => response.url().endsWith("/inspection/oracle/7/run"));
    await page.getByRole("button", { name: /开始巡检/ }).click();
    await explicitResponse;
    assert.equal(runs[1].policy_key, cloned.policy_key);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: "passed", visible_seed_templates: 9, oracle_items: [72, 75, 75], mocked_apis: true, real_inspections: 0, artifacts }));
  } catch (error) {
    console.error((await page.locator("body").innerText()).slice(0, 3500));
    await page.screenshot({ path: path.join(artifacts, "failure.png") });
    console.error(artifacts);
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
