import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";
import { parse } from "@babel/parser";
import { resolveCustomerRequirement } from "../src/modules/assistantRequirement.js";
import { assistantPlanApprovalExpired } from "../src/modules/assistantPlanCustomer.js";
import {
  clearAssistantDispatchJournal as clearJournal,
  currentAssistantDispatchJournal as currentJournal,
  writeAssistantDispatchJournal as writeJournal,
} from "../src/modules/assistantDispatchJournal.js";

// Source-extracted hook/callback regressions, NOT React/DOM or browser E2E.
// Execute production function bodies and JSX guards; replace only hooks and I/O.
function sourceFile(name) {
  const source = readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
  return { source, ast: parse(source, { sourceType: "module", plugins: ["jsx"] }) };
}

function findNode(node, predicate) {
  if (!node || typeof node !== "object") return undefined;
  if (predicate(node)) return node;
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      if (child && typeof child === "object") {
        const found = findNode(child, predicate);
        if (found) return found;
      }
    }
  }
}

function component(file, name) {
  const node = file.ast.program.body.find((item) => item.declaration?.id?.name === name)?.declaration;
  assert.ok(node, `Production component ${name} exists`);
  return node;
}

function declaration(node, name) {
  return node.body.body.find((item) => item.type === "VariableDeclaration"
    && item.declarations.some((value) => value.id.name === name
      || (value.id.type === "ArrayPattern" && value.id.elements.some((element) => element?.name === name))));
}

function loadBindings(name, names, dependencies = {}) {
  const file = sourceFile(name);
  const body = file.ast.program.body.filter((node) => node.type !== "ImportDeclaration")
    .map((node) => {
      const value = node.declaration || node;
      return file.source.slice(value.start, value.end);
    }).join("\n");
  return new Function(...Object.keys(dependencies), `${body}\nreturn { ${names.join(",")} };`)(...Object.values(dependencies));
}

const state = loadBindings("modules/assistantPlanState.js", ["planPresentation"]);
const workspaceFile = sourceFile("AssistantPlanWorkspace.jsx");
const workspaceNode = component(workspaceFile, "AssistantPlanWorkspace");
const parentFile = sourceFile("DiagnosisCenterPage.jsx");
const parentNode = component(parentFile, "DiagnosisCenterPage");

function workspaceBody() {
  const statements = workspaceNode.body.body.filter((node) => node.type !== "ReturnStatement"
    && node !== declaration(workspaceNode, "historyColumns"));
  const names = statements.filter((node) => node.type === "VariableDeclaration")
    .flatMap((node) => node.declarations.flatMap((value) => value.id.type === "ArrayPattern"
      ? value.id.elements.filter(Boolean).map((item) => item.name) : [value.id.name]));
  const ownerGuard = findNode(workspaceNode, (node) => node.type === "JSXExpressionContainer"
    && node.expression.type === "LogicalExpression" && node.expression.right.type === "JSXElement"
    && node.expression.right.openingElement.attributes.some((item) => item.name?.name === "onClick"
      && workspaceFile.source.slice(item.start, item.end).includes("run(plan)")));
  assert.ok(ownerGuard, "Use the actual frozen-plan button guard, not a test copy");
  const resultBranch = findNode(workspaceNode, (node) => node.type === "ConditionalExpression"
    && findNode(node.consequent, (child) => child === ownerGuard));
  assert.ok(resultBranch, "Preserve the enclosing result-tab rendering guard");
  const helpers = workspaceFile.ast.program.body.filter((node) => node.type === "VariableDeclaration"
    || (node.type === "FunctionDeclaration" && node.id.name !== "InlineResult"))
    .map((node) => workspaceFile.source.slice(node.start, node.end)).join("\n");
  const validationButton = findNode(workspaceNode, (node) => node.type === "JSXElement"
    && node.openingElement.name.name === "Button"
    && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "执行前复核"));
  assert.ok(validationButton, "Use the actual manual validation button");
  const attribute = (node, name) => node.openingElement.attributes.find((item) => item.name?.name === name)?.value.expression;
  assert.equal(attribute(validationButton, "onClick").name, "validatePlan");
  assert.equal(attribute(validationButton, "icon").openingElement.name.name, "SafetyCertificateOutlined");
  const validationAlert = findNode(workspaceNode, (node) => node.type === "JSXElement"
    && node.openingElement.name.name === "Alert"
    && node.openingElement.attributes.some((item) => item.name?.name === "message"
      && workspaceFile.source.slice(item.start, item.end).includes("复核结论")));
  assert.ok(validationAlert, "Use the actual validation tone and message expressions");
  const expression = (node, name) => { const value = attribute(node, name); return workspaceFile.source.slice(value.start, value.end); };
  return `${helpers}\nconst ${workspaceFile.source.slice(workspaceNode.params[0].start, workspaceNode.params[0].end)} = props;
    ${statements.map((node) => workspaceFile.source.slice(node.start, node.end)).join("\n")}
    return { ${names.join(",")}, canExecuteSaved: Boolean(${workspaceFile.source.slice(resultBranch.test.start, resultBranch.test.end)})
      && Boolean(${workspaceFile.source.slice(ownerGuard.expression.left.start, ownerGuard.expression.left.end)}),
      validationTone: ${expression(validationAlert, "type")}, validationMessage: ${expression(validationAlert, "message")},
      validationDisabled: ${expression(validationButton, "disabled")}, executeSavedDisabled: ${expression(ownerGuard.expression.right, "disabled")} };`;
}

function parentBody() {
  const pick = (name) => {
    const node = declaration(parentNode, name);
    assert.ok(node, `Production declaration ${name} exists`);
    return parentFile.source.slice(node.start, node.end);
  };
  const epochEffects = parentNode.body.body.filter((node) => node.type === "ExpressionStatement"
    && node.expression.callee?.name === "useEffect"
    && parentFile.source.slice(node.start, node.end).includes("assistantRequestEpoch"));
  assert.ok(epochEffects.length, "Include the real instance-switch epoch effect");
  const manual = findNode(parentNode, (node) => node.type === "JSXAttribute" && node.name.name === "onOpenPlans");
  const workspace = findNode(parentNode, (node) => node.type === "JSXOpeningElement" && node.name.name === "AssistantPlanWorkspace");
  const close = workspace.attributes.find((node) => node.name?.name === "onClose");
  assert.ok(manual?.value?.expression && close?.value?.expression);
  const expression = (node) => parentFile.source.slice(node.value.expression.start, node.value.expression.end);
  return `const { instanceId } = props;
    ${["assistantDispatchBusyRef", "planWorkspaceOpen", "planRequest", "clarification", "pendingDispatch", "entryMode",
      "assistantRequestEpoch", "running", "aiDegradationNotice"].map(pick).join("\n")}
    ${epochEffects.map((node) => parentFile.source.slice(node.start, node.end)).join("\n")}
    ${pick("runDiagnosis")}
    return { planWorkspaceOpen, planRequest, assistantRequestEpoch, running, runDiagnosis,
      manualOpen: ${expression(manual)}, close: ${expression(close)} };`;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class FakeClock {
  now = 0;
  sequence = 0;
  timers = new Map();
  setTimeout = (callback, delay) => {
    const id = ++this.sequence;
    this.timers.set(id, { callback, at: this.now + delay });
    return id;
  };
  clearTimeout = (id) => { this.timers.delete(id); };
}

// Model committed state/effect dependency changes and cleanup, not React rendering.
class HookHarness {
  slots = [];
  cursor = 0;
  effects = [];
  dirty = true;
  clock = new FakeClock();
  constructor(body, dependencies, props) {
    this.props = props;
    this.dependencies = {
      ...dependencies,
      useState: (initial) => {
        const index = this.cursor++;
        this.slots[index] ??= { value: typeof initial === "function" ? initial() : initial };
        return [this.slots[index].value, (value) => {
          const next = typeof value === "function" ? value(this.slots[index].value) : value;
          if (!Object.is(next, this.slots[index].value)) {
            this.slots[index].value = next;
            this.dirty = true;
          }
        }];
      },
      useRef: (initial) => {
        const index = this.cursor++;
        this.slots[index] ??= { ref: { current: initial } };
        return this.slots[index].ref;
      },
      useEffect: (callback, deps) => {
        const index = this.cursor++;
        const previous = this.slots[index];
        if (!previous || deps.some((value, i) => !Object.is(value, previous.deps[i]))) {
          const slot = { deps, cleanup: previous?.cleanup };
          this.slots[index] = slot;
          this.effects.push(() => { slot.cleanup?.(); slot.cleanup = callback(); });
        }
      },
      setTimeout: this.clock.setTimeout,
      clearTimeout: this.clock.clearTimeout,
    };
    this.evaluate = new Function(...Object.keys(this.dependencies), "props", body);
    this.flush();
  }
  flush() {
    for (let count = 0; this.dirty || this.effects.length; count += 1) {
      assert.ok(count < 100, "Hook harness converges");
      if (this.dirty) {
        this.dirty = false;
        this.cursor = 0;
        this.snapshot = this.evaluate(...Object.values(this.dependencies), this.props);
      }
      this.effects.splice(0).forEach((callback) => callback());
    }
  }
  async settle() {
    for (let i = 0; i < 24; i += 1) { await Promise.resolve(); this.flush(); }
  }
  update(props) { this.props = { ...this.props, ...props }; this.dirty = true; this.flush(); }
  async advance(milliseconds) {
    await this.settle();
    const until = this.clock.now + milliseconds;
    for (let count = 0; ; count += 1) {
      const next = [...this.clock.timers.entries()].filter(([, timer]) => timer.at <= until)
        .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
      if (!next) break;
      assert.ok(count < 1000, "Fake timer advancement is bounded");
      this.clock.now = next[1].at;
      this.clock.timers.delete(next[0]);
      next[1].callback();
      await this.settle();
    }
    this.clock.now = until;
    await this.settle();
  }
  unmount() { this.slots.forEach((slot) => slot.cleanup?.()); }
}

function authentication() {
  const storage = new Map();
  const auth = loadBindings("modules/authStorage.js", ["getAuthSession", "isAuthSessionCurrent", "saveAuthToken", "clearAuthToken"], {
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
  });
  auth.saveAuthToken("synthetic-lifecycle-session");
  return auth;
}

const capabilityA = { capability_id: "inspection.run.query", version: 1, definition_version: 1, definition_hash: "read-a",
  kind: "read", execution: "server", plan_executable: true, availability: "available", authorized_engines: ["mysql"] };
const capabilityB = { ...capabilityA, capability_id: "inspection.template.read", definition_hash: "read-b" };
const rootCauseCapability = { capability_id: "diagnosis.root_cause.execute", version: 1, definition_version: 1,
  definition_hash: "root-cause-v1", kind: "collect", execution: "server", handler_status: "wired",
  plan_executable: true, availability: "available", requires_instance: true, authorized_engines: ["mysql"],
  output_schema: "diagnosis.root_cause_result/v1" };
const inspectionExecuteCapability = { capability_id: "inspection.run.execute", version: 2, definition_version: 2,
  definition_hash: "inspection-run-v2", kind: "execute", execution: "server", handler_status: "wired",
  plan_executable: true, availability: "available", requires_instance: true, authorized_engines: ["mysql"],
  output_schema: "inspection.plan_result/v1" };
const reportDocumentCapability = { capability_id: "report.document.generate", version: 3, definition_version: 3,
  definition_hash: "report-document-v3", kind: "collect", execution: "server", handler_status: "wired",
  plan_executable: true, availability: "available", requires_instance: true, authorized_engines: ["mysql"],
  output_schema: "report.document_result/v1" };
const commonEffectContracts = [
  ["monitor.alert.sync", "sync", false], ["monitor.capacity.execute", "collect", true],
  ["monitor.observability.bind", "bind", false], ["performance.workload.collect", "collect", true],
  ["performance.sql_profile.collect", "collect", true], ["performance.index.collect", "collect", true],
  ["performance.table_profile.collect", "collect", true], ["performance.baseline.collect", "collect", true],
  ["performance.capacity.collect", "collect", true], ["operations.startup.plan", "plan", true],
  ["operations.shutdown.plan", "plan", true], ["operations.switchover.plan", "plan", true],
  ["governance.candidate.scan", "scan", true], ["protection.backup.plan", "plan", true],
  ["protection.restore.plan", "plan", true], ["integration.zabbix.sync", "sync", false],
  ["integration.prometheus.sync", "sync", false],
].map(([capability_id, kind, requires_instance]) => ({ capability_id, kind, requires_instance,
  version: 3, definition_version: 3, definition_hash: `${capability_id}-v3`, execution: "server", handler_status: "wired",
  plan_executable: true, availability: "available", authorized_engines: ["mysql"], output_schema: "capability.effect_result/v1" }));
const requestA = { capability: capabilityA, instanceId: 1 };
const requestB = { capability: capabilityB, instanceId: 2 };
const instances = [{ id: 1, db_type: "mysql" }, { id: 2, db_type: "mysql" }];
const planId = `pl${"a".repeat(26)}`;
function savedPlan(overrides = {}) {
  return { schema: "capability.plan_read/v1", plan_id: planId, capability_id: capabilityA.capability_id,
    definition_version: 1, requested_by: 7, instance_id: 1, status: "validated", outcome: null,
    approval_state: "not_required", steps: [], ...overrides };
}
function effectPlan(capability, overrides = {}) {
  return savedPlan({ capability_id: capability.capability_id, definition_version: capability.definition_version,
    db_type: "mysql", steps: [{ seq: 1, title: capability.capability_id, capability_id: capability.capability_id,
      definition_version: capability.definition_version, status: "pending", params: {}, depends_on: [], input_refs: {} }],
    ...overrides });
}
const runningPlan = () => savedPlan({ status: "running" });
const finishedPlan = () => savedPlan({ status: "settled", outcome: "failed" });
const executionBatchId = `CB${"b".repeat(28)}`;
const savedValidation = (overrides = {}) => ({ schema: "capability.plan_validation/v1", plan_id: planId,
  revision: 1, checked_at: "2026-10-20T08:30:00+00:00", plan_status: "validated", can_execute: true,
  issues: [], approval_available: false, requires_recheck: true, ...overrides });

function workspace(overrides = {}, props = {}) {
  const calls = [];
  const auth = authentication();
  const defaults = {
    listAssistantCapabilities: async () => ({ items: [capabilityA, capabilityB] }),
    listAssistantPlans: async () => ({ items: [], total: 0, page: 1, page_size: 10 }),
    getAssistantCapability: async (id) => [capabilityA, capabilityB].find((item) => item.capability_id === id),
    createAssistantPlan: async (payload) => savedPlan({ capability_id: payload.capability_id, instance_id: payload.instance_id }),
    executeAssistantPlan: async () => finishedPlan(),
    getAssistantPlan: async () => savedPlan(),
    getAssistantPlanValidation: async () => savedValidation(),
    currentUser: async () => ({ id: 7 }),
  };
  const wrapped = Object.fromEntries(Object.entries(defaults).map(([name, fallback]) => [name, (...args) => {
    calls.push({ name, args });
    return (overrides[name] || fallback)(...args);
  }]));
  const { currentUser, ...api } = wrapped;
  const harness = new HookHarness(workspaceBody(), { ...state, ...auth, ...api, assistantPlanApprovalExpired,
    authApi: { currentUser }, onAuthExpired: () => () => {} },
    { open: true, onClose: () => {}, instances, instanceId: 1, request: requestA, ...props });
  return Object.assign(harness, { auth, calls, count: (name) => calls.filter((call) => call.name === name).length });
}

function parent(dispatch) {
  const auth = authentication();
  const storage = new Map();
  const browserStorage = { getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
  const unexpected = () => assert.fail("These tests must not enter the client executor");
  const harness = new HookHarness(parentBody(), { ...auth, resolveCustomerRequirement, dispatchAssistantIntent: dispatch,
    problemText: "查询巡检任务", selectedInstance: instances[0], dbInstances: instances, menuItems: undefined,
    resolveSmartNavigation: unexpected, getDispatchReceipt: async () => null,
    writeAssistantDispatchJournal: (value) => writeJournal(value, browserStorage),
    currentAssistantDispatchJournal: (value) => currentJournal(value, browserStorage),
    clearAssistantDispatchJournal: (requestId) => clearJournal(requestId, browserStorage),
    message: { info: unexpected, warning: unexpected, error: unexpected, success: unexpected },
    setWorkspaceStarted: unexpected, setAssistantActionModalOpen: unexpected, setAssistantActionTask: unexpected,
    navigateFromIntent: unexpected, isInstanceProvisioningCommand: unexpected }, { instanceId: 1 });
  return Object.assign(harness, { auth });
}

describe("assistant lifecycle: source-extracted hooks/callbacks, NOT React/DOM E2E", () => {
  test("mount, manual open, close and reopen never create or execute a plan", async () => {
    const h = workspace({}, { open: false, request: null });
    await h.settle();
    assert.equal(h.calls.length, 0);
    h.update({ open: true });
    await h.settle();
    h.update({ open: false });
    h.update({ open: true });
    await h.settle();
    assert.equal(h.count("listAssistantCapabilities"), 2);
    assert.equal(h.count("createAssistantPlan"), 0);
    assert.equal(h.count("executeAssistantPlan"), 0);
    assert.equal(h.count("getAssistantPlanValidation"), 0);
  });

  test("a history entry request selects history once while the engineer default remains new", async () => {
    const historyRequest = { initialTab: "history" };
    const customer = workspace({}, { request: historyRequest, customerMode: true });
    await customer.settle();
    assert.equal(customer.snapshot.tab, "history");
    customer.snapshot.setTab("new");
    await customer.settle();
    customer.snapshot.setBusy(true);
    await customer.settle();
    assert.equal(customer.snapshot.tab, "new", "rerenders do not replay the consumed initial tab");

    const engineer = workspace({}, { request: null });
    await engineer.settle();
    assert.equal(engineer.snapshot.tab, "new");
  });

  test("B arriving during A is visibly queued, applied after A, and still needs confirmation", async () => {
    const completion = deferred();
    const h = workspace({ executeAssistantPlan: () => completion.promise });
    await h.settle();
    const operation = h.snapshot.run();
    await h.settle();
    assert.equal(h.snapshot.busy, true);
    h.update({ open: false });
    h.update({ open: true, request: requestB });
    await h.settle();
    assert.equal(h.snapshot.queuedRequest, true);
    assert.equal(h.snapshot.capabilityId, capabilityA.capability_id);
    completion.resolve(finishedPlan());
    await operation;
    await h.settle();
    assert.equal(h.snapshot.queuedRequest, false);
    assert.equal(h.snapshot.busy, false);
    assert.equal(h.snapshot.capabilityId, capabilityB.capability_id);
    assert.equal(h.snapshot.target, 2);
    assert.equal(h.snapshot.tab, "new");
    assert.equal(h.count("createAssistantPlan"), 1);
    assert.equal(h.count("executeAssistantPlan"), 1);
    await h.advance(90000);
    assert.equal(h.count("createAssistantPlan"), 1, "Queued B is not implicitly created");
  });

  test("busy transitions do not reapply A over a user-edited capability, target or limit", async () => {
    const completion = deferred();
    const h = workspace({ executeAssistantPlan: () => completion.promise });
    await h.settle();
    h.snapshot.setCapabilityId(capabilityB.capability_id);
    h.snapshot.setTarget(2);
    h.snapshot.setPageSize(35);
    await h.settle();
    const operation = h.snapshot.run();
    await h.settle();
    completion.resolve(finishedPlan());
    await operation;
    await h.settle();
    assert.equal(h.snapshot.capabilityId, capabilityB.capability_id);
    assert.equal(h.snapshot.target, 2);
    assert.equal(h.snapshot.pageSize, 35);
    assert.equal(h.snapshot.tab, "result");
    assert.deepEqual(h.calls.find((call) => call.name === "createAssistantPlan").args[0], {
      capability_id: capabilityB.capability_id, definition_version: 1, instance_id: 2,
      params: { page: 1, page_size: 35 }, trigger_type: "assistant",
    });
  });

  for (const failure of [false, true]) {
    test(`deferred detail A cannot ${failure ? "publish an error" : "replace B"} after close/reopen`, async () => {
      const response = deferred();
      const h = workspace({ getAssistantPlan: () => response.promise });
      await h.settle();
      const operation = h.snapshot.readPlan(planId);
      h.update({ open: false });
      h.update({ open: true, request: requestB });
      await h.settle();
      if (failure) response.reject(new TypeError("old connection failed"));
      else response.resolve(runningPlan());
      await operation;
      await h.settle();
      assert.equal(h.snapshot.plan, null);
      assert.equal(h.snapshot.tab, "new");
      assert.equal(h.snapshot.target, 2);
      assert.equal(h.snapshot.actionError, "");
    });
  }

  test("an in-flight poll from A cannot steal B's confirmation tab", async () => {
    const response = deferred();
    let reads = 0;
    const h = workspace({ getAssistantPlan: () => ++reads === 1 ? Promise.resolve(runningPlan()) : response.promise });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    await h.advance(1500);
    h.update({ request: requestB });
    await h.settle();
    response.resolve(runningPlan());
    await h.settle();
    assert.equal(h.snapshot.tab, "new");
    assert.equal(h.snapshot.capabilityId, capabilityB.capability_id);
    await h.advance(3000);
    assert.equal(reads, 2);
  });

  test("the actual boundedWait preflight expires at 90s and ignores a later response", async () => {
    const response = deferred();
    const h = workspace({ getAssistantCapability: () => response.promise });
    await h.settle();
    const operation = h.snapshot.run();
    await h.settle();
    h.update({ open: false });
    h.update({ open: true });
    await h.advance(89999);
    assert.equal(h.snapshot.busy, true);
    await h.advance(1);
    await operation;
    assert.equal(h.snapshot.busy, false);
    assert.match(h.snapshot.actionError, /超时/);
    assert.equal(h.snapshot.uncertain, false, "No write was submitted");
    response.resolve(capabilityA);
    await h.settle();
    assert.equal(h.count("createAssistantPlan"), 0);
    assert.equal(h.count("executeAssistantPlan"), 0);
  });

  for (const stage of ["createAssistantPlan", "executeAssistantPlan"]) {
    for (const status of [502, 503, 504]) {
      test(`${stage} HTTP ${status} after submission stays uncertain without automatic retry`, async () => {
        const h = workspace({ [stage]: async () => { throw Object.assign(new Error(`HTTP ${status}`), { status }); } });
        await h.settle();
        await h.snapshot.run();
        await h.settle();
        assert.equal(h.snapshot.busy, false);
        assert.equal(h.snapshot.uncertain, true);
        assert.equal(h.snapshot.canExecuteSaved, false);
        if (stage === "executeAssistantPlan") {
          assert.equal(h.snapshot.plan.status, "validated", "Keep the persisted snapshot separate from UI uncertainty");
          assert.equal(h.snapshot.view.status, "unknown");
          assert.match(h.snapshot.view.summary, /尚未确认/);
          assert.doesNotMatch(h.snapshot.view.summary, /计划尚未执行/);
        }
        await h.advance(90000);
        assert.equal(h.count(stage), 1);
      });
    }
  }

  test("execute's local 90s deadline also preserves uncertainty without cancelling the server", async () => {
    const response = deferred();
    const h = workspace({ executeAssistantPlan: () => response.promise });
    await h.settle();
    const operation = h.snapshot.run();
    await h.settle();
    await h.advance(90000);
    await operation;
    assert.equal(h.snapshot.uncertain, true);
    assert.equal(h.snapshot.view.status, "unknown");
    assert.equal(h.snapshot.busy, false);
    response.resolve(finishedPlan());
    await h.settle();
    assert.equal(h.snapshot.uncertain, true, "A late response cannot silently settle a timed-out action");
    assert.equal(h.count("executeAssistantPlan"), 1);
  });

  test("preflight HTTP 503 remains a definite unsubmitted failure", async () => {
    const h = workspace({ getAssistantCapability: async () => { throw Object.assign(new Error("unavailable"), { status: 503 }); } });
    await h.settle();
    await h.snapshot.run();
    await h.settle();
    assert.equal(h.snapshot.uncertain, false);
    assert.equal(h.count("createAssistantPlan"), 0);
  });

  test("polling stops at 40 with a notice, and manual refresh resets the notice and budget", async () => {
    const h = workspace({ getAssistantPlan: async () => runningPlan() });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    await h.advance(40 * 1500);
    assert.equal(h.count("getAssistantPlan"), 41, "One manual read plus forty polls");
    assert.equal(h.snapshot.pollBudget.current.count, 40);
    assert.match(h.snapshot.monitorNotice, /上限/);
    assert.equal(h.snapshot.view.status, "running");
    await h.advance(90000);
    assert.equal(h.count("getAssistantPlan"), 41);
    await h.snapshot.readPlan(planId);
    await h.settle();
    assert.equal(h.snapshot.monitorNotice, "");
    assert.equal(h.snapshot.pollBudget.current.count, 0);
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 43);
    assert.equal(h.snapshot.pollBudget.current.count, 1);
  });

  test("poll disconnect is visible and manual refresh recovers monitoring", async () => {
    let fail = false;
    const h = workspace({ getAssistantPlan: async () => {
      if (fail) throw new TypeError("offline");
      return runningPlan();
    } });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    fail = true;
    await h.advance(1500);
    assert.match(h.snapshot.monitorNotice, /自动刷新失败/);
    assert.equal(h.snapshot.plan.status, "running");
    fail = false;
    await h.snapshot.readPlan(planId);
    await h.settle();
    assert.equal(h.snapshot.monitorNotice, "");
    assert.equal(h.snapshot.actionError, "");
    await h.advance(1500);
    assert.equal(h.snapshot.pollBudget.current.count, 1);
  });

  test("the actual JSX execute guard hides unknown actors and non-owner administrators", async () => {
    const actor = deferred();
    const h = workspace({ currentUser: () => actor.promise });
    await h.settle();
    h.snapshot.publish(savedPlan());
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    actor.resolve({ id: 9, role: "admin" });
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    h.snapshot.publish(savedPlan({ requested_by: 9 }));
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, true);
    h.snapshot.publish(savedPlan({ requested_by: 9, definition_version: 2 }));
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    h.snapshot.publish(savedPlan({ requested_by: 9 }));
    await h.settle();
    h.snapshot.setUncertain(true);
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    assert.equal(h.count("executeAssistantPlan"), 0);
  });

  for (const capability of [rootCauseCapability, inspectionExecuteCapability, reportDocumentCapability]) {
    test(`${capability.capability_id} uses the frozen-plan execution path only with its exact live contract and visible target`, async () => {
      const h = workspace({ listAssistantCapabilities: async () => ({ items: [capability] }) });
      await h.settle();
      h.snapshot.publish(effectPlan(capability));
      await h.settle();
      assert.equal(h.snapshot.canExecuteSaved, true);
      await h.snapshot.run(effectPlan(capability));
      await h.settle();
      assert.equal(h.count("executeAssistantPlan"), 1, "execution uses the existing confirmed plan endpoint");

      h.snapshot.publish(effectPlan(capability, { db_type: "oracle" }));
      await h.settle();
      assert.equal(h.snapshot.canExecuteSaved, false, "plan engine must match the visible instance");

      h.snapshot.publish(effectPlan(capability, { definition_version: capability.definition_version + 1 }));
      await h.settle();
      assert.equal(h.snapshot.canExecuteSaved, false, "persisted primary version is exact");
    });
  }

  test("all 17 registered v3 effects use the approval-gated frozen execution path with exact target scope", async () => {
    const h = workspace({ listAssistantCapabilities: async () => ({ items: commonEffectContracts }) });
    await h.settle();
    for (const capability of commonEffectContracts) {
      const saved = effectPlan(capability, { instance_id: capability.requires_instance ? 1 : null,
        db_type: capability.requires_instance ? "mysql" : null, status: "approved", approval_state: "approved",
        approval_available: true, binding_hash: "a".repeat(64), expires_at: "2099-10-20T09:30:00+00:00" });
      h.snapshot.publish(saved);
      await h.settle();
      assert.equal(h.snapshot.canExecuteSaved, true, capability.capability_id);
      await h.snapshot.run(saved);
      await h.settle();
    }
    assert.equal(h.count("executeAssistantPlan"), commonEffectContracts.length);

    const scoped = commonEffectContracts.find((item) => item.requires_instance);
    h.snapshot.publish(effectPlan(scoped, { definition_version: 2 }));
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false, "legacy v2 effect never executes");
    const global = commonEffectContracts.find((item) => !item.requires_instance);
    h.snapshot.publish(effectPlan(global, { instance_id: 1, db_type: "mysql" }));
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false, "global effects cannot borrow the selected instance");
    h.unmount();
  });

  test("a queued root-cause execute response disables replay, polls the original plan, and publishes its settled result", async () => {
    const approved = effectPlan(rootCauseCapability, { status: "approved", approval_state: "approved",
      approval_available: true, binding_hash: "a".repeat(64), expires_at: "2026-10-20T09:30:00+00:00" });
    const queued = { ...approved, execution_batch_id: executionBatchId, execution_queue_state: "queued" };
    const running = effectPlan(rootCauseCapability, { status: "running", approval_state: "approved",
      approval_available: true, binding_hash: "a".repeat(64), expires_at: "2026-10-20T09:30:00+00:00",
      execution_batch_id: executionBatchId, execution_queue_state: "running",
      steps: [{ ...approved.steps[0], status: "running" }] });
    const settled = effectPlan(rootCauseCapability, { status: "settled", outcome: "failed",
      approval_state: "approved", approval_available: true, binding_hash: "a".repeat(64),
      expires_at: "2026-10-20T09:30:00+00:00", execution_batch_id: executionBatchId,
      execution_queue_state: "archived", steps: [{ ...approved.steps[0], status: "failed" }] });
    let reads = 0;
    const h = workspace({ listAssistantCapabilities: async () => ({ items: [rootCauseCapability] }),
      executeAssistantPlan: async () => queued, getAssistantPlan: async () => ++reads === 1 ? running : settled });
    await h.settle();
    h.snapshot.publish(approved);
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, true);
    await h.snapshot.run(approved);
    await h.settle();
    assert.equal(h.count("executeAssistantPlan"), 1);
    assert.equal(h.snapshot.plan.status, "approved");
    assert.equal(h.snapshot.plan.execution_queue_state, "queued");
    assert.equal(h.snapshot.canExecuteSaved, false, "queued delivery cannot be submitted twice");
    assert.deepEqual(h.snapshot.queueNotice?.slice(1), ["已进入持久执行队列", "任务正在等待工作节点处理；页面会持续读取原清单状态。"]);
    assert.equal(h.snapshot.statusSummary, "执行已接收，等待工作节点执行，无需重复提交。");
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 1);
    assert.equal(h.snapshot.plan.status, "running");
    assert.equal(h.snapshot.statusSummary, "工作节点正在执行已接收的任务，无需重复提交。");
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 2);
    assert.equal(h.snapshot.plan.status, "settled");
    assert.equal(h.snapshot.plan.outcome, "failed");
    assert.equal(h.snapshot.plan.execution_queue_state, "archived");
    assert.equal(h.snapshot.canExecuteSaved, false);
    await h.advance(3000);
    assert.equal(h.count("getAssistantPlan"), 2, "settled plans stop queue polling");
  });

  test("root-cause execution fails closed for unavailable, unsupported-schema and out-of-scope registrations", async () => {
    for (const changed of [
      { ...rootCauseCapability, availability: "unavailable" },
      { ...rootCauseCapability, output_schema: "object" },
      { ...rootCauseCapability, authorized_engines: ["oracle"] },
    ]) {
      const h = workspace({ listAssistantCapabilities: async () => ({ items: [changed] }) });
      await h.settle();
      h.snapshot.publish(effectPlan(rootCauseCapability));
      await h.settle();
      assert.equal(h.snapshot.canExecuteSaved, false);
    }
  });

  test("report execution rejects the legacy v2 object declaration", async () => {
    const legacy = { ...reportDocumentCapability, version: 2, definition_version: 2, output_schema: "object" };
    const h = workspace({ listAssistantCapabilities: async () => ({ items: [legacy] }) });
    await h.settle();
    h.snapshot.publish(effectPlan(legacy));
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    await h.snapshot.run(effectPlan(legacy));
    assert.equal(h.count("executeAssistantPlan"), 0);
  });

  for (const interaction of ["manualOpen", "close"]) {
    test(`parent ${interaction} callback invalidates deferred model dispatch`, async () => {
      const response = deferred();
      const h = parent(() => response.promise);
      if (interaction === "close") {
        h.snapshot.manualOpen();
        await h.settle();
      }
      const operation = h.snapshot.runDiagnosis();
      h.snapshot[interaction]();
      await h.settle();
      response.resolve({ action: "confirm", source: "ai", capability: capabilityA, plan: { plan_id: planId } });
      await operation;
      await h.settle();
      assert.deepEqual(h.snapshot.planRequest, { initialTab: "history" });
      assert.equal(h.snapshot.planWorkspaceOpen, interaction === "manualOpen");
    });
  }

  test("parent instance-switch effect and same-token auth generation reject old model dispatch", async () => {
    for (const change of ["instance", "session"]) {
      const response = deferred();
      const h = parent(() => response.promise);
      const operation = h.snapshot.runDiagnosis();
      if (change === "instance") h.update({ instanceId: 2 });
      else h.auth.saveAuthToken("synthetic-lifecycle-session");
      response.resolve({ action: "confirm", source: "ai", capability: capabilityA, plan: { plan_id: planId } });
      await operation;
      await h.settle();
      assert.equal(h.snapshot.planWorkspaceOpen, false);
      assert.equal(h.snapshot.planRequest, null);
    }
  });

  test("late detail and actor results are ignored after unmount", async () => {
    const response = deferred(), actor = deferred();
    const h = workspace({ getAssistantPlan: () => response.promise, currentUser: () => actor.promise });
    await h.settle();
    const operation = h.snapshot.readPlan(planId);
    h.unmount();
    response.resolve(runningPlan());
    actor.resolve({ id: 7 });
    await operation;
    await h.settle();
    assert.equal(h.snapshot.plan, null);
    assert.equal(h.snapshot.actorId, null);
  });
});

describe("plan validation: source-extracted hooks/callbacks and JSX expressions, NOT browser E2E", () => {
  test("terminal plans hide the preflight control and its unchecked default", () => {
    const guarded = (kind, marker) => findNode(workspaceNode, (node) => node.type === "LogicalExpression"
      && workspaceFile.source.slice(node.start, node.end).includes("!terminalPlan && !hasDurableExecution(plan)")
      && node.right.type === "JSXElement" && node.right.openingElement.name.name === kind
      && workspaceFile.source.slice(node.right.start, node.right.end).includes(marker));
    assert.ok(guarded("Button", "执行前复核"));
    assert.ok(guarded("Alert", "复核结论：当前可执行"));
    assert.match(workspaceFile.source, /const terminalPlan = plan\?\.status === "settled"/);
  });

  test("root-cause incident navigation is explicit and only attached to the result button", () => {
    const inline = workspaceFile.ast.program.body.find((node) => node.type === "FunctionDeclaration" && node.id.name === "InlineResult");
    assert.ok(inline);
    assert.equal(inline.body.body.some((node) => node.type === "ExpressionStatement"
      && node.expression.callee?.name === "useEffect"), false);
    const button = findNode(inline, (node) => node.type === "JSXElement" && node.openingElement.name.name === "Button"
      && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "查看完整诊断"));
    assert.ok(button);
    const click = button.openingElement.attributes.find((item) => item.name?.name === "onClick")?.value?.expression;
    assert.match(workspaceFile.source.slice(click.start, click.end),
      /onIncidentReady\(\{ incidentId: result\.incidentId, instanceId: result\.instanceId \}\)/);
  });

  test("root-cause result translates known enums, preserves future values, and uses a stable title", () => {
    const labels = workspaceFile.ast.program.body.find((node) => node.type === "VariableDeclaration"
      && node.declarations.some((item) => item.id.name === "RCA_DISPLAY_LABELS"));
    const display = workspaceFile.ast.program.body.find((node) => node.type === "FunctionDeclaration"
      && node.id.name === "rcaDisplay");
    assert.ok(labels);
    assert.ok(display);
    const render = new Function(`${workspaceFile.source.slice(labels.start, labels.end)}
      ${workspaceFile.source.slice(display.start, display.end)}
      return rcaDisplay;`)();
    assert.equal(render("status", "root_caused"), "已形成根因假设");
    assert.equal(render("dataQuality", "good"), "数据完整");
    assert.equal(render("conclusion", "hypotheses_require_validation"), "根因假设待验证");
    assert.equal(render("severity", "warning"), "警告");
    assert.equal(render("layer", "database"), "数据库");
    assert.equal(render("status", "future_server_state"), "future_server_state");
    assert.match(workspaceFile.source,
      /resultStep\.capability_id === "diagnosis\.root_cause\.execute"\s*\? "根因分析"/);
  });

  test("typed report result exposes one artifact download action", () => {
    const inline = workspaceFile.ast.program.body.find((node) => node.type === "FunctionDeclaration" && node.id.name === "InlineResult");
    assert.ok(inline);
    const button = findNode(inline, (node) => node.type === "JSXElement" && node.openingElement.name.name === "Button"
      && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "下载报告"));
    assert.ok(button);
    const click = button.openingElement.attributes.find((item) => item.name?.name === "onClick")?.value?.expression;
    const callback = workspaceFile.source.slice(click.start, click.end);
    assert.match(callback, /downloadReportArtifact\(value\.artifact_id, value\.format/);
    assert.match(callback, /if \(!isActive\(\)\) return/);
    assert.match(workspaceFile.source, /result\.kind === "report_document"/);
  });

  test("common effects have a distinct persisted-result view and plan-only warning", () => {
    assert.match(workspaceFile.source, /result\.kind === "effect"/);
    assert.match(workspaceFile.source, /已生成操作方案，尚未执行变更/);
    assert.match(workspaceFile.source, /没有执行启动、停库、切换、备份或恢复变更/);
  });

  test("approval reads explain result redaction without claiming an unsupported format", () => {
    assert.match(workspaceFile.source,
      /approvalMode\s*\?\s*<Alert type="info" showIcon message="审批视图不展示执行结果，请由请求人在历史清单查看。"/);
    assert.match(workspaceFile.source,
      /if \(approvalMode\) return <Typography\.Text type="secondary">审批视图不展示<\/Typography\.Text>/);
    const resultButton = findNode(workspaceNode, (node) => node.type === "JSXElement"
      && node.openingElement.name.name === "Button"
      && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "查看结果"));
    const disabled = resultButton?.openingElement.attributes.find((item) => item.name?.name === "disabled")?.value?.expression;
    assert.equal(workspaceFile.source.slice(disabled.start, disabled.end), "approvalMode || !row.result_ref");
  });

  for (const failure of [false, true]) {
    for (const timing of ["during", "after"]) {
      test(`old detail ${failure ? "failure" : "success"} ${timing} newer validation cannot replace its plan or feedback`, async () => {
        const detail = deferred(), validation = deferred();
        const h = workspace({ getAssistantPlan: () => detail.promise, getAssistantPlanValidation: () => validation.promise });
        await h.settle();
        h.snapshot.publish(savedPlan());
        h.snapshot.setTab("result");
        await h.settle();
        const oldRead = h.snapshot.readPlan(`pl${"b".repeat(26)}`);
        await h.settle();
        const check = h.snapshot.validatePlan();
        await h.settle();
        const finishOld = async () => {
          if (failure) detail.reject(new Error("old detail failure"));
          else detail.resolve(savedPlan({ plan_id: `pl${"b".repeat(26)}` }));
          await oldRead;
          await h.settle();
          assert.equal(h.snapshot.plan.plan_id, planId);
          assert.equal(h.snapshot.actionError, "");
        };
        if (timing === "during") {
          await finishOld();
          assert.equal(h.snapshot.validationView?.phase, "loading");
        }
        validation.resolve(savedValidation());
        await check;
        await h.settle();
        if (timing === "after") await finishOld();
        assert.equal(h.snapshot.validationView?.phase, "ready");
        assert.equal(h.snapshot.validationTone, "success");
        assert.equal(h.snapshot.tab, "result");
        assert.equal(h.count("createAssistantPlan"), 0);
        assert.equal(h.count("executeAssistantPlan"), 0);
      });
    }
  }

  for (const result of ["success", "failure", "timeout"]) {
    test(`polling pauses throughout slow validation and resumes after ${result} without resetting its budget`, async () => {
      const response = deferred();
      const h = workspace({ getAssistantPlan: async () => runningPlan(), getAssistantPlanValidation: () => response.promise });
      await h.settle();
      await h.snapshot.readPlan(planId);
      await h.settle();
      await h.advance(1500);
      const count = h.count("getAssistantPlan"), budget = h.snapshot.pollBudget.current.count;
      const check = h.snapshot.validatePlan();
      await h.settle();
      await h.advance(result === "timeout" ? 90000 : 4500);
      assert.equal(h.count("getAssistantPlan"), count, "No polling while validation waits");
      assert.equal(h.snapshot.pollBudget.current.count, budget);
      if (result === "success") response.resolve(savedValidation({ plan_status: "running", can_execute: false, issues: [{ code: "plan_not_executable" }] }));
      else if (result === "failure") response.reject(new Error("validation unavailable"));
      await check;
      await h.settle();
      assert.equal(h.snapshot.validationView.phase, result === "success" ? "ready" : "error");
      assert.equal(h.snapshot.validationBusyRef.current, false);
      await h.advance(1499);
      assert.equal(h.count("getAssistantPlan"), count);
      await h.advance(1);
      assert.equal(h.count("getAssistantPlan"), count + 1);
      assert.equal(h.snapshot.pollBudget.current.count, budget + 1);
      await h.advance(1500);
      assert.equal(h.count("getAssistantPlan"), count + 2, "Polling continues beyond the first resumed read");
      if (result === "timeout") {
        response.resolve(savedValidation());
        await h.settle();
        assert.equal(h.snapshot.validationView, null, "Timed-out validation cannot revive after resumed polling");
      }
      h.unmount();
    });
  }

  for (const failure of [false, true]) {
    test(`superseded in-flight poll ${failure ? "error" : "success"} cannot discard validation or prevent the next poll`, async () => {
      const poll = deferred(), validation = deferred();
      let reads = 0;
      const h = workspace({ getAssistantPlan: () => ++reads === 2 ? poll.promise : Promise.resolve(runningPlan()),
        getAssistantPlanValidation: () => validation.promise });
      await h.settle();
      await h.snapshot.readPlan(planId);
      await h.settle();
      await h.advance(1500);
      assert.equal(reads, 2);
      const check = h.snapshot.validatePlan();
      await h.settle();
      if (failure) poll.reject(new Error("obsolete poll failure"));
      else poll.resolve(finishedPlan());
      await h.settle();
      assert.equal(h.snapshot.plan.status, "running");
      assert.equal(h.snapshot.validationView?.phase, "loading");
      assert.equal(h.snapshot.actionError, "");
      assert.equal(h.snapshot.monitorNotice, "");
      await h.advance(3000);
      assert.equal(reads, 2);
      validation.resolve(savedValidation({ plan_status: "running", can_execute: false, issues: [{ code: "plan_not_executable" }] }));
      await check;
      await h.settle();
      assert.equal(h.snapshot.validationView.phase, "ready");
      await h.advance(1500);
      assert.equal(reads, 3);
      assert.equal(h.snapshot.pollBudget.current.count, 2, "Superseded poll still counts; validation grants no new budget");
      h.unmount();
    });
  }

  test("batched validation completion still restarts polling after invalidating an in-flight read", async () => {
    const stale = [];
    let reads = 0;
    const h = workspace({ getAssistantPlan: () => {
      reads += 1;
      if (reads % 2 === 1) return Promise.resolve(runningPlan());
      const response = deferred();
      stale.push(response);
      return response.promise;
    } });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    for (let round = 0; round < 2; round += 1) {
      await h.advance(1500);
      const before = reads;
      await h.snapshot.validatePlan();
      // Commit only the final state, as React may batch loading and completion.
      await h.settle();
      assert.equal(h.snapshot.validationView.phase, "ready");
      await h.advance(1500);
      assert.equal(reads, before + 1, "The ignored old read is not needed to restart monitoring");
      stale[round].resolve(finishedPlan());
      await h.settle();
      assert.equal(h.snapshot.plan.status, "running", "Old response cannot replace the resumed poll result");
    }
    h.unmount();
  });

  test("poll timer checks the validation ref before a loading render can clean up the timer", async () => {
    const response = deferred();
    const h = workspace({ getAssistantPlan: async () => runningPlan(), getAssistantPlanValidation: () => response.promise });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    const [id, timer] = [...h.clock.timers].find(([, value]) => value.at === h.clock.now + 1500);
    const check = h.snapshot.validatePlan();
    h.clock.timers.delete(id);
    timer.callback();
    assert.equal(h.count("getAssistantPlan"), 1);
    assert.equal(h.snapshot.pollBudget.current.count, 0);
    await h.settle();
    response.resolve(savedValidation());
    await check;
    await h.settle();
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 2);
    h.unmount();
  });

  test("closing a paused poll prevents late validation from restarting it; reopen resumes monitoring", async () => {
    const response = deferred();
    const h = workspace({ getAssistantPlan: async () => runningPlan(), getAssistantPlanValidation: () => response.promise });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    const check = h.snapshot.validatePlan();
    await h.settle();
    h.snapshot.closeWorkspace();
    h.update({ open: false });
    response.resolve(savedValidation());
    await check;
    await h.advance(10000);
    assert.equal(h.count("getAssistantPlan"), 1);
    assert.equal(h.snapshot.validationView, null);
    h.update({ open: true });
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 2);
    assert.equal(h.count("executeAssistantPlan"), 0);
    h.unmount();
  });

  test("manual refresh cancels paused validation and monitoring continues without its late response", async () => {
    const response = deferred();
    const h = workspace({ getAssistantPlan: async () => runningPlan(), getAssistantPlanValidation: () => response.promise });
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    const check = h.snapshot.validatePlan();
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.settle();
    assert.equal(h.snapshot.validating, false);
    assert.equal(h.snapshot.validationBusyRef.current, false);
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 3);
    response.resolve(savedValidation());
    await check;
    await h.settle();
    assert.equal(h.snapshot.validationView, null);
    await h.advance(1500);
    assert.equal(h.count("getAssistantPlan"), 4);
    h.unmount();
  });

  test("ordinary detail/history reads never validate; only the manual callback makes one read with no POST", async () => {
    const h = workspace();
    await h.settle();
    await h.snapshot.readPlan(planId);
    await h.snapshot.loadHistory();
    await h.settle();
    const before = structuredClone(h.snapshot.plan);
    assert.equal(h.snapshot.validationView, null);
    assert.match(h.snapshot.validationMessage, /待核对/);
    assert.equal(h.count("getAssistantPlanValidation"), 0);
    await h.snapshot.validatePlan();
    await h.settle();
    assert.equal(h.count("getAssistantPlanValidation"), 1);
    assert.deepEqual(h.calls.find((call) => call.name === "getAssistantPlanValidation").args, [planId]);
    assert.equal(h.count("createAssistantPlan"), 0);
    assert.equal(h.count("executeAssistantPlan"), 0);
    assert.deepEqual(h.snapshot.plan, before);
    assert.equal(h.snapshot.validationTone, "success");
    assert.match(h.snapshot.validationMessage, /当前可执行/);
    assert.equal(h.snapshot.view.status, "validated", "Validation is not execution success");
    assert.match(workspaceFile.source, /复核不代表执行成功或执行授权；实际执行仍需服务端重查/);
  });

  test("blocking codes and the checked status are displayed separately without changing an existing success", async () => {
    const h = workspace({ getAssistantPlanValidation: async () => savedValidation({
      plan_status: "settled", can_execute: false, issues: [{ code: "plan_not_executable" }],
    }) });
    await h.settle();
    const succeeded = savedPlan({ status: "settled", outcome: "succeeded", steps: [{ seq: 1,
      capability_id: capabilityA.capability_id, status: "succeeded", result_ref: { kind: "capability_read",
        capability_id: capabilityA.capability_id, schema: "inspection.run_list/v1", inline: {
          schema: "inspection.run_list/v1", items: [], total: 0, page: 1, page_size: 20,
        } } }] });
    h.snapshot.publish(succeeded);
    await h.settle();
    assert.equal(h.snapshot.view.status, "succeeded");
    await h.snapshot.validatePlan();
    await h.settle();
    assert.deepEqual(h.snapshot.plan, succeeded);
    assert.equal(h.snapshot.view.status, "succeeded");
    assert.equal(h.snapshot.validationTone, "warning");
    assert.match(h.snapshot.validationMessage, /阻断/);
    assert.deepEqual(h.snapshot.validationView.value.issues, [{ code: "plan_not_executable" }]);
    assert.match(workspaceFile.source, /validationView.value.issues.map/);
    assert.match(workspaceFile.source, /displayTime\(validationView.value.checked_at\)/);
  });

  test("can_execute never grants owner, approval, version, capability or uncertainty exceptions", async () => {
    const h = workspace();
    await h.settle();
    for (const overrides of [{ requested_by: 9 }, { approval_state: "approved" }, { definition_version: 2 },
      { capability_id: "future.capability.read" }, { status: "approved" }]) {
      h.snapshot.publish(savedPlan(overrides));
      await h.settle();
      await h.snapshot.validatePlan();
      await h.settle();
      assert.equal(h.snapshot.validationTone, "success");
      assert.equal(h.snapshot.canExecuteSaved, false);
    }
    h.snapshot.publish(savedPlan());
    h.snapshot.setUncertain(true);
    await h.settle();
    await h.snapshot.validatePlan();
    await h.settle();
    assert.equal(h.snapshot.canExecuteSaved, false);
    assert.equal(h.snapshot.uncertain, true);
    assert.equal(h.count("executeAssistantPlan"), 0);
  });

  test("duplicate validation clicks and run in either direction are mutually exclusive before rerender", async () => {
    const validation = deferred(), execution = deferred();
    const h = workspace({ getAssistantPlanValidation: () => validation.promise, executeAssistantPlan: () => execution.promise });
    await h.settle();
    h.snapshot.publish(savedPlan());
    await h.settle();
    const first = h.snapshot.validatePlan();
    await h.snapshot.validatePlan();
    await h.snapshot.run();
    await h.snapshot.run(h.snapshot.plan);
    await h.settle();
    assert.equal(h.snapshot.executeSavedDisabled, true);
    assert.equal(h.count("getAssistantPlanValidation"), 1);
    assert.equal(h.count("createAssistantPlan"), 0);
    assert.equal(h.count("executeAssistantPlan"), 0);
    validation.resolve(savedValidation());
    await first;
    await h.settle();
    const operation = h.snapshot.run(h.snapshot.plan);
    await h.snapshot.validatePlan();
    await h.settle();
    assert.equal(h.snapshot.validationDisabled, true);
    assert.equal(h.snapshot.validationView, null);
    assert.equal(h.count("getAssistantPlanValidation"), 1);
    assert.equal(h.count("executeAssistantPlan"), 1);
    execution.resolve(finishedPlan());
    await operation;
    await h.settle();
    assert.equal(h.snapshot.busy, false);
  });

  for (const reason of ["http", "protocol", "timeout"]) {
    test(`${reason} failure removes a previous green result and never retries or publishes late success`, async () => {
      const response = deferred();
      let reads = 0;
      const h = workspace({ getAssistantPlanValidation: () => ++reads === 1 ? Promise.resolve(savedValidation()) : response.promise });
      await h.settle();
      h.snapshot.publish(savedPlan());
      await h.settle();
      await h.snapshot.validatePlan();
      await h.settle();
      assert.equal(h.snapshot.validationTone, "success");
      const operation = h.snapshot.validatePlan();
      await h.settle();
      assert.equal(h.snapshot.validationTone, "info");
      assert.equal(h.snapshot.validationView.value, undefined);
      if (reason === "timeout") await h.advance(90000);
      else response.reject(Object.assign(new Error("password=must-not-echo"),
        reason === "http" ? { status: 503 } : { code: "assistant_response_invalid" }));
      await operation;
      await h.settle();
      assert.equal(h.snapshot.validationTone, "info");
      assert.match(h.snapshot.validationMessage, /复核失败.*待核对/);
      assert.equal(h.snapshot.validationView.value, undefined);
      assert.equal(h.snapshot.validationBusyRef.current, false);
      assert.equal(JSON.stringify(h.snapshot.validationView).includes("must-not-echo"), false);
      response.resolve(savedValidation());
      await h.advance(90000);
      assert.equal(h.snapshot.validationView.phase, "error");
      assert.equal(h.snapshot.plan.status, "validated");
      assert.equal(h.snapshot.uncertain, false, "Read failure is not submitted execution uncertainty");
      assert.equal(reads, 2);
      assert.equal(h.count("executeAssistantPlan"), 0);
    });
  }

  const changes = {
    "same-plan publish": (h) => h.snapshot.publish(savedPlan()),
    "different-plan publish": (h) => h.snapshot.publish(savedPlan({ plan_id: `pl${"b".repeat(26)}` })),
    "new request": (h) => h.update({ request: requestB }),
    "close/reopen": (h) => { h.update({ open: false }); h.update({ open: true }); },
    "close callback": (h) => h.snapshot.closeWorkspace(),
    "instance context": (h) => h.update({ instanceId: 2 }),
    "same-token new session": (h) => h.auth.saveAuthToken("synthetic-lifecycle-session"),
    "different session": (h) => h.auth.saveAuthToken("other-synthetic-session"),
    "logout": (h) => h.auth.clearAuthToken(),
    "unmount": (h) => h.unmount(),
  };
  for (const [name, change] of Object.entries(changes)) {
    for (const failure of [false, true]) {
      test(`${name} rejects a late validation ${failure ? "error" : "success"}`, async () => {
        const response = deferred();
        const h = workspace({ getAssistantPlanValidation: () => response.promise });
        await h.settle();
        h.snapshot.publish(savedPlan());
        await h.settle();
        const operation = h.snapshot.validatePlan();
        await h.settle();
        change(h);
        await h.settle();
        if (failure) response.reject(new Error("old failure"));
        else response.resolve(savedValidation());
        await operation;
        await h.settle();
        assert.notEqual(h.snapshot.validationView?.phase, "ready");
        assert.notEqual(h.snapshot.validationView?.phase, "error");
        assert.notEqual(h.snapshot.validationTone, "success");
        assert.equal(h.count("executeAssistantPlan"), 0);
      });
    }
  }

  test("switching detail clears green immediately, even if the new detail fails", async () => {
    const detail = deferred();
    const h = workspace({ getAssistantPlan: () => detail.promise });
    await h.settle();
    h.snapshot.publish(savedPlan());
    await h.settle();
    await h.snapshot.validatePlan();
    await h.settle();
    const operation = h.snapshot.readPlan(`pl${"b".repeat(26)}`);
    await h.settle();
    assert.equal(h.snapshot.validationView, null);
    detail.reject(new Error("detail unavailable"));
    await operation;
    await h.settle();
    assert.equal(h.snapshot.validationView, null);
    assert.equal(h.snapshot.plan.plan_id, planId);
  });

  for (const failure of [false, true]) {
    test(`old validation ${failure ? "failure" : "success"} cannot clear the newer validation lock or overwrite it`, async () => {
      const first = deferred(), second = deferred();
      let reads = 0;
      const h = workspace({ getAssistantPlanValidation: () => ++reads === 1 ? first.promise : second.promise });
      await h.settle();
      h.snapshot.publish(savedPlan());
      await h.settle();
      const old = h.snapshot.validatePlan();
      h.update({ open: false });
      h.update({ open: true });
      await h.settle();
      const next = h.snapshot.validatePlan();
      await h.settle();
      if (failure) first.reject(new Error("old error"));
      else first.resolve(savedValidation());
      await old;
      await h.settle();
      assert.equal(h.snapshot.validationView.phase, "loading");
      assert.equal(h.snapshot.validationBusyRef.current, true);
      await h.snapshot.run();
      await h.snapshot.validatePlan();
      assert.equal(reads, 2);
      assert.equal(h.count("createAssistantPlan"), 0);
      second.resolve(savedValidation({ can_execute: false, issues: [{ code: "capability_disabled" }] }));
      await next;
      await h.settle();
      assert.equal(h.snapshot.validationTone, "warning");
      assert.equal(h.snapshot.validationView.value.issues[0].code, "capability_disabled");
    });
  }

  test("a cached green result is hidden and invalidated on session generation change", async () => {
    const h = workspace();
    await h.settle();
    h.snapshot.publish(savedPlan());
    await h.settle();
    await h.snapshot.validatePlan();
    await h.settle();
    h.auth.saveAuthToken("synthetic-lifecycle-session");
    h.update({});
    await h.settle();
    assert.equal(h.snapshot.validationView, null);
    assert.notEqual(h.snapshot.validationTone, "success");
    assert.equal(h.count("getAssistantPlanValidation"), 1);
  });
});
