import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@babel/parser";

const require = createRequire(import.meta.url);
const antdRequire = createRequire(require.resolve("antd/package.json"));
const { FormStore } = antdRequire("rc-field-form/lib/useForm.js");
const source = readFileSync(new URL("../src/UnifiedReportCenterPage.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const body = ast.program.body.find((node) => node.declaration?.id?.name === "UnifiedReportCenterPage").declaration.body.body;
const declarations = [...ast.program.body, ...body].flatMap((node) => node.declarations || []);
function evaluate(node, scope) {
  // Same realm as FormStore: rc-util's merge intentionally checks object prototypes.
  return vm.compileFunction(`with (scope) { return (${source.slice(node.start, node.end)}); }`, ["scope"])(scope);
}
const method = (name, scope) => evaluate(declarations.find((node) => node.id.name === name).init, scope);
function find(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (predicate(node)) return node;
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      const result = find(child, predicate);
      if (result) return result;
    }
  }
  return null;
}
const mountEffect = body.find((node) => node.expression?.callee?.name === "useEffect"
  && find(node.expression.arguments[0], (item) => item.type === "CallExpression" && item.callee.name === "loadCatalog"));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

function loadingHarness(api = {}) {
  const state = {}, writes = [], errors = [];
  const scope = {
    mounted: { current: true }, catalogRequest: { current: 0 }, schedulesRequest: { current: 0 },
    formPageGeneration: { current: 0 },
    runsRequest: { current: 0 }, detailRequest: { current: 0 },
    filters: { keyword: "A", reportType: "all" }, schedules: [],
    message: { error: (error) => errors.push(error), success() {} },
    getReportCatalog: async () => ({ items: [] }),
    getReportCatalogStats: async () => ({ total: 0, signed: 0, by_archive_state: {} }),
    getReportSchedules: async () => ({ items: [] }), getReportSubscriptions: async () => ({ items: [] }),
    getReportScheduleRuns: async () => ({ items: [] }), syncReportCatalog: async () => ({ synced: 0 }),
    ...api,
  };
  for (const name of ["Loading", "RunsLoading", "SchedulesLoading", "Catalog", "Stats", "CatalogError", "Schedules", "Subscriptions", "SchedulesLoaded", "Runs"]) {
    scope[`set${name}`] = (value) => { state[name] = value; writes.push([name, value]); };
  }
  for (const name of ["loadCatalog", "loadSchedules", "loadRuns"]) scope[name] = method(name, scope);
  // Exercise the real mount cleanup, including StrictMode effect replay, without starting unrelated reads.
  const mount = () => evaluate(mountEffect.expression.arguments[0], { ...scope, loadCatalog() {}, loadSchedules() {} })();
  return { scope, state, writes, errors, mount };
}

test("latest catalog query wins over a late old success", async () => {
  const a = deferred(), b = deferred();
  const h = loadingHarness({ getReportCatalog: (query) => query.keyword === "A" ? a.promise : b.promise });
  const first = h.scope.loadCatalog(); h.scope.filters = { keyword: "B" };
  const second = h.scope.loadCatalog();
  b.resolve({ items: [{ id: 2 }] }); await second;
  a.resolve({ items: [{ id: 1 }] }); await first;
  assert.equal(h.state.Catalog[0].id, 2);
});

test("old failure and finally cannot hide a pending latest query", async () => {
  const a = deferred(), b = deferred();
  const h = loadingHarness({ getReportCatalog: (query) => query.keyword === "A" ? a.promise : b.promise });
  const first = h.scope.loadCatalog(); h.scope.filters = { keyword: "B" };
  const second = h.scope.loadCatalog();
  a.reject(new Error("old failure")); await first;
  const during = { loading: h.state.Loading, error: h.state.CatalogError, toasts: [...h.errors] };
  b.resolve({ items: [] }); await second;
  assert.equal(during.loading, true);
  assert.equal(during.error, "");
  assert.deepEqual(during.toasts, []);
  assert.deepEqual(h.state.Catalog, []);
});

test("superseded catalog does not start a statistics request", async () => {
  const a = deferred(), b = deferred(); let statsReads = 0;
  const h = loadingHarness({ getReportCatalog: (query) => query.keyword === "A" ? a.promise : b.promise,
    getReportCatalogStats: async () => { statsReads += 1; return { total: 1 }; } });
  const first = h.scope.loadCatalog(); h.scope.filters = { keyword: "B" };
  const second = h.scope.loadCatalog();
  a.resolve({ items: [] }); await first;
  const before = statsReads;
  b.resolve({ items: [] }); await second;
  assert.equal(before, 0);
  assert.equal(statsReads, 1);
});

for (const reject of [false, true]) {
  test(`late statistics ${reject ? "failure" : "success"} cannot overwrite a newer catalog`, async () => {
    const oldStats = deferred(); let count = 0;
    const h = loadingHarness({ getReportCatalog: async (q) => ({ items: [{ id: q.keyword }] }),
      getReportCatalogStats: () => ++count === 1 ? oldStats.promise : Promise.resolve({ total: 2 }) });
    const first = h.scope.loadCatalog(); await tick();
    h.scope.filters = { keyword: "B" }; await h.scope.loadCatalog();
    if (reject) oldStats.reject(new Error("old stats")); else oldStats.resolve({ total: 1 });
    await first;
    assert.equal(h.state.Catalog[0].id, "B");
    assert.equal(h.state.Stats.total, 2);
    assert.equal(h.state.CatalogError, "");
    assert.deepEqual(h.errors, []);
  });
}

test("unmount during statistics suppresses all late writes and notifications", async () => {
  const stats = deferred();
  const h = loadingHarness({ getReportCatalogStats: () => stats.promise });
  const cleanup = h.mount();
  const request = h.scope.loadCatalog(); await tick(); cleanup();
  const before = h.writes.length;
  stats.reject(new Error("late stats")); await request;
  assert.equal(h.writes.length, before);
  assert.deepEqual(h.errors, []);
});

test("mount cleanup and re-entry invalidate the old generation even when mounted becomes true", async () => {
  const old = deferred(); let calls = 0;
  const h = loadingHarness({ getReportCatalog: () => ++calls === 1 ? old.promise : Promise.resolve({ items: [{ id: 2 }] }) });
  const cleanup = h.mount(); const first = h.scope.loadCatalog(); cleanup();
  const cleanupAgain = h.mount(); await h.scope.loadCatalog();
  old.resolve({ items: [{ id: 1 }] }); await first;
  assert.equal(h.state.Catalog[0].id, 2);
  cleanupAgain();
});

test("runs loading stays independent of catalog loading", async () => {
  const rows = deferred(), runs = deferred();
  const h = loadingHarness({ getReportCatalog: () => rows.promise, getReportScheduleRuns: () => runs.promise });
  const catalog = h.scope.loadCatalog();
  const history = h.scope.loadRuns([{ id: 1, name: "synthetic" }]);
  rows.resolve({ items: [] }); await catalog;
  const during = { catalog: h.state.Loading, runs: h.state.RunsLoading };
  runs.resolve({ items: [] }); await history;
  assert.equal(during.catalog, false);
  assert.equal(during.runs, true);
  assert.equal(h.state.RunsLoading, false);
});

test("runs fallback schedule read cannot overwrite the independently loaded schedule list", async () => {
  const schedules = deferred();
  const h = loadingHarness({ getReportSchedules: () => schedules.promise });
  const history = h.scope.loadRuns();
  h.scope.setSchedules([{ id: 2 }]);
  schedules.resolve({ items: [{ id: 1 }] }); await history;
  assert.equal(h.state.Schedules[0].id, 2);
});

test("schedule and run reads discard old responses and unmounted failures", async () => {
  for (const name of ["loadSchedules", "loadRuns"]) {
    const old = deferred(); let calls = 0;
    const api = name === "loadSchedules" ? "getReportSchedules" : "getReportScheduleRuns";
    const h = loadingHarness({ [api]: () => ++calls === 1 ? old.promise : Promise.resolve({ items: [{ id: 2 }] }) });
    const cleanup = h.mount();
    const args = name === "loadRuns" ? [[{ id: 1 }]] : [];
    const first = h.scope[name](...args); await h.scope[name](...args);
    const before = h.writes.length; cleanup();
    old.reject(new Error("late failure")); await first;
    assert.equal(h.writes.length, before);
    assert.deepEqual(h.errors, []);
  }
});

test("schedule and run reloads keep the latest successful response after tab re-entry", async () => {
  for (const name of ["loadSchedules", "loadRuns"]) {
    const old = deferred(); let calls = 0;
    const api = name === "loadSchedules" ? "getReportSchedules" : "getReportScheduleRuns";
    const h = loadingHarness({ [api]: () => ++calls === 1 ? old.promise : Promise.resolve({ items: [{ id: 2 }] }) });
    const args = name === "loadRuns" ? [[{ id: 1 }]] : [];
    const cleanup = h.mount(); const first = h.scope[name](...args); cleanup();
    const cleanupAgain = h.mount(); await h.scope[name](...args);
    old.resolve({ items: [{ id: 1 }] }); await first;
    assert.equal(h.state[name === "loadSchedules" ? "Schedules" : "Runs"][0].id, 2);
    cleanupAgain();
  }
});

test("latest statistics failure leaves no previous catalog rows or fabricated zero total", async () => {
  const h = loadingHarness();
  h.scope.setCatalog([{ id: 1 }]); h.scope.setStats({ total: 1 });
  h.scope.getReportCatalogStats = async () => { throw new Error("statistics unavailable"); };
  await h.scope.loadCatalog();
  assert.deepEqual(h.state.Catalog, []);
  assert.equal(h.state.Stats, null);
  assert.equal(h.state.CatalogError, "statistics unavailable");
  assert.equal(h.state.Loading, false);
  assert.deepEqual(h.errors, ["statistics unavailable"]);
});

test("unmount while loading the run schedule list prevents late child requests", async () => {
  const schedules = deferred(); let childRequests = 0;
  const h = loadingHarness({ getReportSchedules: () => schedules.promise,
    getReportScheduleRuns: async () => { childRequests += 1; return { items: [] }; } });
  const cleanup = h.mount(); const request = h.scope.loadRuns(); cleanup();
  const before = h.writes.length;
  schedules.resolve({ items: [{ id: 1 }] }); await request;
  assert.equal(childRequests, 0);
  assert.equal(h.writes.length, before);
});

test("source sync invalidated by a newer query cannot trigger another catalog read or toast", async () => {
  const sync = deferred(); let reads = 0; const messages = [];
  const h = loadingHarness({ syncReportCatalog: () => sync.promise,
    getReportCatalog: async () => { reads += 1; return { items: [{ id: 2 }] }; } });
  h.scope.message.success = (value) => messages.push(value);
  const first = h.scope.loadCatalog(false, true);
  h.scope.filters = { keyword: "B" }; await h.scope.loadCatalog();
  sync.resolve({ synced: 20 }); await first;
  assert.equal(reads, 1);
  assert.equal(h.state.Catalog[0].id, 2);
  assert.deepEqual(messages, []);
});

test("unmount during source sync suppresses its error and finally", async () => {
  const sync = deferred();
  const h = loadingHarness({ syncReportCatalog: () => sync.promise });
  const cleanup = h.mount(); const request = h.scope.loadCatalog(false, true); cleanup();
  const before = h.writes.length;
  sync.reject(new Error("old sync")); await request;
  assert.equal(h.writes.length, before);
  assert.deepEqual(h.errors, []);
});

function realForm(names) {
  const form = new FormStore(() => {}).getForm();
  const hooks = form.getInternalHooks("RC_FORM_INTERNAL_HOOKS");
  for (const name of names) hooks.registerField({
    props: {}, getNamePath: () => [name], onStoreChange() {}, isListField: () => false, isList: () => false,
    getMeta: () => ({}), getErrors: () => [], getWarnings: () => [],
  });
  return form;
}
function formHarness(kind, names) {
  const form = realForm(names), posts = [];
  const scope = {
    [`${kind}Form`]: form, editingSchedule: null, editingSubscription: null,
    setEditingSchedule: (row) => { scope.editingSchedule = row; },
    setEditingSubscription: (row) => { scope.editingSubscription = row; },
    setScheduleOpen() {}, setSubscriptionOpen() {}, setSnapshotOpen() {},
    message: { success() {}, error: (error) => { throw new Error(error); } },
    loadSchedules: async () => {}, loadCatalog: async () => {},
    createReportSchedule: async (payload) => posts.push(payload),
    createReportSubscription: async (payload) => posts.push(payload),
    createReportSnapshot: async (payload) => { posts.push(payload); return { artifact_uid: "synthetic" }; },
  };
  scope.csvValues = method("csvValues", scope); scope.csvNumbers = method("csvNumbers", scope);
  bindFormSessions(scope);
  return { form, posts, scope };
}

function bindFormSessions(scope) {
  scope.mounted ??= { current: true };
  scope.formPageGeneration ??= { current: 0 };
  scope.formSessions ??= { current: Object.fromEntries(["snapshot", "schedule", "subscription"].map((kind) =>
    [kind, { generation: 0, open: false, pending: null }])) };
  scope.setSubmitting ??= () => {};
  scope.latestLoads ??= { current: { loadCatalog: scope.loadCatalog, loadSchedules: scope.loadSchedules } };
  for (const name of ["changeFormSession", "startFormSubmission", "isFormSubmissionCurrent", "finishFormSubmission",
    "closeSnapshot", "closeSchedule", "closeSubscription", "openSnapshot"]) {
    if (declarations.some((node) => node.id.name === name)) scope[name] = method(name, scope);
  }
}

test("edit schedule then new clears old scope, watermark and conditional fields in submitted payload", async () => {
  const h = formHarness("schedule", ["name", "instance_ids", "watermark", "filter_limit", "enabled", "cron_expression"]);
  const open = method("openSchedule", h.scope);
  open({ id: 7, name: "old", filter: { instance_ids: [101] }, watermark: "old", cron_expression: "0 1 * * *" });
  h.scope.setScheduleOpen(false); open();
  await method("submitSchedule", h.scope)();
  assert.deepEqual(h.posts[0].filter_json.instance_ids, []);
  assert.equal(h.posts[0].watermark, undefined);
  assert.equal(h.posts[0].cron_expression, undefined);
});

test("edit subscription then new clears previous recipient, channel and schedule", async () => {
  const h = formHarness("subscription", ["name", "schedule_id", "recipients", "channels", "report_types", "enabled"]);
  const open = method("openSubscription", h.scope);
  open({ id: 7, schedule_id: 1, recipients: ["old@example.invalid"], channels: ["old-channel"] });
  h.scope.setSubscriptionOpen(false); open();
  await method("submitSubscription", h.scope)();
  assert.equal(h.posts[0].schedule_id, undefined);
  assert.deepEqual(h.posts[0].recipients, []);
  assert.deepEqual(h.posts[0].channels, []);
});

test("editing another record resets optional fields omitted by the backend", () => {
  const h = formHarness("schedule", ["name", "watermark", "instance_ids"]);
  const open = method("openSchedule", h.scope);
  open({ id: 1, watermark: "old", filter: { instance_ids: [101] } });
  open({ id: 2, name: "new", filter: { instance_ids: [102] } });
  assert.equal(h.form.getFieldValue("watermark"), undefined);
  assert.equal(h.form.getFieldValue("instance_ids"), "102");
  assert.equal(h.scope.editingSchedule.id, 2);
});

test("snapshot reopen resets abandoned scope and watermark before submission", async () => {
  const h = formHarness("snapshot", ["title", "instance_ids", "watermark", "report_type", "limit"]);
  h.form.setFieldsValue({ instance_ids: "101", watermark: "old" });
  const button = find(ast, (node) => node.type === "JSXElement" && node.openingElement.name.name === "Button"
    && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "生成快照"));
  const handler = button.openingElement.attributes.find((node) => node.name?.name === "onClick").value.expression;
  evaluate(handler, h.scope)();
  await method("submitSnapshot", h.scope)();
  assert.deepEqual(h.posts[0].instance_ids, []);
  assert.equal(h.posts[0].watermark, undefined);
});

function submitHarness(kind) {
  const form = realForm(["name", "title", "instance_ids", "filter_limit", "enabled", "schedule_id", "recipients", "channels"]);
  const validate = form.validateFields;
  const state = { open: false, editing: null, keyword: "A", submitting: {} };
  const posts = [], queries = [], notices = [], writes = [];
  const shared = {
    mounted: { current: true }, formPageGeneration: { current: 0 },
    catalogRequest: { current: 0 }, schedulesRequest: { current: 0 }, runsRequest: { current: 0 }, detailRequest: { current: 0 },
    latestLoads: { current: null },
  };
  const h = { form, state, posts, queries, notices, writes, validate: () => validate(), post: async () => ({ artifact_uid: "synthetic" }) };
  form.validateFields = () => h.validate();
  const submitName = `submit${kind[0].toUpperCase()}${kind.slice(1)}`;
  const post = (id, payload) => { posts.push({ id, payload }); return h.post(); };
  h.render = () => {
    // Every render gets its own filters/editing values and callbacks, as in React.
    const scope = {
      ...shared, [`${kind}Form`]: form, filters: { keyword: state.keyword }, schedules: [],
      editingSchedule: kind === "schedule" ? state.editing : null,
      editingSubscription: kind === "subscription" ? state.editing : null,
      message: { success: (value) => notices.push(["success", value]), error: (value) => notices.push(["error", value]) },
      getReportCatalog: async (query) => { queries.push(query.keyword); return { items: [{ id: query.keyword }] }; },
      getReportCatalogStats: async () => ({ total: 1 }),
      getReportSchedules: async () => ({ items: [] }), getReportSubscriptions: async () => ({ items: [] }),
      createReportSnapshot: (payload) => post(null, payload),
      createReportSchedule: (payload) => post(null, payload), updateReportSchedule: post,
      createReportSubscription: (payload) => post(null, payload), updateReportSubscription: post,
      setEditingSchedule: (row) => { state.editing = row; }, setEditingSubscription: (row) => { state.editing = row; },
      setSubmitting: (update) => { state.submitting = typeof update === "function" ? update(state.submitting) : update; writes.push("submitting"); },
    };
    for (const name of ["ScheduleOpen", "SubscriptionOpen", "SnapshotOpen"]) {
      scope[`set${name}`] = (open) => { state.open = open; writes.push(name); };
    }
    for (const name of ["Loading", "CatalogError", "Catalog", "Stats", "SchedulesLoading", "Schedules", "Subscriptions", "SchedulesLoaded"]) {
      scope[`set${name}`] = (value) => { state[name] = value; writes.push(name); };
    }
    scope.csvValues = method("csvValues", scope); scope.csvNumbers = method("csvNumbers", scope);
    scope.loadCatalog = method("loadCatalog", scope); scope.loadSchedules = method("loadSchedules", scope);
    bindFormSessions(scope); shared.formSessions = scope.formSessions;
    const layout = body.find((node) => node.expression?.callee?.name === "useLayoutEffect");
    if (layout) evaluate(layout.expression.arguments[0], scope)();
    h.scope = scope;
    h.submit = method(submitName, scope);
  };
  h.open = (name = "A", id = 101) => {
    if (kind === "snapshot") {
      const button = find(ast, (node) => node.type === "JSXElement" && node.openingElement.name.name === "Button"
        && node.children.some((child) => child.type === "JSXText" && child.value.trim() === "生成快照"));
      evaluate(button.openingElement.attributes.find((node) => node.name?.name === "onClick").value.expression, h.scope)();
    } else method(`open${kind[0].toUpperCase()}${kind.slice(1)}`, h.scope)(id == null ? null : { id, name, enabled: true });
    form.setFieldsValue({ name, title: name, enabled: true, instance_ids: "901" });
    h.render();
  };
  h.close = () => {
    const modal = find(ast, (node) => node.type === "JSXOpeningElement" && node.name.name === "Modal"
      && node.attributes.some((attr) => attr.name?.name === "open" && attr.value?.expression?.name === `${kind}Open`));
    evaluate(modal.attributes.find((node) => node.name?.name === "onCancel").value.expression, h.scope)();
    h.render();
  };
  h.unmount = () => {
    const cleanup = evaluate(mountEffect.expression.arguments[0], { ...h.scope, loadCatalog() {}, loadSchedules() {} })();
    cleanup();
  };
  h.remount = () => { shared.mounted.current = true; h.render(); };
  h.render(); h.open();
  return h;
}

test("snapshot POST completion refreshes committed filter B, never its captured filter A", async () => {
  const h = submitHarness("snapshot"), post = deferred();
  h.post = () => post.promise;
  const request = h.submit(); await tick();
  h.close(); h.state.keyword = "B"; h.render(); await h.scope.loadCatalog();
  post.resolve({ artifact_uid: "synthetic-A" }); await request;
  assert.deepEqual(h.queries, ["B", "B"]);
  assert.equal(h.state.Catalog[0].id, "B");
  assert.deepEqual(h.notices, []);
});

for (const key of ["archive", "hold"]) {
  for (const transition of ["current", "filter", "unmount", "remount"]) {
    for (const fail of [false, true]) {
      test(`catalog menu ${key}: late ${fail ? "failure" : "success"} after ${transition}`, async () => {
        const pending = deferred(), queries = [], posts = [], notices = [];
        const h = loadingHarness({
          getReportCatalog: async (query) => { queries.push(query.keyword); return { items: [{ id: query.keyword }] }; },
          message: { success: (value) => notices.push(["success", value]), error: (value) => notices.push(["error", value]) },
        });
        const latestLoads = { current: null };
        const layout = body.find((node) => node.expression?.callee?.name === "useLayoutEffect");
        const columns = declarations.find((node) => node.id.name === "catalogColumns").init;
        const menu = find(columns, (node) => node.type === "JSXAttribute" && node.name.name === "menu");
        const callback = menu.value.expression.properties.find((node) => node.key.name === "onClick").value;
        const render = (keyword) => {
          const scope = { ...h.scope, latestLoads, filters: { keyword }, row: { id: 901, legal_hold: false },
            archiveReportArtifact: (...args) => { posts.push(args); return pending.promise; },
            setReportArtifactLegalHold: (...args) => { posts.push(args); return pending.promise; },
          };
          scope.loadCatalog = method("loadCatalog", scope);
          evaluate(layout.expression.arguments[0], scope)();
          return { load: scope.loadCatalog, click: evaluate(callback, scope) };
        };
        const cleanup = h.mount();
        const a = render("A");
        const request = a.click({ key });
        // Observe rejection immediately, but assert it is handled by the menu callback.
        const settled = request.then(() => null, (error) => error);
        assert.equal(posts.length, 1);
        assert.equal(posts[0][0], 901);
        if (key === "hold") assert.equal(posts[0][1].enabled, true);
        const keyword = transition === "current" ? "A" : "B";
        await render(keyword).load();
        if (transition === "unmount" || transition === "remount") cleanup();
        if (transition === "remount") { h.mount(); render("B"); }
        const writesBefore = h.writes.length;
        if (fail) pending.reject(new Error("synthetic menu failure")); else pending.resolve({});
        assert.equal(await settled, null, "menu failures must not become unhandled rejections");
        const active = transition === "current" || transition === "filter";
        assert.deepEqual(queries, active && !fail ? [keyword, keyword] : [keyword]);
        assert.equal(h.state.Catalog[0].id, keyword);
        assert.deepEqual(notices.map(([type]) => type), active ? [fail ? "error" : "success"] : []);
        if (active && fail) assert.equal(notices[0][1], "synthetic menu failure");
        if (!active || fail) assert.equal(h.writes.length, writesBefore);
      });
    }
  }
}

for (const kind of ["snapshot", "schedule", "subscription"]) {
  test(`${kind}: modal loading is confined to OK button props so cancellation stays reachable`, () => {
    const modal = find(ast, (node) => node.type === "JSXOpeningElement" && node.name.name === "Modal"
      && node.attributes.some((attr) => attr.name?.name === "open" && attr.value?.expression?.name === `${kind}Open`));
    // AntD 5.29.3 ignores every cancel event while Modal.confirmLoading is true.
    assert.ok(modal);
    assert.equal(modal.attributes.some((attr) => attr.name?.name === "confirmLoading"), false);
    assert.equal(modal.attributes.some((attr) => attr.type === "JSXSpreadAttribute"), false);
    const buttonProps = modal.attributes.find((attr) => attr.name?.name === "okButtonProps");
    assert.ok(buttonProps?.value?.expression);
    for (const busy of [false, true]) {
      const submitting = Object.fromEntries(["snapshot", "schedule", "subscription"].map((name) =>
        [name, name === kind ? busy : !busy]));
      assert.deepEqual(evaluate(buttonProps.value.expression, { submitting }), { loading: busy });
    }
    const closeName = `close${kind[0].toUpperCase()}${kind.slice(1)}`;
    assert.equal(modal.attributes.find((attr) => attr.name?.name === "onCancel").value.expression.name, closeName);
  });

  for (const phase of ["validate", "post"]) {
    for (const fail of [false, true]) {
      for (const transition of ["close", "replace", "unmount"]) {
        test(`${kind}: old ${phase} ${fail ? "failure" : "success"} is isolated after ${transition}`, async () => {
          const h = submitHarness(kind), pending = deferred();
          if (phase === "validate") h.validate = () => pending.promise; else h.post = () => pending.promise;
          const request = h.submit(); await tick();
          if (transition === "unmount") h.unmount();
          else { h.close(); if (transition === "replace") h.open("B", 102); }
          const beforeWrites = h.writes.length;
          const fields = h.form.getFieldsValue(true);
          if (fail) pending.reject(new Error("old failure"));
          else pending.resolve(phase === "validate" ? { name: "A", title: "A", instance_ids: "901" } : { artifact_uid: "old" });
          await request;
          assert.equal(h.posts.length, phase === "validate" ? 0 : 1);
          assert.equal(h.state.open, transition === "replace" || transition === "unmount");
          assert.deepEqual(h.form.getFieldsValue(true), fields);
          assert.deepEqual(h.notices, []);
          if (transition === "unmount") assert.equal(h.writes.length, beforeWrites);
        });
      }
    }
  }

  test(`${kind}: double submit is locked before validation and throughout POST`, async () => {
    const h = submitHarness(kind), validation = deferred(), post = deferred(); let validations = 0;
    h.validate = () => { validations += 1; return validation.promise; }; h.post = () => post.promise;
    const first = h.submit(), duplicate = h.submit();
    const before = validations;
    validation.resolve({ name: "A", title: "A" }); await tick();
    const duplicatePost = h.submit(); await tick();
    const posted = h.posts.length;
    post.resolve({ artifact_uid: "synthetic" }); await Promise.all([first, duplicate, duplicatePost]);
    assert.equal(before, 1); assert.equal(validations, 1); assert.equal(posted, 1);
    assert.equal(h.state.open, false); assert.equal(h.state.submitting[kind], false);
    assert.equal(h.notices.filter(([type]) => type === "success").length, 1);
  });

  test(`${kind}: old finally cannot unlock a newer pending submission`, async () => {
    const h = submitHarness(kind), a = deferred(), b = deferred();
    h.post = () => a.promise; const first = h.submit(); await tick();
    h.close(); h.open("B", 102); h.post = () => b.promise;
    const second = h.submit(); await tick();
    a.resolve({ artifact_uid: "A" }); await first;
    const open = h.state.open, busy = h.state.submitting[kind];
    const duplicate = h.submit(); await tick(); const posted = h.posts.length;
    b.resolve({ artifact_uid: "B" }); await Promise.all([second, duplicate]);
    assert.equal(open, true); assert.equal(busy, true); assert.equal(posted, 2);
    assert.equal(h.state.open, false);
  });

  test(`${kind}: POST from an earlier mount cannot refresh or notify after remount`, async () => {
    const h = submitHarness(kind), post = deferred(); h.post = () => post.promise;
    const request = h.submit(); await tick(); h.unmount(); h.remount(); h.open("B", 102);
    const before = h.writes.length;
    post.resolve({ artifact_uid: "A" }); await request;
    assert.equal(h.writes.length, before); assert.deepEqual(h.notices, []);
    assert.equal(h.form.getFieldValue("name"), "B"); assert.equal(h.state.open, true);
  });

  for (const phase of ["validate", "post"]) {
    test(`${kind}: current ${phase} failure preserves input and releases lock for retry`, async () => {
      const h = submitHarness(kind);
      if (phase === "validate") h.validate = async () => { throw { errorFields: [{ name: ["name"], errors: ["required"] }] }; };
      else h.post = async () => { throw new Error("current failure"); };
      await h.submit();
      assert.equal(h.state.open, true); assert.equal(h.state.submitting[kind], false);
      assert.equal(h.form.getFieldValue("name"), "A");
      assert.equal(h.notices.length, phase === "validate" ? 0 : 1);
      h.validate = async () => h.form.getFieldsValue(true); h.post = async () => ({ artifact_uid: "retry" });
      await h.submit();
      assert.equal(h.state.open, false); assert.equal(h.state.submitting[kind], false);
    });
  }
}
