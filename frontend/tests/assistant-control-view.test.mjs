import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "@babel/parser";

const source = readFileSync(new URL("../src/AssistantBatchControlView.jsx", import.meta.url), "utf8");
const node = parse(source, { sourceType: "module", plugins: ["jsx"] }).program.body
  .find((entry) => entry.declaration?.id?.name === "AssistantBatchControlView").declaration;
const end = node.body.body.find((entry) => entry.type === "ReturnStatement").start;
const body = source.slice(node.body.start + 1, end)
  + "return {batchId,batch,executionId,loading,error,read,changeBatch,setExecutionId,history,historyLoading,historyError,loadHistory,changeHistoryPage,selectExecution,panelActive};";
const id = (n = 1) => `CB${n.toString(16).padStart(28, "0")}`;
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const historyPage = (page = 1, page_size = 10, items = [], total = 0) => ({ schema: "capability.batch_list/v2", page, page_size, items, total });

function harness(fetcher = async (batchId) => ({ batch_id: batchId, items: [{ execution_id: 9 }] }), options = {}) {
  let cursor = 0, dirty = true, value, generation = 0, active = true, loginEpoch = "a".repeat(32);
  const slots = [], effects = [], calls = [], historyCalls = [], detailOptions = [], timers = new Map();
  let props = { initialBatchId: id(), isActive: () => active, ...options.props };
  let nextTimer = 0;
  const dependencies = {
    getAssistantControlBatch: async (batchId, requestOptions) => { calls.push(batchId); detailOptions.push(requestOptions); return fetcher(batchId); },
    listAssistantControlBatches: async (query, requestOptions) => { historyCalls.push({ query, options: requestOptions }); return options.list ? options.list(query) : historyPage(query.page, query.page_size); },
    isAssistantBatchId: (value) => typeof value === "string" && /^CB[a-f0-9]{28}$/.test(value),
    getAuthSession: () => ({ token: "test-token", generation, loginEpoch }),
    isAuthSessionCurrent: (saved) => saved.token === "test-token" && saved.generation === generation && saved.loginEpoch === loginEpoch,
    setTimeout: (callback) => { const key = ++nextTimer; timers.set(key, callback); return key; },
    clearTimeout: (key) => timers.delete(key),
    useState: (initial) => { const index = cursor++; slots[index] ??= { value: initial }; return [slots[index].value, (update) => {
      const next = typeof update === "function" ? update(slots[index].value) : update;
      if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true; }
    }]; },
    useRef: (initial) => { const index = cursor++; slots[index] ??= { ref: { current: initial } }; return slots[index].ref; },
    useEffect: (callback, deps) => { const index = cursor++, old = slots[index];
      if (!old || deps.some((dep, i) => !Object.is(dep, old.deps[i]))) effects.push(() => {
        old?.cleanup?.(); slots[index] = { deps, cleanup: callback() };
      });
    },
  };
  const render = new Function(...Object.keys(dependencies), "{initialBatchId='',isActive=()=>true}", body);
  const settle = async () => { for (let n = 0; n < 20; n += 1) {
    if (dirty) { dirty = false; cursor = 0; value = render(...Object.values(dependencies), props);
      while (effects.length) effects.shift()(); }
    await Promise.resolve();
  } assert.equal(dirty, false); };
  return { get value() { return value; }, calls, historyCalls, detailOptions, settle,
    expire: () => { for (const [key, callback] of timers) { timers.delete(key); callback(); } },
    closeWithoutRender: () => { active = false; },
    login: async () => { generation += 1; loginEpoch = generation.toString(16).padStart(32, "0"); dirty = true; await settle(); },
    epochOnly: async () => { loginEpoch = "b".repeat(32); dirty = true; await settle(); },
    update: async (patch) => { props = { ...props, ...patch }; dirty = true; await settle(); },
    unmount: () => { for (const slot of slots) slot?.cleanup?.(); },
  };
}

test("control tab reads no batch by default; explicit read selects the sole persisted execution", async () => {
  const h = harness(); await h.settle(); assert.equal(h.calls.length, 0);
  await h.value.read(); await h.settle(); assert.deepEqual(h.calls, [id()]);
  assert.equal(h.value.executionId, 9); assert.equal(h.value.batch.batch_id, id()); h.unmount();
});

test("invalid ID never reaches the control API", async () => {
  const h = harness(); await h.settle(); h.value.changeBatch("1/../pause"); await h.settle();
  await h.value.read(); assert.equal(h.calls.length, 0); h.unmount();
});

for (const race of ["target", "session", "unmount"]) {
  test(`late control batch after ${race} cannot choose or expose an execution`, async () => {
    const late = deferred(), h = harness(() => late.promise); await h.settle(); const reading = h.value.read();
    await h.settle();
    if (race === "target") { h.value.changeBatch(id(2)); await h.settle(); }
    if (race === "session") await h.login();
    if (race === "unmount") h.unmount();
    late.resolve({ batch_id: id(), items: [{ execution_id: 9 }] }); await reading; await h.settle();
    assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null); h.unmount();
  });
}

test("failure removes stale members and shows a safe error, without old-schema fallback or replay", async () => {
  let fail = false;
  const h = harness(async (batchId) => { if (fail) throw new Error("private database failure"); return { batch_id: batchId, items: [{ execution_id: 9 }] }; });
  await h.settle(); await h.value.read(); await h.settle(); fail = true;
  await h.value.read(); await h.settle(); assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null);
  assert.match(h.value.error, /读取失败/); assert.doesNotMatch(h.value.error, /private/);
  assert.equal(h.calls.length, 2); h.unmount();
});

test("control view is explicit and unmounts when its parent dialog closes", () => {
  const parent = readFileSync(new URL("../src/AssistantBatchWorkspace.jsx", import.meta.url), "utf8");
  assert.match(parent, /return open \? <Modal/);
  assert.match(parent, /isActive=\{\(\) => visible\.current\}/);
  assert.match(source, /mounted\.current = false; epoch\.current \+= 1/);
});

test("closing parent activity before React commits discards late reads", async () => {
  const late = deferred(), h = harness(() => late.promise); await h.settle();
  const reading = h.value.read(); await h.settle(); h.closeWithoutRender();
  late.resolve({ batch_id: id(), items: [{ execution_id: 9 }] }); await reading; await h.settle();
  assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null); h.unmount();
});

test("a timed-out control read stays failed after late success and never retries automatically", async () => {
  const late = deferred(), h = harness(() => late.promise); await h.settle();
  const reading = h.value.read(); await h.settle(); h.expire(); await reading; await h.settle();
  assert.match(h.value.error, /读取失败/); assert.equal(h.value.loading, false);
  late.resolve({ batch_id: id(), items: [{ execution_id: 9 }] }); await h.settle();
  assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null); assert.equal(h.calls.length, 1); h.unmount();
});

test("history is never read on mount, detail read, or member selection; its explicit button sends captured auth", async () => {
  const h = harness(); await h.settle(); assert.equal(h.historyCalls.length, 0);
  await h.value.read(); await h.settle(); h.value.selectExecution(9); await h.settle(); assert.equal(h.historyCalls.length, 0);
  await h.value.loadHistory(); await h.settle();
  assert.deepEqual(h.historyCalls[0].query, { page: 1, page_size: 10 });
  assert.deepEqual(h.historyCalls[0].options.authSession, { token: "test-token", generation: 0, loginEpoch: "a".repeat(32) });
  assert.deepEqual(h.detailOptions[0].authSession, h.historyCalls[0].options.authSession);
  assert.equal(h.calls.length, 1); assert.equal(h.value.executionId, 9); h.unmount();
});

test("history summary does not invent an execution; selecting a batch explicitly reads detail then chooses a member", async () => {
  const h = harness(async (batchId) => ({ batch_id: batchId, items: [{ execution_id: 21 }, { execution_id: 22 }] }),
    { list: async ({ page, page_size }) => historyPage(page, page_size, [{ batch_id: id(2), status: "paused" }], 1) });
  await h.settle(); await h.value.loadHistory(); await h.settle();
  assert.equal(h.calls.length, 0); assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null);
  await h.value.read(h.value.history.items[0].batch_id); await h.settle();
  assert.deepEqual(h.calls, [id(2)]); assert.equal(h.value.batchId, id(2)); assert.equal(h.value.batch.batch_id, id(2));
  assert.equal(h.value.executionId, null); h.value.selectExecution(999); await h.settle(); assert.equal(h.value.executionId, null);
  h.value.selectExecution(22); await h.settle(); assert.equal(h.value.executionId, 22); assert.equal(h.value.panelActive(), true); h.unmount();
});

test("pagination reads only the clicked page and resets to page one on explicit page-size changes", async () => {
  const h = harness(undefined, { list: async ({ page, page_size }) => historyPage(page, page_size, [], 0) }); await h.settle();
  await h.value.loadHistory(2, 10); await h.settle(); await h.value.changeHistoryPage(3, 10); await h.settle();
  await h.value.changeHistoryPage(3, 50); await h.settle();
  assert.deepEqual(h.historyCalls.map((call) => call.query), [{ page: 2, page_size: 10 }, { page: 3, page_size: 10 }, { page: 1, page_size: 50 }]);
  assert.equal(h.value.history.page, 1); assert.equal(h.value.history.page_size, 50); assert.equal(h.calls.length, 0);
  assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null); h.unmount();
});

test("invalid page/size and synchronous double clicks do not issue additional reads", async () => {
  const late = deferred(), h = harness(() => late.promise, { list: () => late.promise }); await h.settle();
  for (const [page, size] of [[0, 10], [1000001, 10], [1.5, 10], [1, 0], [1, 101], [1, "10"]]) await h.value.loadHistory(page, size);
  assert.equal(h.historyCalls.length, 0);
  const originalRead = h.value.read, reading = originalRead(); await originalRead(); assert.equal(h.calls.length, 1);
  const load = h.value.loadHistory, listing = load(); await load(); assert.equal(h.historyCalls.length, 1);
  late.resolve({ ...historyPage(), batch_id: id(), items: [{ execution_id: 9 }] }); await reading; await listing; await h.settle(); h.unmount();
});

for (const result of ["success", "failure"]) {
  test(`an old history page ${result} cannot overwrite a newer page or release its loading guard`, async () => {
    const late = deferred(), h = harness(undefined, { list: ({ page, page_size }) => page === 1 ? late.promise : Promise.resolve(historyPage(page, page_size, [{ batch_id: id(2) }], 11)) });
    await h.settle(); const old = h.value.loadHistory(1, 10); await h.settle();
    await h.value.loadHistory(2, 10); await h.settle(); assert.equal(h.value.history.page, 2);
    if (result === "failure") late.reject(new Error("private old page")); else late.resolve(historyPage(1, 10, [{ batch_id: id() }], 11));
    await old; await h.settle(); assert.equal(h.value.history.page, 2); assert.equal(h.value.historyError, ""); assert.equal(h.value.historyLoading, false); h.unmount();
  });
}

for (const race of ["close", "session", "epoch", "unmount", "new-activity"]) {
  for (const result of ["success", "failure"]) {
    test(`late history ${result} after ${race} cannot repopulate history or select a batch/execution`, async () => {
      const late = deferred(), h = harness(undefined, { list: () => late.promise }); await h.settle();
      const pending = h.value.loadHistory(); await h.settle();
      if (race === "close") h.closeWithoutRender();
      if (race === "session") await h.login();
      if (race === "epoch") await h.epochOnly();
      if (race === "unmount") h.unmount();
      if (race === "new-activity") await h.update({ isActive: () => false });
      if (result === "failure") late.reject(new Error("private old history")); else late.resolve(historyPage(1, 10, [{ batch_id: id() }], 1));
      await pending; await h.settle(); assert.equal(h.value.history, null); assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null);
      assert.equal(h.value.historyError.includes("private"), false); assert.equal(h.historyCalls.length, 1); assert.equal(h.calls.length, 0); h.unmount();
    });
  }
}

for (const status of [401, 403, 409, 503]) {
  test(`history ${status} clears stale summaries, shows failure rather than an empty success, and never retries`, async () => {
    let fail = false;
    const h = harness(undefined, { list: ({ page, page_size }) => {
      if (fail) throw Object.assign(new Error("private denied SQL"), { status });
      return historyPage(page, page_size, [{ batch_id: id() }], 1);
    } });
    await h.settle(); await h.value.loadHistory(); await h.settle(); assert.equal(h.value.history.items.length, 1); fail = true;
    await h.value.loadHistory(); await h.settle(); assert.equal(h.value.history, null); assert.equal(h.value.historyLoading, false);
    assert.match(h.value.historyError, status === 401 || status === 403 ? /权限被服务端拒绝/ : /读取失败/);
    assert.doesNotMatch(h.value.historyError, /private|SQL/); assert.equal(h.historyCalls.length, 2); assert.equal(h.calls.length, 0); h.unmount();
  });
}

test("history timeout ignores its late success and does not auto-read details or retry", async () => {
  const late = deferred(), h = harness(undefined, { list: () => late.promise }); await h.settle();
  const pending = h.value.loadHistory(); await h.settle(); h.expire(); await pending; await h.settle();
  assert.equal(h.value.history, null); assert.match(h.value.historyError, /读取失败/); assert.equal(h.value.historyLoading, false);
  late.resolve(historyPage(1, 10, [{ batch_id: id() }], 1)); await h.settle();
  assert.equal(h.value.history, null); assert.equal(h.historyCalls.length, 1); assert.equal(h.calls.length, 0); h.unmount();
});

test("selecting another batch/member synchronously invalidates the previous panel before commit", async () => {
  const h = harness(async (batchId) => ({ batch_id: batchId, items: [{ execution_id: 1 }, { execution_id: 2 }] }));
  await h.settle(); await h.value.read(); await h.settle(); h.value.selectExecution(1); await h.settle();
  const firstPanelActive = h.value.panelActive; assert.equal(firstPanelActive(), true);
  h.value.selectExecution(2); assert.equal(firstPanelActive(), false); await h.settle();
  const secondPanelActive = h.value.panelActive; assert.equal(secondPanelActive(), true);
  const pending = h.value.read(id(2)); assert.equal(secondPanelActive(), false); await pending; await h.settle();
  h.value.selectExecution(2); await h.settle(); assert.equal(h.value.panelActive(), true); assert.equal(secondPanelActive(), false); h.unmount();
});

test("editing the target during a history request cannot let its later summaries restore old detail selection", async () => {
  const late = deferred(), h = harness(undefined, { list: () => late.promise }); await h.settle(); await h.value.read(); await h.settle();
  const pending = h.value.loadHistory(); await h.settle(); h.value.changeBatch(id(2)); await h.settle();
  late.resolve(historyPage(1, 10, [{ batch_id: id() }], 1)); await pending; await h.settle();
  assert.equal(h.value.batchId, id(2)); assert.equal(h.value.batch, null); assert.equal(h.value.executionId, null); assert.equal(h.value.history.items[0].batch_id, id()); h.unmount();
});

test("stale cached callbacks cannot read history/detail or change selection when activity closes without rerender", async () => {
  const h = harness(); await h.settle(); await h.value.read(); await h.settle(); const old = h.value;
  h.closeWithoutRender(); await old.loadHistory(); await old.read(id(2)); old.changeBatch(id(2)); old.selectExecution(9);
  assert.equal(old.panelActive(), false); assert.equal(h.historyCalls.length, 0); assert.equal(h.calls.length, 1); h.unmount();
});

test("same-token epoch change immediately hides history and old callbacks cannot borrow the new session", async () => {
  const h = harness(undefined, { list: ({ page, page_size }) => historyPage(page, page_size, [{ batch_id: id() }], 1) });
  await h.settle(); await h.value.loadHistory(); await h.settle(); const stale = h.value;
  await h.epochOnly(); assert.equal(h.value.history, null); assert.equal(h.value.batchId, "");
  await stale.loadHistory(); await stale.read(id()); assert.equal(h.historyCalls.length, 1); assert.equal(h.calls.length, 0); h.unmount();
});
