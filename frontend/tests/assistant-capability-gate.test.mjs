import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const backendRoot = path.resolve(root, "../backend");
const loaded = spawnSync("python3", ["-c",
  "import json, runpy, sys; print(json.dumps(runpy.run_path(sys.argv[1])['CAPABILITY_DECLARATIONS']))",
  path.resolve(backendRoot, "app/services/capability_declarations.py"),
], { encoding: "utf8", env: { ...process.env, PYTHONPATH: backendRoot } });
assert.equal(loaded.status, 0, loaded.stderr);
const declarations = JSON.parse(loaded.stdout);
const gaps = new Set(["inspect.reports", "inspect.compare", "perf.awr.compare", "rescue.reports", "rescue.approvals"]);
// 无页面的能力：有 binding、有 handler，但 target_key 为空（能力矩阵页面已移除）。
// 页面驱动的助手路径对它必须返回 unsupported —— 不能让浏览器抢在计划确认之前执行；
// 只允许 AI 按 capability_id 走服务端计划。这条集合显式钉死，防止悄悄多出"没页面"的能力。
const pageless = declarations.filter((entry) => entry.bindings.length
  && entry.bindings.every((binding) => !binding.target_key));
// Derived from the code-owned declarations: every server capability must be confirmed into a
// plan, and a client branch must never be mistaken for one.
const servers = new Set(declarations.filter((entry) => entry.execution === "server").map((entry) => entry.capability_id));
const view = (entry) => ({
  ...structuredClone(entry), definition_version: entry.version, enabled: true,
  availability: "available", unavailable_reason: null,
  authorized_engines: [...entry.engines], plan_executable: entry.execution === "server",
});
const catalog = (items = declarations.map(view)) => ({ schema: "capability.registry/v1", items });
const find = (id) => view(declarations.find((item) => item.capability_id === id));
const instance = (engine = "oracle") => ({ id: 7, db_type: engine, instance_name: "test", host: "test.invalid", port: 1 });
const request = (targetKey, command = "查询") => ({ targetKey, targetName: targetKey, command, title: targetKey });
let vite;
let actions;

test.before(async () => {
  vite = await createServer({ root, appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false } });
  actions = await vite.ssrLoadModule("/src/modules/assistantActions.js");
});
test.after(async () => { await vite?.close(); });

async function withCatalog(value, run, status = 200) {
  const original = globalThis.fetch;
  const originalWindow = globalThis.window;
  const authEvents = [];
  globalThis.window = { dispatchEvent: (event) => { authEvents.push(event.type); return true; } };
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const call = { path: String(url), method: options.method || "GET", body: options.body };
    calls.push(call);
    if (call.path.includes("/assistant/capabilities")) {
      if (value instanceof Error) throw value;
      if (value instanceof Response) return value;
      return Response.json(typeof value === "function" ? value() : value, { status });
    }
    if (call.path.includes("/zabbix/configs")) return Response.json({ items: [{ id: 1, enabled: true }] });
    if (call.path.includes("/observability/sources")) return Response.json({ items: [{ id: 2, source_type: "prometheus", enabled: true }] });
    return Response.json({ items: [], rows: [], total: 0 });
  };
  try { await run(calls, authEvents); } finally {
    globalThis.fetch = original;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}
const business = (calls) => calls.filter((call) => !call.path.includes("/assistant/capabilities"));

const branchCommands = {
  read: "查询", inventory: "查询资产", discovery: "自动识别资产", jobs: "查询任务",
  alerts: "查询告警", import_alerts: "导入告警", collect: "采集", linkage_health: "查询联动",
  auto_bind: "自动绑定", logs: "查询日志", fault_timeline: "故障时间线", source_status: "查询链路",
  trace_id: "查询 Trace ID: abc-123", refresh: "刷新", history: "查询历史",
  table_profile: "查询对象", startup: "启动", shutdown: "关闭", switchover: "switchover",
  candidates: "查询候选", governance_section: "扫描治理", scan: "扫描治理",
  backup: "生成备份方案", restore: "生成恢复方案", tasks: "查询任务", status: "查询", sync: "同步", generate: "生成报告",
  root_cause: "执行端到端根因诊断",
};

test("every eligible declaration branch and engine selects exactly its code-owned binding", async (t) => {
  let checked = 0;
  const checkedTargets = new Set();
  assert.equal(declarations.length, 79, "declaration coverage changed; review the executor bindings");
  assert.deepEqual(pageless.map((entry) => entry.capability_id), ["operations.capability_matrix.read"]);
  for (const entry of declarations) {
    for (const binding of entry.bindings) {
      if (!binding.target_key) continue;  // 无页面能力：由本用例开头的 pageless 断言 + test 169 覆盖
      if (gaps.has(binding.target_key) || binding.target_key === "inspect.run" || binding.branch === "historical_results") continue;
      assert.ok(Object.hasOwn(branchCommands, binding.branch), binding.branch);
      for (const engine of binding.engine ? [binding.engine] : entry.engines) {
        const req = request(binding.target_key, branchCommands[binding.branch]);
        await withCatalog(catalog([view(entry)]), async (calls) => {
          const prepared = await actions.prepareAssistantAction(req, { instance: instance(engine) });
          assert.equal(prepared.capability?.capability_id, entry.capability_id, `${binding.target_key}/${binding.branch}/${engine}`);
          const expected = servers.has(entry.capability_id) ? "needs_input"
            : entry.handler_status === "declared_not_wired" ? "unsupported" : "ready";
          assert.equal(prepared.status, expected,
            `${binding.target_key}/${binding.branch}/${engine}/${prepared.capability?.capability_id}: ${prepared.reason}`);
          assert.equal(business(calls).length, 0, "preparation must never call business APIs");
          assert.equal(calls.length, 1, "one fresh catalog request");
        });
        const withoutBranch = view(entry);
        withoutBranch.bindings = withoutBranch.bindings.filter((row) =>
          !(row.target_key === binding.target_key && row.branch === binding.branch && row.engine === binding.engine));
        await withCatalog(catalog([withoutBranch]), async () => {
          const denied = await actions.prepareAssistantAction(req, { instance: instance(engine) });
          assert.equal(denied.status, "unsupported", `${binding.target_key}: cannot substitute another branch`);
          assert.equal(denied.reason, entry.handler_status === "declared_not_wired"
            ? "handler_not_wired" : "binding_not_supported");
        });
        await withCatalog(catalog([view(entry)]), async (calls) => {
          const executed = await actions.executeAssistantActionRequest(req, { instance: instance(engine) });
          if (servers.has(entry.capability_id) || entry.handler_status === "declared_not_wired") {
            assert.equal(executed.data.status, servers.has(entry.capability_id) ? "needs_input" : "unsupported");
            if (servers.has(entry.capability_id)) assert.equal(executed.data.code, "plan_confirmation_required");
            assert.equal(business(calls).length, 0);
            return;
          }
          assert.ok(business(calls).length > 0, `${binding.target_key}/${binding.branch}/${engine}: no real business call`);
          const endpoints = binding.endpoints || [binding];
          for (const call of business(calls)) {
            const pathname = new URL(call.path, "http://frontend.invalid").pathname.replace(/^\/api\/v1/, "");
            assert.ok(endpoints.some((endpoint) => {
              const pattern = endpoint.endpoint_template.split(/(\{[^{}]+\})/).map((part) =>
                part.startsWith("{") ? "[^/]+" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("");
              return endpoint.method === call.method && new RegExp(`^${pattern}$`).test(pathname);
            }), `${binding.target_key}/${binding.branch}/${engine}: unexpected ${call.method} ${pathname}`);
          }
          assert.equal(calls.filter((call) => call.path.includes("/assistant/capabilities")).length, 1);
        });
        await withCatalog(catalog([{ ...view(entry), authorized_engines: entry.engines.filter((value) => value !== engine) }]), async (calls) => {
          const code = (await actions.executeAssistantActionRequest(req, { instance: instance(engine) })).data.code;
          assert.equal(code, entry.handler_status === "declared_not_wired" ? "handler_not_wired" : "engine_not_authorized");
          assert.equal(business(calls).length, 0, `${binding.target_key}/${binding.branch}/${engine}: cannot broaden authorization`);
        });
        checkedTargets.add(binding.target_key);
        checked += 1;
      }
    }
  }
  assert.ok(checked > 200, `only checked ${checked} branch/engine combinations`);
  // 只统计有页面的 target：无页面能力（pageless）在本用例开头单独断言，不混进页面覆盖数。
  const targets = new Set(declarations.flatMap((entry) => entry.bindings.map((binding) => binding.target_key)).filter(Boolean));
  // 70 → 69：能力矩阵页面移除后，声明侧不再有 ops.matrix 这个页面 target。
  // 与 test_capability_declarations 里前端 TARGET_TITLES 的 69 项保持一致。
  assert.equal([...targets].filter((target) => target !== "inspect.run").length, 69);
  assert.deepEqual([...targets].filter((target) => !checkedTargets.has(target)).sort(), [...gaps, "inspect.run"].sort());
  t.diagnostic(`${checked} branch/engine combinations; ${checkedTargets.size} executable-or-confirmed targets; 5 explicit semantic gaps; inspect.run remains dedicated`);
});

test("branch predicates and precedence match legacy commands, including normalization", async () => {
  const cases = [
    ["asset.center", "刷新", "oracle", "asset.inventory.read"],
    ["asset.center", "刷新 拓扑", "oracle", "asset.discovery.execute"],
    ["ops.overview", "关闭再启动", "oracle", "operations.startup.plan"],
    ["ops.overview", "开库", "oracle", "operations.startup.plan"],
    ["ops.overview", "停库", "oracle", "operations.shutdown.plan"],
    ["ops.overview", "启动", "mysql", "operations.runtime_overview.read"],
    ["ops.ha", "切换", "oracle", "operations.switchover.plan"],
    ["ops.ha", "切换", "postgresql", "operations.ha.read"],
    ["ops.governance", "分析", "mysql", "governance.candidate.scan"],
    ["ops.governance", "分析", "postgresql", "governance.candidate.read"],
    ["monitor.observability.apm", "trace id:", "oracle", "monitor.apm_source.read"],
    ["monitor.observability.apm", "链路id=abc", "oracle", "monitor.apm_trace.read"],
  ];
  for (const [target, command, engine, id] of cases) {
    await withCatalog(catalog(), async () => {
      const prepared = await actions.prepareAssistantAction(request(target, command), { instance: instance(engine) });
      assert.equal(prepared.status, servers.has(id) ? "needs_input" : "ready", `${target}/${command}: ${prepared.reason}`);
      if (servers.has(id)) assert.equal(prepared.reason, "plan_confirmation_required");
      assert.equal(prepared.capability.capability_id, id);
    });
  }
});

test("unknown, hidden, disabled, unsynced, unwired, stale and unauthorized gates make zero business calls", async () => {
  const base = find("platform.dashboard.read");
  const cases = [
    [catalog(), request("unknown.target"), "unknown_target"],
    [catalog(), request("__proto__"), "unknown_target"],
    [catalog(), { ...request("unknown.target"), capability_id: "platform.dashboard.read" }, "unknown_target"],
    [catalog(), request("inspect.run"), "unknown_target"],
    [catalog([]), request("home.dashboard"), "capability_not_authorized"],
    [catalog([{ ...base, enabled: false }]), request("home.dashboard"), "capability_disabled"],
    [catalog([{ ...base, availability: "blocked", unavailable_reason: "definition_not_synced" }]), request("home.dashboard"), "definition_not_synced"],
    [catalog([{ ...base, availability: "blocked", unavailable_reason: "definition_out_of_sync" }]), request("home.dashboard"), "definition_out_of_sync"],
    [catalog([{ ...base, availability: "blocked", unavailable_reason: "scope_not_supported" }]), request("home.dashboard"), "scope_not_supported"],
    [catalog([{ ...base, handler_status: "declared_not_wired" }]), request("home.dashboard"), "handler_not_wired"],
    [catalog([{ ...base, execution: "client", handler_status: "new_unknown_wiring" }]), request("home.dashboard"), "handler_not_wired"],
    [catalog([{ ...base, definition_version: base.version + 1 }]), request("home.dashboard"), "stale_definition_version"],
    [catalog([{ ...base, authorized_engines: ["mysql"] }]), request("home.dashboard"), "engine_not_authorized"],
    [catalog([{ ...base, authorized_engines: [] }]), request("home.dashboard"), "engine_not_authorized"],
    [catalog([{ ...base, authorized_engines: undefined }]), request("home.dashboard"), "engine_not_authorized"],
    [catalog([{ ...base, engines: [] }]), request("home.dashboard"), "engine_not_authorized"],
    [catalog([{ ...base, bindings: [] }]), request("home.dashboard"), "binding_not_supported"],
    [catalog([{ ...base, bindings: [base.bindings[0], base.bindings[0]] }]), request("home.dashboard"), "binding_not_supported"],
    [catalog([{ ...base, bindings: [{ ...base.bindings[0], engine: null }] }]), request("home.dashboard"), "binding_not_supported"],
    [catalog([base, base]), request("home.dashboard"), "ambiguous_capability"],
    [{ items: [base] }, request("home.dashboard"), "invalid_capability_catalog"],
  ];
  for (const [value, req, code] of cases) {
    await withCatalog(value, async (calls) => {
      const result = await actions.executeAssistantActionRequest(req, { instance: instance() });
      assert.equal(result.__assistantResult, true);
      assert.equal(result.data.status, "unsupported", `${code}: ${JSON.stringify(result.data)}`);
      assert.equal(result.data.code, code);
      assert.match(result.summary, /[\u4e00-\u9fff]/u, "denials need a user-facing explanation");
      assert.equal(result.data.message, result.summary);
      assert.equal(business(calls).length, 0, code);
    });
  }
});

test("no identity, denied permissions and catalog failures fail closed without fallback", async () => {
  for (const [value, status, code] of [
    [{ detail: { code: "authentication_required" } }, 401, "authentication_required"],
    [{ detail: { code: "permission_denied" } }, 403, "permission_denied"],
    [{ detail: "Not Found" }, 404, "catalog_unavailable"],
    [{ detail: { code: "registry_storage_unavailable" } }, 503, "catalog_unavailable"],
    [new Error("offline"), 200, "catalog_unavailable"],
    [null, 200, "invalid_capability_catalog"],
    [new Response("not json", { headers: { "Content-Type": "application/json" } }), 200, "invalid_capability_catalog"],
  ]) {
    await withCatalog(value, async (calls, authEvents) => {
      const result = await actions.executeAssistantActionRequest(request("asset.center", "自动识别资产"));
      assert.equal(result.data.status, "unsupported");
      assert.equal(result.data.code, code);
      assert.match(result.summary, /[\u4e00-\u9fff]/u);
      if (code === "catalog_unavailable") assert.match(result.summary, /加载失败/);
      if (code === "invalid_capability_catalog") assert.match(result.summary, /格式异常/);
      assert.equal(business(calls).length, 0);
      assert.deepEqual(authEvents, status === 401 ? ["auth-expired"] : []);
    }, status);
  }
});

test("capability checks are fresh at execution, never implicitly syncing or trusting model selectors", async () => {
  let reads = 0;
  await withCatalog(() => ++reads === 1 ? catalog() : catalog([]), async (calls) => {
    const req = { ...request("home.dashboard"), capability_id: "asset.discovery.execute", handler: "arbitrary", url: "/assets/discovery/apply" };
    const prepared = await actions.prepareAssistantAction(req);
    assert.equal(prepared.capability.capability_id, "platform.dashboard.read");
    const result = await actions.executeAssistantActionRequest(req);
    assert.equal(result.data.status, "unsupported");
    assert.equal(reads, 2);
    assert.equal(calls.every((call) => call.method === "GET" && call.path.includes("/assistant/capabilities")), true);
  });
});

test("a hanging catalog read stops waiting after 90 seconds, never retries or executes late", async () => {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const originalFetch = globalThis.fetch;
  const timers = new Map();
  const calls = [];
  const pending = [];
  globalThis.setTimeout = (callback, milliseconds) => {
    assert.equal(milliseconds, 90_000, "catalog timeout must be bounded to 90 seconds");
    const timer = {};
    timers.set(timer, callback);
    return timer;
  };
  globalThis.clearTimeout = (timer) => timers.delete(timer);
  globalThis.fetch = (url) => {
    calls.push(String(url));
    return new Promise((resolve) => pending.push(resolve));
  };
  try {
    for (const execute of [false, true]) {
      const running = execute
        ? actions.executeAssistantActionRequest(request("home.dashboard"))
        : actions.prepareAssistantAction(request("home.dashboard"));
      assert.equal(timers.size, 1);
      [...timers.values()][0]();
      const result = await running;
      assert.equal(execute ? result.data.status : result.status, "unsupported");
      assert.equal(execute ? result.data.code : result.reason, "catalog_unavailable");
      assert.match(result.summary, /加载失败/);
      assert.equal(timers.size, 0, "timeout timer must be cleared");
      pending.shift()(Response.json(catalog()));
      await new Promise((resolve) => setImmediate(resolve));
      assert.equal(calls.length, execute ? 2 : 1, "neither timeout nor late completion may retry");
      assert.equal(calls.every((url) => url === "/api/v1/assistant/capabilities"), true);
    }
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    globalThis.fetch = originalFetch;
  }
});

test("catalog metadata cannot redirect static client calls or reinterpret an in-flight request", async () => {
  const entry = find("platform.dashboard.read");
  entry.handler = "arbitrary.handler";
  entry.bindings[0].endpoint_template = "/assets/discovery/apply";
  entry.bindings[0].method = "POST";
  const req = request("home.dashboard");
  await withCatalog(() => {
    req.targetKey = "asset.center";
    req.command = "自动识别资产";
    return catalog([entry]);
  }, async (calls) => {
    const result = await actions.executeAssistantActionRequest(req);
    // The dashboard is a confirmed server capability now: metadata cannot redirect it to a
    // browser fetch, so preparation only offers the plan confirmation.
    assert.ok(["needs_input", "unsupported"].includes(result.data.status));
    assert.deepEqual(business(calls), []);
  });
});

test("all three wired server capabilities require explicit plan confirmation and never use legacy HTTP", async () => {
  for (const target of ["inspect.overview", "inspect.tasks", "inspect.policies", "inspect.templates"]) {
    await withCatalog(catalog(), async (calls) => {
      const prepared = await actions.prepareAssistantAction(request(target), { instance: instance() });
      assert.equal(prepared.status, "needs_input");
      assert.equal(prepared.reason, "plan_confirmation_required");
      assert.match(prepared.summary, /确认执行清单/);
      assert.ok(servers.has(prepared.capability.capability_id));
      const result = await actions.executeAssistantActionRequest(request(target), { instance: instance() });
      assert.equal(result.data.status, "needs_input");
      assert.equal(result.data.code, "plan_confirmation_required");
      assert.equal(business(calls).length, 0);
    });
  }
  // 能力矩阵已无页面：按页面 key 触发必须直接 unsupported，且不得发起业务请求。
  // 它仍可通过 capability_id 走服务端计划（后端 test_capability_plan.py 覆盖）。
  assert.deepEqual(pageless.map((entry) => entry.capability_id), ["operations.capability_matrix.read"]);
  await withCatalog(catalog(), async (calls) => {
    const prepared = await actions.prepareAssistantAction(request("ops.matrix"), { instance: instance() });
    assert.equal(prepared.status, "unsupported");
    assert.equal((await actions.executeAssistantActionRequest(request("ops.matrix"))).data.status, "unsupported");
    assert.equal(business(calls).length, 0);
  });
});

test("client revisions stay pinned while current server revisions require unchanged reader contracts", async () => {
  for (const [id, target] of [
    ["inspection.run.query", "inspect.tasks"], ["inspection.template.read", "inspect.templates"],
    // operations.capability_matrix.read 已无页面，页面驱动的版本校验对它不适用（见 pageless）。
  ]) {
    const current = { ...find(id), version: 2, definition_version: 2 };
    await withCatalog(catalog([current]), async (calls) => {
      const prepared = await actions.prepareAssistantAction(request(target), { instance: instance() });
      assert.equal(prepared.status, "needs_input");
      assert.equal(prepared.reason, "plan_confirmation_required");
      assert.equal(prepared.capability.definition_version, 2);
      assert.equal((await actions.executeAssistantActionRequest(request(target), { instance: instance() })).data.code, "plan_confirmation_required");
      assert.equal(business(calls).length, 0);
    });
    for (const patch of [
      { definition_version: 1 }, { version: 0, definition_version: 0 }, { version: "2", definition_version: "2" },
      { version: 1.5, definition_version: 1.5 }, { version: Number.MAX_SAFE_INTEGER + 1, definition_version: Number.MAX_SAFE_INTEGER + 1 },
      { output_schema: "future-reader/v2" },
    ]) {
      await withCatalog(catalog([{ ...current, ...patch }]), async (calls) => {
        const result = await actions.executeAssistantActionRequest(request(target), { instance: instance() });
        assert.equal(result.data.status, "unsupported");
        assert.equal(result.data.code, patch.output_schema ? "server_contract_not_supported" : "stale_definition_version");
        assert.equal(business(calls).length, 0);
      });
    }
  }
});

test("persisted effect products require their exact registered version and schema", async () => {
  for (const [id, target, command, version, schema] of [
    ["performance.sql_profile.collect", "perf.sql", "采集", 3, "capability.effect_result/v1"],
    ["report.document.generate", "report.unified", "生成报告", 3, "report.document_result/v1"],
    ["diagnosis.root_cause.execute", "diagnose.investigation", "执行端到端根因诊断", 1, "diagnosis.root_cause_result/v1"],
  ]) {
    const current = find(id);
    assert.equal(current.version, version);
    assert.equal(current.output_schema, schema);
    for (const patch of [
      { version: version + 1, definition_version: version + 1 },
      { output_schema: "object" },
    ]) {
      await withCatalog(catalog([{ ...current, ...patch }]), async (calls) => {
        const result = await actions.executeAssistantActionRequest(request(target, command), { instance: instance() });
        assert.equal(result.data.status, "unsupported");
        assert.equal(result.data.code, "server_contract_not_supported");
        assert.equal(business(calls).length, 0);
      });
    }
  }
});

test("semantic report/comparison/approval gaps are not task-list or history success", async () => {
  for (const target of gaps) {
    await withCatalog(catalog(), async (calls) => {
      const prepared = await actions.prepareAssistantAction(request(target), { instance: instance() });
      assert.equal(prepared.status, "unsupported");
      assert.equal(prepared.reason, "semantic_gap");
      assert.equal((await actions.executeAssistantActionRequest(request(target), { instance: instance() })).data.status, "unsupported");
      assert.equal(business(calls).length, 0);
    });
  }
});

test("wired pages confirm a plan; only genuine client branches read in the browser", async () => {
  for (const target of ["home.dashboard", "asset.center", "perf.overview", "system.params", "diagnose.incidents", "rescue.tasks", "system.identity", "diagnose.investigation", "ops.configGovernance"]) {
    await withCatalog(catalog(), async (calls) => {
      assert.equal((await actions.prepareAssistantAction(request(target))).status, "needs_input", target);
      const result = await actions.executeAssistantActionRequest(request(target));
      assert.equal(result.__assistantResult, true);
      assert.equal(result.data.status, "needs_input");
      assert.equal(result.data.code, "plan_confirmation_required");
      assert.deepEqual(business(calls), [], `${target} must not fetch in the browser`);
    });
  }
});

test("instance and parameter requirements cannot be disabled by request flags", async () => {
  await withCatalog(catalog(), async (calls) => {
    const req = { ...request("perf.sql"), requiresInstance: false };
    assert.equal((await actions.executeAssistantActionRequest(req)).data.code, "missing_instance");
    assert.match((await actions.prepareAssistantAction(req)).summary, /选择.*数据库实例/);
    assert.equal((await actions.executeAssistantActionRequest(req, { instance: { id: -1, db_type: "oracle" } })).data.code, "missing_instance");
    assert.equal((await actions.executeAssistantActionRequest(req, { instance: { id: Number.MAX_SAFE_INTEGER + 1, db_type: "oracle" } })).data.code, "missing_instance");
    assert.equal((await actions.executeAssistantActionRequest({ ...request("system.params", "修改配置"), requiresParameters: false })).data.code, "missing_parameters");
    assert.equal((await actions.executeAssistantActionRequest({ ...request("perf.sql"), targetDbType: "mysql" }, { instance: instance() })).data.code, "target_engine_mismatch");
    assert.equal((await actions.executeAssistantActionRequest(request("perf.slowsql"), { instance: instance("postgresql") })).data.status, "unsupported");
    assert.equal(business(calls).length, 0);
  });
});

test("progress preserves blocked/unsupported/unknown/expired/cancelled/plan_only outcomes without completed execution steps", async () => {
  for (const outcome of ["unsupported", "blocked", "unknown", "expired", "cancelled", "needs_input", "failed", "plan_only"]) {
    for (const payload of [{ status: outcome, code: "test_reason" }, { status: "settled", outcome, outcome_reason: "test_reason" },
      { status: "success", outcome }, { status: "failed", outcome }, { status: "unsupported", outcome }]) {
      const progress = [];
      const result = await actions.runAssistantActionProgress({
        request: request("home.dashboard"), execute: async () => ({ __assistantResult: true, data: payload, summary: "执行完成" }),
        onProgress: (state) => progress.push(state),
      });
      assert.equal(result.status, outcome);
      assert.equal(result.outcome, payload.outcome || null);
      assert.notEqual(result.percent, 100);
      assert.equal(result.steps.find((row) => row.key === "execute").status, outcome);
      assert.equal(result.steps.find((row) => row.key === "result").status, "pending");
      assert.doesNotMatch(result.summary, /执行完成/);
      assert.equal(progress.some((state) => state.steps.find((row) => row.key === "execute").status === "success"), false);
    }
  }
  for (const status of ["running", "queued", "accepted", "validated", "awaiting_approval", "settled", "new_unknown_status"]) {
    const result = await actions.runAssistantActionProgress({ request: request("home.dashboard"), execute: async () => ({ status }) });
    assert.notEqual(result.status, "success", status);
    assert.notEqual(result.steps.find((row) => row.key === "execute").status, "success", status);
  }
  const succeeded = await actions.runAssistantActionProgress({ request: request("home.dashboard"), execute: async () => ({ status: "settled", outcome: "succeeded" }) });
  assert.equal(succeeded.status, "success");
  assert.equal(succeeded.outcome, "succeeded");
  const unknownOutcome = await actions.runAssistantActionProgress({ request: request("home.dashboard"), execute: async () => ({ status: "success", outcome: "future_outcome" }) });
  assert.equal(unknownOutcome.status, "unknown");
  const blankOutcome = await actions.runAssistantActionProgress({ request: request("home.dashboard"), execute: async () => ({ status: "success", outcome: "" }) });
  assert.equal(blankOutcome.status, "unknown");
});

test("historical ignored completed filter and instance-list fallback are absent", () => {
  const source = readFileSync(path.join(root, "src/modules/assistantActions.js"), "utf8");
  assert.doesNotMatch(source, /status:\s*["']completed["']/);
  assert.doesNotMatch(source, /instanceApi/);
  assert.doesNotMatch(source, /listInspectionTasks|listInspectionPolicies|genericOpsApi\.capabilityMatrix/);
});
