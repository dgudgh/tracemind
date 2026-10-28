import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@babel/parser";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("notification workflow exposes failed delivery retry and direct alert dispatch", () => {
  const api = read("../src/modules/workflowApi.js");
  const management = read("../src/WorkflowManagementPages.jsx");
  const alerts = read("../src/UnifiedAlertEventPage.jsx");

  assert.match(api, /retryNotifyLog/);
  assert.match(api, /sendAlertNotification/);
  assert.match(api, /\/notify\/logs\/\$\{logId\}\/retry/);
  assert.match(management, /失败原因/);
  assert.match(management, /重新发送/);
  assert.match(alerts, /sendAlertNotification/);
  assert.doesNotMatch(alerts, /createAction\(r, "notify"\)/);
});

test("alert inspection and diagnosis use one real centered ITSM ticket dialog", () => {
  const api = read("../src/modules/workflowApi.js");
  const drawer = read("../src/framework/integration/ItsmTicketDrawer.jsx");
  const alerts = read("../src/UnifiedAlertEventPage.jsx");
  const report = read("../src/InspectionReportDetailPage.jsx");
  const diagnosis = read("../src/DiagnosisCenterPage.jsx");

  assert.match(api, /createSourceTicket/);
  assert.match(drawer, /createSourceTicket/);
  assert.match(drawer, /工单创建成功/);
  assert.match(drawer, /<Modal/);
  assert.match(drawer, /\bcentered\b/);
  for (const page of [alerts, report, diagnosis]) {
    assert.match(page, /ItsmTicketDrawer/);
  }
});

test("ticket close API carries the source close decision", () => {
  const api = read("../src/modules/workflowApi.js");
  assert.match(api, /close_source: closeSource/);
});

test("alert delivery distinguishes success, partial failure, pending and unknown outcomes", async (t) => {
  const source = read("../src/UnifiedAlertEventPage.jsx");
  const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
  const component = ast.program.body.find((node) => node.declaration?.id?.name === "UnifiedAlertEventPage").declaration;
  const notify = component.body.body.flatMap((node) => node.declarations || [])
    .find((node) => node.id.name === "notifyEvent").init;
  for (const [delivery, level, expected] of [
    [{ sent_count: 2 }, "success", "共 2 条"],
    [{ failed_count: 2 }, "error", "失败 2 条"],
    [{ sent_count: 1, failed_count: 1 }, "warning", "失败 1 条"],
    [{ pending_count: 2 }, "warning", "待处理 2 条"],
    [{ unknown_count: 1 }, "warning", "结果未知 1 条"],
    [{ cancelled_count: 1 }, "warning", "已取消 1 条"],
    [{}, "warning", "没有新增投递"],
  ]) {
    await t.test(JSON.stringify(delivery), async () => {
      const messages = [];
      const loading = [];
      let reloads = 0;
      const run = vm.runInNewContext(`(${source.slice(notify.start, notify.end)})`, {
        setNotifyingId: (value) => loading.push(value),
        sendAlertNotification: async () => ({ delivery }),
        load: async () => { reloads += 1; },
        message: Object.fromEntries(["success", "warning", "error"].map((key) => [
          key, (text) => messages.push({ level: key, text }),
        ])),
      });
      await run({ id: 7 });
      assert.equal(messages.length, 1);
      assert.equal(messages[0].level, level);
      assert.ok(messages[0].text.includes(expected));
      assert.deepEqual(loading, [7, null]);
      assert.equal(reloads, 1);
    });
  }
});
