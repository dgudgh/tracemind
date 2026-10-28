const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

// No server, login storage, or database: bundle only this page in memory and intercept all traffic.
async function main() {
  const root = path.resolve(__dirname, "..");
  const frontend = path.join(root, "frontend");
  const entry = path.join(frontend, "sql-profile-qa-entry.jsx");
  const mockApi = "\0sql-profile-qa-api";
  const { build } = await import(pathToFileURL(path.join(frontend, "node_modules/vite/dist/node/index.js")));
  const bundle = await build({
    configFile: false, root: frontend, logLevel: "error",
    define: { "process.env.NODE_ENV": JSON.stringify("development") },
    plugins: [{
      name: "isolated-sql-profile-qa",
      enforce: "pre",
      resolveId(id) {
        if (id === entry) return entry;
        if (/performanceApi(?:\.js)?$/.test(id)) return mockApi;
      },
      load(id) {
        if (id === mockApi) return `
          export const getSqlProfile = (id, query) => window.qa.request('get', id, query);
          export const collectSqlProfile = (id) => window.qa.request('post', id);
          export const getSqlProfileDetail = (sql, id, profile_id) => window.qa.request('detail', id, { sql, profile_id });
          export const compareSqlPlans = () => Promise.resolve({});
        `;
        if (id === entry) return `
          import React, { useState } from 'react';
          import { createRoot } from 'react-dom/client';
          import SQLProfileCenterPage from './src/SQLProfileCenterPage.jsx';
          import 'antd/dist/reset.css';
          const instances = [
            { id: 1, db_type: 'oracle', instance_name: 'Oracle QA' },
            { id: 2, db_type: 'mysql', instance_name: 'MySQL QA' },
            { id: 3, db_type: 'postgresql', instance_name: 'PostgreSQL QA' },
            { id: 4, db_type: 'sqlserver', instance_name: 'SQL Server QA' },
          ];
          window.qa = {
            calls: [], pending: [], hold: null, readError: null, outcome: { status: 'success', count: 60 },
            history: false,
            async request(kind, id, query) {
              this.calls.push({ kind, id, query });
              if (this.hold === kind + ':' + id) {
                return new Promise((resolve, reject) => this.pending.push({ kind, id, resolve, reject }));
              }
              if (kind === 'post') {
                if (this.outcome.http) throw Object.assign(new Error(this.outcome.message), { status: this.outcome.http });
                return this.outcome;
              }
              if (kind === 'get') {
                if (this.readError) throw new Error(this.readError);
                const exists = this.history || id !== 1;
                const items = exists && !query?.schema ? [{ instance_id: id, id, sql_id: 'SQL_' + id,
                  sql_text: 'select 1', schema_name: 'QA', capture_time: '2026-09-08T10:00:00', source: id > 2 ? 'awr' : 'live' }] : [];
                if (this.duplicates && id === 1 && items.length) items.push({ ...items[0], id: 101, schema_name: 'QA_B', plan_hash: 'plan-b' });
                return { items,
                  total_stored: exists ? 60 : 0, latest_capture_time: exists ? '2026-09-08T10:00:00' : null,
                  collection_capability: { live_supported: id <= 2 }, read_only: true };
              }
              return { profile: { id: query.profile_id, sql_id: query.sql, instance_id: id, schema_name: 'PROFILE_' + query.profile_id },
                plan: null, plans: [], plan_status: 'not_collected',
                risks: [{ severity: 'warning', risk_type: 'measured_elapsed', reason: 'Measured elapsed 5000ms' }] };
            },
            release(value) { this.hold = null; this.pending.splice(0).forEach((p) => p.resolve(value)); },
          };
          function Harness() {
            const [id, setId] = useState(1);
            return <main style={{ padding: 16, minWidth: 0 }}>
              <select aria-label="QA instance" value={id} onChange={(e) => setId(Number(e.target.value))}>
                <option value="0">None</option>
                {instances.map((instance) => <option key={instance.id} value={instance.id}>{instance.instance_name}</option>)}
              </select>
              <SQLProfileCenterPage instances={instances} selectedInstanceId={id || null} />
            </main>;
          }
          createRoot(document.getElementById('root')).render(<React.StrictMode><Harness /></React.StrictMode>);
        `;
      },
    }],
    build: { write: false, minify: false, lib: { entry, name: "SqlProfileQA", formats: ["iife"] } },
  });
  const output = (Array.isArray(bundle) ? bundle[0] : bundle).output;
  const javascript = output.filter((item) => item.type === "chunk").map((item) => item.code).join("\n");
  const css = output.filter((item) => item.fileName.endsWith(".css")).map((item) => item.source).join("\n");
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), "sql-profile-qa-"));
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [], unexpectedRequests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = route.request().url();
    if (url === "https://sql-profile-qa.test/") return route.fulfill({ contentType: "text/html; charset=utf-8", body:
      `<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body><div id="root"></div><script src="/qa.js"></script></body></html>` });
    if (url === "https://sql-profile-qa.test/qa.js") return route.fulfill({ contentType: "application/javascript; charset=utf-8", body: javascript });
    unexpectedRequests.push(url);
    return route.abort();
  });
  const collect = () => page.getByRole("button", { name: "采集 SQL 画像", exact: true }).click();
  const refresh = () => page.getByRole("button", { name: "刷新", exact: true }).click();
  const ready = () => page.waitForFunction(() => !document.querySelector(".ant-btn-loading, .ant-spin-spinning"));
  const set = (patch) => page.evaluate((value) => Object.assign(window.qa, value), patch);
  const switchTo = (id) => page.getByLabel("QA instance").selectOption(String(id));
  const has = (text) => page.getByText(text, { exact: true }).first().waitFor();
  try {
    await page.goto("https://sql-profile-qa.test/");
    await has("暂无存档数据");
    assert.equal(await page.evaluate(() => qa.calls.filter((c) => c.kind === "post").length), 0);

    await set({ outcome: { http: 502, message: "ORA-01031: insufficient privileges" } });
    await collect(); await ready();
    await has("ORA-01031: insufficient privileges");
    assert.equal(await page.locator(".ant-alert-success").count(), 0);

    await set({ history: true, outcome: { status: "partial", count: 40, retained_count: 60, errors: ["disk_reads failed"], message: "部分来源不可用" } });
    await collect(); await has("SQL_1"); await ready();
    assert.equal(await page.locator(".ant-alert-warning").count(), 1);
    assert.match(await page.locator(".ant-alert-warning").innerText(), /disk_reads failed/);
    assert.equal(await page.locator(".ant-alert-success").count(), 0);

    await page.getByPlaceholder("Schema", { exact: true }).fill("NO_MATCH");
    await page.getByPlaceholder("Schema", { exact: true }).press("Enter");
    await has("当前筛选无匹配");
    await set({ outcome: { status: "empty", count: 0, retained_count: 60, errors: [], message: "本次查询返回 0 行" } });
    await collect(); await ready();
    assert.equal(await page.getByPlaceholder("Schema", { exact: true }).inputValue(), "NO_MATCH");
    assert.equal(await page.evaluate(() => qa.calls.filter((c) => c.kind === "get").at(-1).query.schema), "NO_MATCH");
    await has("当前筛选无匹配，已有存档数据");
    await page.screenshot({ path: path.join(artifacts, "filtered-empty-desktop.png") });

    await page.getByPlaceholder("Schema", { exact: true }).fill("");
    await has("SQL_1");
    await set({ readError: "archive unavailable" });
    await refresh(); await has("请求失败：archive unavailable");
    assert.equal(await page.getByText("SQL_1", { exact: true }).count(), 0);
    await set({ readError: null, hold: "get:1" });
    await refresh();
    await page.waitForFunction(() => qa.pending.length > 0);
    await switchTo(2); await has("SQL_2");
    await page.evaluate(() => qa.release({ items: [{ instance_id: 1, sql_id: "STALE_SQL" }], total_stored: 1 }));
    await ready();
    assert.equal(await page.getByText("STALE_SQL", { exact: true }).count(), 0);

    await switchTo(1); await has("SQL_1");
    await set({ hold: "post:1" });
    await collect();
    await page.waitForFunction(() => qa.pending.length > 0);
    await switchTo(2); await has("SQL_2");
    const reads = await page.evaluate(() => qa.calls.filter((c) => c.kind === "get" && c.id === 1).length);
    await page.evaluate(() => qa.release({ status: "success", count: 60 }));
    await ready();
    assert.equal(await page.evaluate(() => qa.calls.filter((c) => c.kind === "get" && c.id === 1).length), reads);
    assert.equal(await page.locator(".ant-alert-success").count(), 0);

    await set({ outcome: { http: 422, message: "collector unsupported" } });
    await collect(); await ready();
    await has("此实例不支持实时采集");
    assert.equal(await page.getByRole("button", { name: "采集 SQL 画像", exact: true }).isDisabled(), true);
    for (const id of [3, 4]) {
      await switchTo(id); await has(`SQL_${id}`);
      await has("此实例不支持实时采集");
      assert.equal(await page.getByRole("button", { name: "采集 SQL 画像", exact: true }).isDisabled(), true);
    }

    await switchTo(1); await has("SQL_1");
    await set({ outcome: { status: "failed", count: 0, retained_count: 60, errors: ["ORA-01031:" + "permission_denied_".repeat(25)], message: "采集器失败" } });
    await collect(); await ready();
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await ready();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `overflow at ${width}`);
      const table = await page.locator(".ant-table-content").evaluate((element) => ({ width: element.clientWidth, scroll: element.scrollWidth }));
      assert.ok(table.scroll >= table.width);
      await page.screenshot({ path: path.join(artifacts, `failed-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await set({ duplicates: true });
    await refresh(); await has("QA_B");
    const detailButtons = page.locator(".ant-table-tbody").getByRole("button", { name: /详情/ });
    assert.equal(await detailButtons.count(), 2);
    for (const [index, profileId] of [[1, 101], [0, 1]]) {
      await detailButtons.nth(index).click();
      const dialog = page.getByRole("dialog");
      await dialog.getByText(`PROFILE_${profileId}`, { exact: true }).waitFor();
      assert.deepEqual(await page.evaluate(() => qa.calls.filter((c) => c.kind === "detail").at(-1)),
        { kind: "detail", id: 1, query: { sql: "SQL_1", profile_id: profileId } });
      await dialog.getByRole("tab", { name: "执行计划分析", exact: true }).click();
      await dialog.getByText("暂无执行计划", { exact: true }).waitFor();
      assert.equal(await dialog.getByText("执行计划树", { exact: true }).count(), 0);
      await dialog.getByRole("tab", { name: "风险分析", exact: true }).click();
      await dialog.getByText("Measured elapsed 5000ms", { exact: true }).waitFor();
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
      await dialog.waitFor({ state: "hidden" });
    }
    await switchTo(0); await has("请选择实例后查看 SQL 画像中心");
    assert.equal(await page.getByRole("button", { name: "采集 SQL 画像", exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "刷新", exact: true }).isDisabled(), true);
    assert.deepEqual(errors, []);
    assert.deepEqual(unexpectedRequests, []);
    console.log(JSON.stringify({ status: "passed", mocked_apis: true, services_started: 0, real_database_calls: 0,
      login_changes: 0, viewports: [1440, 390], artifacts }));
  } catch (error) {
    await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
    console.error({ artifacts, errors, unexpectedRequests, body: (await page.locator("body").innerText()).slice(0, 2500) });
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
