import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "..");
const backendRoot = path.resolve(frontendRoot, "../backend");
const declarations = spawnSync("python3", ["-c",
  "import json, runpy, sys; print(json.dumps(runpy.run_path(sys.argv[1])['CAPABILITY_DECLARATIONS']))",
  path.resolve(backendRoot, "app/services/capability_declarations.py"),
], { encoding: "utf8", env: { ...process.env, PYTHONPATH: backendRoot } });
assert.equal(declarations.status, 0, declarations.stderr);
const catalog = {
  schema: "capability.registry/v1",
  items: JSON.parse(declarations.stdout).map((item) => ({
    ...item, definition_version: item.version, enabled: true,
    availability: "available", unavailable_reason: null,
    authorized_engines: [...item.engines], plan_executable: item.execution === "server",
  })),
};
const diagnosisPageSource = readFileSync(
  path.join(frontendRoot, "src", "DiagnosisCenterPage.jsx"),
  "utf8",
);
const menuSource = readFileSync(path.join(frontendRoot, "src", "menuConfig.jsx"), "utf8");
const actionSource = readFileSync(
  path.join(frontendRoot, "src", "modules", "assistantActions.js"),
  "utf8",
);

let vite;
let actions;

test.before(async () => {
  vite = await createServer({
    root: frontendRoot,
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
  });
  actions = await vite.ssrLoadModule("/src/modules/assistantActions.js");
});

test.after(async () => {
  await vite?.close();
});

test("assistant distinguishes navigation, execution, and parameterized actions", () => {
  assert.equal(
    actions.resolveAssistantActionRequest(
      "帮我打开资产中心",
      { target_key: "asset.center", target_name: "资产管理" },
    ),
    null,
  );

  assert.equal(actions.isInstanceProvisioningCommand("添加数据库实例"), true);
  assert.equal(
    actions.resolveAssistantActionRequest(
      "添加数据库实例",
      { target_key: "asset.center", target_name: "资产管理" },
    ),
    null,
  );

  const execution = actions.resolveAssistantActionRequest(
    "帮我自动识别并刷新资产拓扑",
    { target_key: "asset.center", target_name: "资产管理" },
  );
  assert.equal(execution.targetKey, "asset.center");
  assert.equal(execution.requiresParameters, false);

  const parameterized = actions.resolveAssistantActionRequest(
    "修改 Zabbix 配置",
    { target_key: "integration.zabbix", target_name: "Zabbix 联动" },
  );
  assert.equal(parameterized.targetKey, "integration.zabbix");
  assert.equal(parameterized.requiresParameters, true);
});

test("assistant progress runner reports success without leaving the current page", async () => {
  const progress = [];
  const request = {
    targetKey: "asset.center",
    targetName: "资产管理",
    title: "识别并汇总资产",
    command: "帮我查询资产",
    requiresInstance: false,
  };
  const result = await actions.runAssistantActionProgress({
    request,
    execute: async () => ({
      __assistantResult: true,
      data: { total: 8, items: [{ id: 1 }] },
      summary: "资产汇总完成",
      metrics: [{ label: "主机", value: 8 }],
    }),
    onProgress: (state) => progress.push(state),
  });

  assert.equal(result.status, "success");
  assert.equal(result.percent, 100);
  assert.equal(result.summary, "资产汇总完成");
  assert.deepEqual(result.metrics, [{ label: "主机", value: 8 }, { label: "返回记录", value: 8 }]);
  assert.equal(progress[0].status, "running");
  assert.equal(progress.at(-1).steps.every((step) => step.status === "success"), true);
});

test("assistant progress runner exposes missing parameters and missing instances", async () => {
  const parameterResult = await actions.runAssistantActionProgress({
    request: {
      targetKey: "system.params",
      targetName: "系统参数",
      title: "修改系统参数",
      command: "修改系统参数",
      requiresParameters: true,
      requiresInstance: false,
    },
    execute: async () => assert.fail("parameterized action must not execute"),
  });
  assert.equal(parameterResult.status, "needs_input");
  assert.match(parameterResult.error, /补充参数/);

  const instanceResult = await actions.runAssistantActionProgress({
    request: {
      targetKey: "perf.sql",
      targetName: "SQL 画像",
      title: "采集 SQL 画像",
      command: "分析 SQL",
      requiresParameters: false,
      requiresInstance: true,
    },
    execute: async () => assert.fail("instance action must not execute without an instance"),
  });
  assert.equal(instanceResult.status, "needs_input");
  assert.match(instanceResult.error, /选择数据库实例/);
});

test("assistant progress runner terminates timed out actions", async () => {
  const result = await actions.runAssistantActionProgress({
    request: {
      targetKey: "home.dashboard",
      targetName: "工作台",
      title: "加载平台运行总览",
      command: "查询工作台",
      requiresInstance: false,
    },
    execute: () => new Promise((resolve) => setTimeout(() => resolve({}), 40)),
    timeoutMs: 5,
  });

  assert.equal(result.status, "failed");
  assert.match(result.error, /超过 1 秒/);
});

test("assistant recognizes visible leaf targets without promising they are executable", () => {
  const leafKeys = [...menuSource.matchAll(/\{\s*key:\s*"([^"]+)"(?:(?!\{\s*key:)[\s\S])*?\bdata:\s*"[^"]+"/g)]
    .map((match) => match[1])
    .filter((key) => !key.includes(" "));
  // 这些页面不走"助手页面清单"这条路：
  // - diagnose.smart / inspect.run 有专门入口；
  // - platform.engines 是只读引擎目录页，只调用 /engine-catalog，没有对应能力声明，
  //   助手只能把它当普通页面导航，不能承诺执行。加进 TARGET_TITLES 会让下面的
  //   边界测试把它当可执行目标去跑，反而制造出"能执行"的假承诺。
  const dedicated = new Set(["diagnose.smart", "inspect.run", "platform.engines"]);
  const targetTitleBlock = actionSource.slice(
    actionSource.indexOf("const TARGET_TITLES = {"),
    actionSource.indexOf("const SUPPORTED_TARGETS"),
  );
  const coverage = new Set([...targetTitleBlock.matchAll(/^\s*"([^"]+)":/gm)].map((match) => match[1]));
  const missing = [...new Set(leafKeys)].filter((key) => !dedicated.has(key) && !coverage.has(key));

  assert.deepEqual(missing, []);
});

const SERVER_TARGETS = new Set([
  "inspect.overview", "inspect.tasks", "inspect.policies", "inspect.templates", "ops.matrix",
  // C13 batch 1 live-engine readers.
  "ops.overview", "ops.space", "ops.log", "ops.session", "perf.lock", "perf.slowsql",
  // C13 batch 2 platform readers.
  "home.dashboard", "perf.overview", "backup.overview", "backup.report", "backup.policy", "ops.ha",
  // C13 batch 3 inventories, catalogues and stored integration reads.
  "asset.center", "report.unified", "integration.zabbix", "integration.prometheus",
  "rescue.cases", "rescue.tools", "ops.governance.objectStorage",
  // C13 batch 4 platform/integration configuration readers.
  "system.params", "integration.notify", "integration.itsm", "rescue.overview",
  "system.user", "monitor.alert.db", "backup.platform", "rescue.agents",
  "system.aiEvaluation", "diagnose.incidents", "protection.automation", "rescue.tasks",
  "system.ai", "ops.governance", "system.identity", "system.schedule",
  "monitor.alert.unified", "perf.trend", "report.alert",
  "diagnose.snapshot.agents", "diagnose.snapshot.policies", "monitor.observability.grafana", "ops.configGovernance", "diagnose.investigation",
  "ops.security", "ops.configGovernance", "diagnose.investigation", "perf.awr.history",
  "perf.capacity", "perf.table", "perf.index", "perf.workload", "perf.sql",
  "backup.plan", "backup.restore", "monitor.observability", "monitor.observability.apm", "ops.object", "perf.awr.history",
  // C14 approval-gated metric collection.
  "perf.baseline",
]);

// Declared, bound to a page action, but deliberately not server-executable (no evidence model).
const UNWIRED_TARGETS = new Set(["perf.optimization"]);
// A client branch that cannot be prepared from the generic command (needs a source/window).
const PARAMETER_REQUIRED_TARGETS = new Set(["monitor.observability.logs"]);

test("visible targets respect client, explicit server plan, and semantic-gap boundaries", async () => {
  const targetTitleBlock = actionSource.slice(
    actionSource.indexOf("const TARGET_TITLES = {"),
    actionSource.indexOf("const SUPPORTED_TARGETS"),
  );
  const targets = [...targetTitleBlock.matchAll(/^\s*"([^"]+)":\s*"([^"]+)"/gm)]
    .map((match) => ({ targetKey: match[1], targetName: match[2] }));
  const originalFetch = globalThis.fetch;
  const calls = [];

  globalThis.fetch = async (url, options = {}) => {
    const path = String(url);
    calls.push({ path, method: options.method || "GET" });
    if (path.includes("/assistant/capabilities")) {
      return Response.json(catalog);
    }
    let payload = { items: [], rows: [], total: 0, count: 0, id: 1, ok: true };
    if (path.includes("/zabbix/configs")) {
      payload = { items: [{ id: 1, enabled: true }] };
    } else if (path.includes("/observability/sources")) {
      payload = { items: [{ id: 2, source_type: "prometheus", enabled: true }] };
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    const instance = {
      id: 7,
      instance_name: "orcl",
      db_type: "oracle",
      host: "192.168.102.200",
      port: 1521,
    };
    for (const target of targets) {
      const before = calls.length;
      const result = await actions.executeAssistantActionRequest(
        {
          ...target,
          title: target.targetName,
          command: "执行检查",
          targetDbType: "oracle",
        },
        { instance },
      );
      assert.equal(result.__assistantResult, true, `${target.targetKey} did not return an assistant result`);
      const businessCalls = calls.slice(before).filter((call) => !call.path.includes("/assistant/capabilities"));
      const payload = result.data || {};
      if (["inspect.reports", "inspect.compare", "perf.awr.compare", "rescue.reports", "rescue.approvals"].includes(target.targetKey)) {
        assert.equal(payload.status, "unsupported", target.targetKey);
        assert.equal(payload.code, "semantic_gap");
        assert.equal(businessCalls.length, 0);
      } else if (PARAMETER_REQUIRED_TARGETS.has(target.targetKey)) {
        assert.ok(["needs_input", "unsupported"].includes(payload.status), `${target.targetKey}: ${payload.status}`);
        assert.equal(businessCalls.length, 0);
      } else if (UNWIRED_TARGETS.has(target.targetKey)) {
        assert.ok(["unsupported", "needs_input"].includes(payload.status), `${target.targetKey}: ${payload.status}`);
        assert.equal(businessCalls.length, 0, `${target.targetKey} must not execute in the browser`);
      } else if (SERVER_TARGETS.has(target.targetKey)) {
        // C13 server readers are confirmed into a plan (or refused for the chosen engine) and
        // never fall back to a browser-side fetch.
        assert.ok(["needs_input", "unsupported"].includes(payload.status), `${target.targetKey}: ${payload.status}`);
        if (payload.status === "needs_input") assert.equal(payload.code, "plan_confirmation_required");
        assert.equal(businessCalls.length, 0, `${target.targetKey} must not fetch in the browser`);
      } else {
        assert.ok(businessCalls.length > 0, `${target.targetKey}: authorized client branch must retain its real implementation`);
      }
    }
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.filter((item) => item.path.includes("/assistant/capabilities")).length, targets.length);
  // Backup/restore plans are now server-side plan steps: the browser must not POST them.
  assert.ok(!calls.some((item) => item.path.includes("/api/v1/backup-center/plan")));
  for (const path of ["/api/v1/operations/oracle/7/tablespaces", "/api/v1/system/dashboard-stats"]) {
    assert.ok(!calls.some((item) => item.path.includes(path)), `client call leaked for ${path}`);
  }
});

test("natural-language actions use server dispatch and manual navigation as fallback", () => {
  const runBlock = diagnosisPageSource.slice(diagnosisPageSource.indexOf("const runDiagnosis = async"), diagnosisPageSource.indexOf("const createTicket ="));
  assert.match(runBlock, /resolveCustomerRequirement/);
  assert.doesNotMatch(runBlock, /resolveAssistantActionRequest|runAssistantAction|runDiagnosisCenter/);
  assert.match(runBlock, /navigateFromIntent/);
});
