import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import { createServer } from "vite";
import { resolveCustomerRequirement } from "../src/modules/assistantRequirement.js";
import { buildClarificationSubmission, normalizeClarificationAnswers } from "../src/modules/requirementClarification.js";
import {
  ASSISTANT_DISPATCH_JOURNAL_KEY,
  ASSISTANT_DISPATCH_JOURNAL_TTL,
  clearAssistantDispatchJournal as clearJournal,
  currentAssistantDispatchJournal as currentJournal,
  readAssistantDispatchJournal as readJournal,
  writeAssistantDispatchJournal as writeJournal,
} from "../src/modules/assistantDispatchJournal.js";

const source = readFileSync(new URL("../src/DiagnosisCenterPage.jsx", import.meta.url), "utf8");
const functions = {};
traverseModule.default(parse(source, { sourceType: "module", plugins: ["jsx"] }), {
  VariableDeclarator({ node }) {
    if (["runDiagnosis", "navigateFromIntent"].includes(node.id.name)) {
      functions[node.id.name] = source.slice(node.init.start, node.init.end);
    }
  },
  CallExpression({ node }) {
    if (node.callee?.name === "useEffect" && source.slice(node.start, node.end).includes("readAssistantDispatchJournal")) {
      functions.recoveryEffect = source.slice(node.arguments[0].start, node.arguments[0].end);
    }
  },
});

function harness(text, dispatch, navigation = null, storage = new Map()) {
  const events = [];
  const state = {};
  const payloads = [];
  const browserStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  const session = { loginEpoch: "a".repeat(32) };
  const requestEpoch = { current: 0 };
  state.problemText = text;
  const bindings = {
    problemText: text, instanceId: 7, selectedInstance: { id: 7, db_type: "mysql" },
    dbInstances: [{ id: 7, db_type: "mysql" }, { id: 9, db_type: "oracle" }], menuItems: undefined,
    assistantRequestEpoch: requestEpoch, dispatchEpochRef: requestEpoch,
    assistantDispatchBusyRef: { current: null }, pendingDispatch: null,
    recoveryStartedRef: { current: false }, recoveryInstanceRef: { current: 7 },
    getAuthSession: () => session, isAuthSessionCurrent: () => true,
    writeAssistantDispatchJournal: (value) => writeJournal(value, browserStorage),
    readAssistantDispatchJournal: () => readJournal(browserStorage),
    currentAssistantDispatchJournal: (value) => currentJournal(value, browserStorage),
    clearAssistantDispatchJournal: (requestId) => clearJournal(requestId, browserStorage),
    resolveCustomerRequirement,
    dispatchAssistantIntent: async (payload) => {
      payloads.push(payload);
      const { request_id: _requestId, ...visible } = payload;
      events.push(["dispatch", visible]);
      return dispatch(payload);
    },
    getDispatchReceipt: async (requestId) => {
      events.push(["receipt", requestId]);
      return state.receipt ? state.receipt(requestId) : null;
    },
    resolveSmartNavigation: () => { events.push(["match"]); return navigation; },
    setRunning: (value) => { state.running = value; },
    setAiDegradationNotice: (value) => { state.notice = value; },
    setPlanRequest: (value) => { state.plan = value; },
    setPlanWorkspaceOpen: (value) => { state.workspaceOpen = value; },
    setInstanceId: (value) => { state.instanceId = value; },
    setProblemText: (value) => {
      state.problemText = typeof value === "function" ? value(state.problemText) : value;
      bindings.problemText = state.problemText;
    },
    setPendingDispatch: (value) => { state.pending = typeof value === "function" ? value(state.pending) : value; bindings.pendingDispatch = state.pending; },
    setClarification: (value) => { state.clarification = typeof value === "function" ? value(state.clarification) : value; },
    setWorkspaceStarted: () => {},
    isInstanceProvisioningCommand: () => false,
    onNavigate: (...args) => { events.push(["navigate", ...args]); },
    message: Object.fromEntries(["info", "warning", "error"].map((key) => [key, (value) => events.push([key, value])])),
    normalizeMenuKey: (key) => key,
  };
  const bind = (fn) => new Function(...Object.keys(bindings), `return (${fn});`)(...Object.values(bindings));
  bindings.navigateFromIntent = bind(functions.navigateFromIntent);
  return { run: (options) => bind(functions.runDiagnosis)(options), events, payloads, state, bindings,
    recover: () => bind(functions.recoveryEffect)(), journal: () => readJournal(browserStorage), storage };
}

for (const text of ["帮我巡检所有数据库", "生成售后分析报告", "采集慢 SQL", "订单提交超时", "添加数据库实例", "查看慢 SQL 分析页面"]) {
  test(`unavailable API opens a manual page without running '${text}'`, async () => {
    const h = harness(text, () => ({ action: "navigate", source: "local" }), {
      targetKey: "inspect.run", title: "一键巡检", targetDbType: "mysql",
    });
    await h.run();
    assert.deepEqual(h.events.filter(([name]) => ["dispatch", "match", "navigate", "error"].includes(name)), [
      ["dispatch", { text, instance_id: 7 }], ["match"], ["navigate", "inspect.run", 7, {
        kind: "manual_requirement", text, targetKey: "inspect.run", title: "一键巡检",
        reason: h.state.notice.text, requestId: h.payloads[0].request_id,
      }],
    ]);
    assert.equal(h.state.plan, undefined);
    assert.equal(h.state.running, false);
    assert.match(h.state.notice.text, /手动操作/);
  });
}

test("model workflow opens the prepared workflow plan", async () => {
  const planId = `pl${"b".repeat(26)}`;
  const h = harness("列出巡检任务", () => ({
    action: "workflow", source: "ai", workflow: { workflow_id: "inspection.jobs.list" },
    plan: { plan_id: planId },
  }));
  await h.run();
  assert.equal(h.state.plan.planId, planId);
  assert.equal(h.state.workspaceOpen, true);
  assert.equal(h.events.filter(([name]) => name === "navigate").length, 0);
});

for (const action of ["executed", "confirm"]) {
  test(`model ${action} opens the saved plan, with no local execution or navigation`, async () => {
    const capability = { capability_id: "inspection.run.query" };
    const h = harness("查询巡检任务", () => ({ action, source: "ai", capability, plan: { plan_id: "pl1" } }));
    await h.run();
    assert.deepEqual({ ...h.state.plan, requestId: undefined },
      { capability, instanceId: 7, planId: "pl1", params: {}, requirementText: "查询巡检任务", requestId: undefined });
    assert.equal(h.state.plan.requestId, h.journal().requestId);
    assert.equal(h.state.workspaceOpen, true);
    assert.deepEqual(h.events.map(([name]) => name), ["dispatch"]);
  });
}

test("transport failure remains pending and does not navigate or open manual capabilities", async () => {
  const h = harness("检查业务故障", () => { throw new Error("offline"); });
  await h.run();
  assert.equal(h.events.filter(([name]) => name === "dispatch").length, 1);
  assert.equal(h.events.filter(([name]) => name === "error").length, 0);
  assert.equal(h.state.plan, undefined);
  assert.equal(h.state.workspaceOpen, undefined);
  assert.equal(h.state.pending.requestId, h.journal().requestId);
});

test("invalid model plans and no-match responses degrade to manual pages", async () => {
  for (const response of [{ action: "executed", source: "ai" }, { action: "navigate", source: "ai" }]) {
    const h = harness("查询报告", () => response, { targetKey: "report.unified", title: "统一报告" });
    await h.run();
    assert.ok(h.events.some(([name, target]) => name === "navigate" && target === "report.unified"));
    assert.equal(h.state.plan, undefined);
  }
});

test("server permission, validation and execution errors remain visible without navigating", async () => {
  for (const status of [401, 403, 409, 422, 500]) {
    const h = harness("查询任务", () => { throw Object.assign(new Error("服务端拒绝或执行失败"), { status }); });
    await h.run();
    assert.ok(h.events.some(([name]) => name === "error"));
    assert.ok(!h.events.some(([name]) => ["match", "navigate"].includes(name)));
    assert.equal(h.state.workspaceOpen, undefined);
  }
});

test("missing instance opens the model-selected capability for manual input", async () => {
  const capability = { capability_id: "inspection.run.execute" };
  const h = harness("巡检", () => ({ action: "navigate", source: "ai", capability, reason: "需要先选择实例" }));
  await h.run();
  assert.equal(h.state.plan.capability, capability);
  assert.equal(h.state.workspaceOpen, true);
  assert.ok(!h.events.some(([name]) => name === "match"));
});

test("page navigation never silently switches to the first instance of another engine", async () => {
  const h = harness("Oracle 空间管理", () => ({ action: "navigate", source: "local" }), {
    targetKey: "ops.space", title: "空间管理", targetDbType: "oracle",
  });
  await h.run();
  assert.ok(h.events.some(([name, target, id]) => name === "navigate" && target === "ops.space" && id === null));
});

test("the real local matcher opens inspection, report and slow SQL pages without a model", async () => {
  const vite = await createServer({
    root: new URL("..", import.meta.url).pathname,
    appType: "custom", logLevel: "silent", server: { middlewareMode: true, hmr: false },
  });
  try {
    const { resolveSmartNavigation } = await vite.ssrLoadModule("/src/modules/smartNavigation.js");
    for (const [text, targetKey] of [
      ["帮我巡检所有数据库", "inspect.run"],
      ["生成售后分析报告", "report.unified"],
      ["采集慢 SQL", "perf.slowsql"],
      ["新增数据库实例", "asset.center"],
    ]) {
      const result = await resolveCustomerRequirement({
        text, instanceId: 7,
        dispatch: async () => ({ action: "navigate", source: "local" }),
        resolveNavigation: (value) => resolveSmartNavigation(value),
      });
      assert.equal(result.mode, "manual");
      assert.equal(result.navigation?.targetKey, targetKey, text);
    }
  } finally {
    await vite.close();
  }
});

test("a stale login or replaced request cannot open a late result", async () => {
  for (const invalidate of [
    (h) => { h.bindings.isAuthSessionCurrent = () => false; },
    (h) => { h.bindings.assistantRequestEpoch.current++; },
  ]) {
    let settle;
    const h = harness("查询任务", () => new Promise((resolve) => { settle = resolve; }));
    // The predicate closes over the bindings object so a login change is observed.
    h.bindings.isAuthSessionCurrent = () => h.state.loginValid !== false;
    const run = new Function(...Object.keys(h.bindings), `return (${functions.runDiagnosis});`)(...Object.values(h.bindings));
    const pending = run();
    invalidate(h);
    h.state.loginValid = false;
    settle({ action: "executed", source: "ai", plan: { plan_id: "late" } });
    await pending;
    assert.equal(h.state.plan, undefined);
    assert.equal(h.state.workspaceOpen, undefined);
  }
});


test("a dropped dispatch response is recovered from the request receipt, not re-modeled", async () => {
  const planId = `pl${"c".repeat(26)}`;
  let reads = 0;
  const value = await resolveCustomerRequirement({
    text: "列出巡检任务", instanceId: 7,
    dispatch: async () => { throw new TypeError("network down"); },
    readReceipt: async (requestId) => {
      reads += 1;
      assert.match(requestId, /^[A-Za-z0-9._:-]{8,}$/);
      return { action: "executed", plan: { plan_id: planId }, capability: { capability_id: "inspection.run.query" } };
    },
    resolveNavigation: () => null,
  });
  assert.equal(reads, 1);
  assert.equal(value.mode, "plan");
  assert.equal(value.planId, planId);
});

test("a missing receipt after transport loss remains pending on the original request", async () => {
  const value = await resolveCustomerRequirement({
    text: "列出巡检任务", instanceId: 7,
    dispatch: async () => { throw new TypeError("network down"); },
    readReceipt: async () => { throw Object.assign(new Error("missing"), { status: 404 }); },
    resolveNavigation: () => ({ targetKey: "inspect.tasks", title: "巡检任务", targetDbType: "mysql" }),
  });
  assert.equal(value.mode, "pending");
  assert.match(value.requestId, /^[A-Za-z0-9._:-]{8,}$/);
});


test("the page-provided request id is sent and retained on every branch", async () => {
  const seen = [];
  const value = await resolveCustomerRequirement({
    text: "列出巡检任务", instanceId: 7, requestId: "req-fixed-123",
    dispatch: async (payload) => {
      seen.push(payload.request_id);
      return { action: "executed", plan: { plan_id: `pl${"d".repeat(26)}` }, capability: { capability_id: "inspection.run.query" } };
    },
    resolveNavigation: () => null,
  });
  assert.deepEqual(seen, ["req-fixed-123"]);
  assert.equal(value.requestId, "req-fixed-123");
});

test("clarification preserves the prompt, moves instance outside answers, and submits under a new request id", async () => {
  const questions = [
    { key: "scope", label: "影响范围", type: "text", required: true },
    { key: "window", label: "时间窗", type: "datetime", required: true },
    { key: "instance_id", label: "目标实例", type: "instance", required: true },
  ];
  const h = harness("客户现场出现问题，需要进行端到端根因分析", (payload) => payload.answers
    ? ({ action: "executed", plan: { plan_id: "pl-clarified" }, capability: { capability_id: "diagnosis.root_cause.execute" } })
    : ({ action: "clarify", questions, answers: { scope: "支付" }, goal: "补齐诊断范围", reason: "需要上下文" }));
  await h.run();
  const firstId = h.state.clarification.requestId;
  assert.equal(h.state.clarification.text, "客户现场出现问题，需要进行端到端根因分析");
  assert.equal(h.state.workspaceOpen, undefined);

  const submission = buildClarificationSubmission({
    questions,
    values: { scope: " 支付链路 ", window: "2026-10-23T10:00", instance_id: 9 },
    selectedInstanceId: 7,
    instances: h.bindings.dbInstances,
  });
  assert.deepEqual(submission, {
    answers: { scope: "支付链路", window: "2026-10-23T10:00" },
    instanceId: 9,
  });
  await h.run({ answers: submission.answers, targetInstanceId: submission.instanceId,
    text: h.state.clarification.text });
  const second = h.payloads[1];
  assert.notEqual(second.request_id, firstId);
  assert.equal(second.text, "客户现场出现问题，需要进行端到端根因分析");
  assert.equal(second.instance_id, 9);
  assert.deepEqual(second.answers, submission.answers);
  assert.equal(Object.hasOwn(second.answers, "instance_id"), false);
  assert.equal(h.state.instanceId, 9);
  assert.equal(h.state.plan.planId, "pl-clarified");
});

test("refresh recovery restores clarification text and the next POST keeps the original requirement", async () => {
  const text = "客户现场出现问题，需要进行端到端根因分析";
  const questions = [{ key: "impact_scope", label: "影响范围", type: "text", required: true }];
  const sharedStorage = new Map();
  const first = harness(text, () => ({ action: "clarify", questions, answers: { symptom: "订单提交超时" },
    goal: "补齐范围", reason: "请补充影响范围" }), null, sharedStorage);
  await first.run();
  const originalRequestId = first.state.clarification.requestId;
  assert.equal(first.journal().text, text);

  const refreshed = harness("", (payload) => ({ action: "executed", plan: { plan_id: "pl-after-refresh" },
    capability: { capability_id: "diagnosis.root_cause.execute" }, received: payload.text }), null, sharedStorage);
  refreshed.state.receipt = (requestId) => {
    assert.equal(requestId, originalRequestId);
    return { action: "clarify", questions, answers: { symptom: "订单提交超时" },
      goal: "补齐范围", reason: "请补充影响范围", request_id: requestId };
  };
  const cleanup = refreshed.recover();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(refreshed.state.problemText, text);
  assert.equal(refreshed.state.clarification.text, text);
  assert.equal(refreshed.payloads.length, 0, "mount recovery reads the receipt without another POST");

  const submission = buildClarificationSubmission({ questions,
    initialAnswers: refreshed.state.clarification.answers,
    values: { ...refreshed.state.clarification.answers, impact_scope: "全部客户" },
    selectedInstanceId: 7, instances: refreshed.bindings.dbInstances });
  await refreshed.run({ answers: submission.answers, targetInstanceId: submission.instanceId,
    text: refreshed.state.clarification.text });
  assert.equal(refreshed.payloads.length, 1);
  assert.notEqual(refreshed.payloads[0].request_id, originalRequestId);
  assert.equal(refreshed.payloads[0].text, text);
  assert.deepEqual(refreshed.payloads[0].answers, { symptom: "订单提交超时", impact_scope: "全部客户" });
  cleanup?.();
});

test("dispatch journal keeps bounded text for the full durable queue window and reads legacy entries", () => {
  const storage = new Map();
  const browserStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const at = 2_000_000_000_000;
  const session = { loginEpoch: "a".repeat(32) };
  assert.equal(writeJournal({ requestId: "req-journal-123", instanceId: 7, session, text: "现场故障", at }, browserStorage), true);
  assert.equal(readJournal(browserStorage, at + 24 * 60 * 60 * 1000).text, "现场故障");
  assert.equal(readJournal(browserStorage, at + ASSISTANT_DISPATCH_JOURNAL_TTL + 1), null);
  assert.equal(writeJournal({ requestId: "req-journal-456", instanceId: 7, session, text: "x".repeat(2001), at }, browserStorage), false);

  storage.set(ASSISTANT_DISPATCH_JOURNAL_KEY, JSON.stringify({ version: 1, requestId: "req-legacy-123",
    instanceId: 7, loginEpoch: session.loginEpoch, at }));
  assert.equal(readJournal(browserStorage, at).text, "");
});

test("two clarification rounds retain previously supplied fields instead of asking for them again", async () => {
  const text = "客户现场出现问题，需要进行端到端根因分析";
  const timeQuestion = [{ key: "time_window", label: "发生时间", type: "datetime", required: true }];
  const impactQuestion = [{ key: "impact_scope", label: "影响范围", type: "text", required: true }];
  const h = harness(text, (payload) => {
    if (!payload.answers) return { action: "clarify", questions: timeQuestion,
      answers: { symptom: "订单提交超时" }, reason: "请补充时间" };
    if (!payload.answers.impact_scope) return { action: "clarify", questions: impactQuestion,
      answers: payload.answers, reason: "请补充影响范围" };
    return { action: "executed", plan: { plan_id: "pl-two-rounds" },
      capability: { capability_id: "diagnosis.root_cause.execute" } };
  });

  await h.run();
  const first = buildClarificationSubmission({ questions: timeQuestion,
    initialAnswers: h.state.clarification.answers,
    values: { ...h.state.clarification.answers, time_window: "2026-10-23T10:00" },
    selectedInstanceId: 7, instances: h.bindings.dbInstances });
  assert.deepEqual(first.answers, { symptom: "订单提交超时", time_window: "2026-10-23T10:00" });
  await h.run({ answers: first.answers, targetInstanceId: first.instanceId, text });

  const second = buildClarificationSubmission({ questions: impactQuestion,
    initialAnswers: h.state.clarification.answers,
    values: { ...h.state.clarification.answers, impact_scope: "支付链路全部客户" },
    selectedInstanceId: 7, instances: h.bindings.dbInstances });
  assert.deepEqual(second.answers, { symptom: "订单提交超时", time_window: "2026-10-23T10:00",
    impact_scope: "支付链路全部客户" });
  await h.run({ answers: second.answers, targetInstanceId: second.instanceId, text });

  assert.equal(h.payloads.length, 3);
  assert.deepEqual(h.payloads[1].answers, first.answers);
  assert.deepEqual(h.payloads[2].answers, second.answers);
  assert.equal(h.state.plan.planId, "pl-two-rounds");
});

test("existing clarification answers are bounded scalars and never carry an instance field", () => {
  assert.deepEqual(normalizeClarificationAnswers({ symptom: " timeout ", attempts: 2, acknowledged: true,
    instance_id: 99, selected_instance_id: 98, instance: 97 }),
  { answers: { symptom: "timeout", attempts: 2, acknowledged: true } });
  assert.match(normalizeClarificationAnswers({ nested: { unsafe: true } }).error, /无效/);
  assert.match(normalizeClarificationAnswers({ attempts: 1.5 }).error, /无效/);
  assert.match(normalizeClarificationAnswers({ symptom: "x".repeat(2001) }).error, /无效/);
  assert.match(normalizeClarificationAnswers(Object.fromEntries(Array.from({ length: 11 }, (_, index) => [`k${index}`, index]))).error, /过多/);
});

test("pending requests retain one journal, block duplicate POST, and recover by GET only", async () => {
  const h = harness("查询正在执行的任务", () => { throw new TypeError("network down"); });
  h.state.receipt = () => { throw Object.assign(new Error("missing"), { status: 404 }); };
  await h.run();
  const requestId = h.state.pending.requestId;
  assert.equal(h.payloads.length, 1);
  assert.equal(h.journal().requestId, requestId);

  await h.run();
  assert.equal(h.payloads.length, 1, "same unresolved requirement does not get a new POST key");
  assert.ok(h.events.some(([name, value]) => name === "warning" && /回查原请求/.test(value)));

  await h.run({ recoverOnly: true, requestId, targetInstanceId: 7, text: "查询正在执行的任务" });
  assert.equal(h.payloads.length, 1, "recoverOnly never dispatches another POST");
  assert.equal(h.events.filter(([name]) => name === "receipt").length, 2);
  assert.equal(h.state.pending.requestId, requestId);
  assert.equal(h.journal().requestId, requestId);
});

test("a terminal failed receipt releases the pending lock and keeps the failure visible", async () => {
  const h = harness("查询正在执行的任务", () => { throw new TypeError("network down"); });
  h.state.receipt = () => null;
  await h.run();
  const requestId = h.state.pending.requestId;
  h.state.receipt = () => ({ action: "failed", reason: "执行失败", reason_code: "diagnosis_failed" });
  await h.run({ recoverOnly: true, requestId, targetInstanceId: 7, text: "查询正在执行的任务" });
  assert.equal(h.state.pending, null);
  assert.equal(h.journal(), null);
  assert.ok(h.events.some(([name, value]) => name === "error" && value === "执行失败"));
});

test("a response cannot publish after another request replaces its journal key", async () => {
  let finish;
  const h = harness("查询任务", () => new Promise((resolve) => { finish = resolve; }));
  const operation = h.run();
  const oldId = h.journal().requestId;
  h.bindings.writeAssistantDispatchJournal({ requestId: "req-replacement-123", instanceId: 7,
    session: h.bindings.getAuthSession() });
  finish({ action: "executed", plan: { plan_id: "late" }, capability: { capability_id: "inspection.run.query" } });
  await operation;
  assert.notEqual(h.journal().requestId, oldId);
  assert.equal(h.state.plan, undefined);
});

test("mount recovery and RCA detail callbacks are guarded by current request and context", () => {
  assert.match(source, /recoveryEpoch === dispatchEpochRef\.current/);
  assert.match(source, /currentAssistantDispatchJournal\(\{ requestId: saved\.requestId/);
  assert.match(source, /\(recoveryInstanceRef\.current \|\| null\) === saved\.instanceId/);
  assert.match(source, /onIncidentReady=\{openPlanIncident\}/);
  assert.match(source, /incidentContextRef\.current === context/);
});
