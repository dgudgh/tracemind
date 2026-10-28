import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createServer } from "vite";
import { matchingSlowSqlSnapshot, slowSqlConclusion, slowSqlFields, slowSqlReportHtml, slowSqlSnapshot } from "../src/SlowSqlResult.js";

const sample = { category: "top_avg", rows: [
  { sql_id: "abc", executions: 2, avg_elapsed_seconds: 3.25, avg_cpu_seconds: 1.5, avg_buffer_gets: 20, avg_disk_reads: 0, sql_text: "select '<script>alert(1)</script>' from dual" },
  { sql_id: "abc", executions: 1, avg_elapsed_seconds: null, sql_text: "select 2 from dual" },
] };

test("slow SQL report distinguishes cursor count, SQL ids and cumulative counters", () => {
  const snapshot = slowSqlSnapshot(sample, 1, { hours: 3 }, "2026-09-10T02:30:00Z");
  const report = slowSqlConclusion(snapshot);
  assert.match(report.lines.join(" "), /2 条SQL 游标记录.*1 个 SQL ID/);
  assert.match(report.lines.join(" "), /3.25 秒/);
  assert.match(report.scope, /3 小时.*累计值.*不是该时间段的增量/);
  assert.match(report.limitation, /未执行 AI/);
  assert.equal(matchingSlowSqlSnapshot(snapshot, 2), null);
  assert.equal(matchingSlowSqlSnapshot(snapshot, "1"), snapshot);
});

test("fields match returned average values, never pretend missing totals exist", () => {
  const fields = slowSqlFields(sample.rows).map((field) => field.key);
  assert.ok(fields.includes("avg_cpu_seconds"));
  assert.ok(fields.includes("avg_buffer_gets"));
  assert.ok(!fields.includes("cpu_seconds"));
  assert.ok(!fields.includes("buffer_gets"));
});

test("empty and unavailable responses are not health conclusions", () => {
  assert.throws(() => slowSqlSnapshot({ status: "degraded", message: "连接失败", rows: [] }, 1), /连接失败/);
  assert.throws(() => slowSqlSnapshot({}, 1), /记录列表/);
  const empty = slowSqlConclusion(slowSqlSnapshot({ rows: [] }, 1));
  assert.match(empty.lines.join(" "), /不能据此判断数据库无慢 SQL/);
  assert.match(slowSqlConclusion(slowSqlSnapshot({ category: "waiting", rows: [] }, 1)).scope, /不是历史时间窗口/);
});

test("export contains all original rows and escapes SQL rather than executing it", () => {
  const html = slowSqlReportHtml(slowSqlSnapshot(sample, 1), "test<db>");
  assert.match(html, /test&lt;db&gt;/);
  assert.ok(!html.includes("<script>"));
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /select 2 from dual/);
  assert.match(html, /未归档到巡检报告/);
});

test("assistant keeps all 50 rows beyond preview limit for the exact instance", async () => {
  const vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  try {
    const actions = await vite.ssrLoadModule("/src/modules/assistantActions.js");
    const rows = Array.from({ length: 50 }, (_, i) => ({ sql_id: `sql${i}`, avg_elapsed_seconds: i, sql_text: "x".repeat(1000) }));
    const instance = { id: 7, db_type: "oracle", host: "localhost", port: 1521 };
    const request = { targetKey: "perf.slowsql", targetName: "慢 SQL", instanceId: 7, command: "分析慢SQL" };
    const completed = await actions.runAssistantActionProgress({
      request, instance,
      execute: async () => ({
        __assistantResult: true,
        data: { status: "success", count: 50, rows },
        summary: "", notice: "",
        executionResult: { engine: "oracle", instanceId: 7, slowSql: { data: { rows }, instanceId: 7 } },
      }),
    });
    assert.equal(completed.status, "success");
    assert.match(completed.preview, /结果已截断/);
    assert.equal(completed.executionResult.slowSql.data.rows.length, 50);
    assert.equal(completed.executionResult.slowSql.data.rows[49].sql_id, "sql49");
    assert.equal(completed.executionResult.slowSql.instanceId, 7);
    const failed = await actions.runAssistantActionProgress({
      request, instance,
      execute: async () => ({ __assistantResult: true, data: { status: "degraded", message: "network unavailable", rows: [] } }),
    });
    assert.equal(failed.status, "failed");
    assert.match(failed.error, /network unavailable/);
    assert.ok(!failed.executionResult);
    const partial = await actions.runAssistantActionProgress({
      request, instance, execute: async () => ({ status: "partial", message: "plan missing" }),
    });
    assert.equal(partial.status, "partial");
    assert.match(partial.summary, /部分完成/);
  } finally {
    await vite.close();
  }
});

test("page loads on entry and forwards complete assistant result through navigation", () => {
  const page = readFileSync(new URL("../src/OracleSlowSqlPage.jsx", import.meta.url), "utf8");
  const diagnosis = readFileSync(new URL("../src/DiagnosisCenterPage.jsx", import.meta.url), "utf8");
  const router = readFileSync(new URL("../src/AppPageRouter.jsx", import.meta.url), "utf8");
  assert.match(page, /useEffect\(\(\) => \{[\s\S]*getOracleSlowSqlRealtime/);
  assert.match(page, /controller\.abort\(\)/);
  assert.match(page, /!disposed/);
  assert.match(page, /查询结论/);
  assert.match(page, /collectSlowSql\(instanceId, \{ hours \}\)/);
  assert.match(page, /token !== collectionRequest\.current/);
  assert.match(diagnosis, /onNavigate\?\.\([^;]*assistantActionTask\?\.executionResult\)/);
  assert.match(router, /executionResult=\{/);
});

test("collection passes the selected hours to the backend", async () => {
  const vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  const originalFetch = globalThis.fetch;
  try {
    const api = await vite.ssrLoadModule("/src/modules/inspectionApi.js");
    let called;
    globalThis.fetch = async (url, options) => {
      called = { url: String(url), method: options.method };
      return new Response(JSON.stringify({ status: "success", saved_count: 3 }), { status: 200 });
    };
    await api.collectSlowSql(7, { hours: 3 });
    assert.match(called.url, /\/slow-sql\/collect\/7\?hours=3$/);
    assert.equal(called.method, "POST");
  } finally {
    globalThis.fetch = originalFetch;
    await vite.close();
  }
});

test("SQL detail keeps child/container/plan identity and abort signal", async () => {
  const vite = await createServer({ root: new URL("..", import.meta.url).pathname, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  const originalFetch = globalThis.fetch;
  try {
    const api = await vite.ssrLoadModule("/src/modules/inspectionApi.js");
    let called;
    globalThis.fetch = async (url, options) => {
      called = { url: String(url), options };
      return new Response(JSON.stringify({ status: "success" }), { status: 200 });
    };
    const controller = new AbortController();
    await api.getOracleSqlDetail(7, "abc", { child_number: 0, con_id: 12, plan_hash_value: 42 }, { signal: controller.signal });
    const params = new URL(called.url, "http://localhost").searchParams;
    assert.equal(params.get("sql_id"), "abc");
    assert.equal(params.get("child_number"), "0");
    assert.equal(params.get("con_id"), "12");
    assert.equal(params.get("plan_hash_value"), "42");
    assert.equal(called.options.signal, controller.signal);
  } finally {
    globalThis.fetch = originalFetch;
    await vite.close();
  }
});
