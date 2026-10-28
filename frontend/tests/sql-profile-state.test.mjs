import assert from "node:assert/strict";
import test from "node:test";
import { collectionOutcome, createSqlProfileController, liveCollectionSupported, sqlProfileView } from "../src/SQLProfileCenterState.js";

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const archive = (id = 1, sql = "sql-a") => ({
  items: sql ? [{ instance_id: id, sql_id: sql }] : [], total_stored: sql ? 1 : 0,
  latest_capture_time: "2026-09-08T10:00:00", collection_capability: { live_supported: true }, read_only: true,
});
const create = (api = {}, id = 1) => createSqlProfileController(id, {
  getSqlProfile: async () => archive(id), collectSqlProfile: async () => ({ status: "success", count: 1 }),
  getSqlProfileDetail: async (sql_id) => ({ profile: { sql_id } }), ...api,
});

for (const [status, type] of [["failed", "error"], ["partial", "warning"], ["empty", "warning"], ["unsupported", "warning"], ["success", "success"]]) {
  test(`POST ${status} preserves outcome, errors and retained count`, async () => {
    const controller = create({ collectSqlProfile: async () => ({ status, count: 0, retained_count: 9, message: "collector result", errors: ["ORA-01031", "source unavailable"] }) });
    await controller.collect();
    const state = controller.getState();
    assert.equal(state.collection.status, status);
    assert.equal(state.collection.type, type);
    assert.match(state.collection.description, /ORA-01031.*source unavailable.*保留存档 9 条/);
    assert.equal(state.collecting, false);
    assert.equal(state.items.length, 1);
    if (status !== "success") assert.notEqual(state.collection.title, "采集成功");
  });
}

test("unknown POST status fails closed", () => {
  assert.equal(collectionOutcome({ count: 60 }).type, "error");
  assert.match(collectionOutcome({}).description, /响应状态无效/);
});

for (const status of [502, 422]) {
  test(`HTTP ${status} displays actual detail without manufacturing an empty success`, async () => {
    const controller = create({ collectSqlProfile: async () => { throw Object.assign(new Error("actual collector reason"), { status }); } });
    await controller.load();
    await controller.collect();
    const state = controller.getState();
    assert.equal(state.collection.status, status === 422 ? "unsupported" : "failed");
    assert.match(state.collection.description, /actual collector reason/);
    assert.equal(state.items.length, 1);
    assert.equal(state.collecting, false);
    if (status === 422) assert.equal(state.capability.live_supported, false);
  });
}

test("no history and filter mismatch are distinct; GET never collects", async () => {
  let postCount = 0;
  let result = archive(1, null);
  const controller = create({ getSqlProfile: async () => result, collectSqlProfile: async () => { postCount += 1; } });
  await controller.load();
  assert.equal(sqlProfileView(controller.getState()).empty, "暂无存档数据");
  result = { ...result, total_stored: 60 };
  await controller.load({ schema: "NO_MATCH", source: "awr", days: 1, only_issues: true });
  assert.equal(sqlProfileView(controller.getState()).label, "无匹配");
  assert.match(sqlProfileView(controller.getState()).empty, /筛选无匹配/);
  assert.equal(postCount, 0);
});

test("empty collection with retained history is not no-history", async () => {
  const controller = create({ getSqlProfile: async () => ({ ...archive(1, null), total_stored: 60 }),
    collectSqlProfile: async () => ({ status: "empty", count: 0, retained_count: 60 }) });
  await controller.collect();
  assert.equal(sqlProfileView(controller.getState()).label, "本次采集无数据");
  assert.match(sqlProfileView(controller.getState()).empty, /筛选无匹配/);
});

test("refresh clears rows immediately and errors cannot masquerade as no history", async () => {
  const pending = deferred();
  let calls = 0;
  const controller = create({ getSqlProfile: () => ++calls === 1 ? archive() : pending.promise });
  await controller.load();
  const refresh = controller.load();
  assert.deepEqual(controller.getState().items, []);
  assert.equal(controller.getState().loading, true);
  pending.reject(new Error("archive unavailable"));
  await refresh;
  assert.equal(sqlProfileView(controller.getState()).label, "请求失败");
  assert.match(sqlProfileView(controller.getState()).empty, /请求失败.*archive unavailable/);
  assert.equal(controller.getState().latestCaptureTime, null);
});

for (const lateError of [false, true]) {
  test(`latest filter request wins over late ${lateError ? "failure" : "success"} and finally`, async () => {
    const old = deferred(), latest = deferred();
    let count = 0;
    const controller = create({ getSqlProfile: () => ++count === 1 ? old.promise : latest.promise });
    const first = controller.load({ schema: "old" });
    const second = controller.load({ schema: "new" });
    if (lateError) old.reject(new Error("stale error")); else old.resolve(archive(1, "stale"));
    await first;
    assert.equal(controller.getState().loading, true);
    assert.equal(controller.getState().readError, null);
    latest.resolve(archive(1, "latest"));
    await second;
    assert.equal(controller.getState().items[0].sql_id, "latest");
  });
}

test("old GET cannot overwrite a newer completed GET", async () => {
  const old = deferred();
  let count = 0;
  const controller = create({ getSqlProfile: () => ++count === 1 ? old.promise : archive(1, "new") });
  const first = controller.load();
  await controller.load();
  old.resolve(archive(1, "old"));
  await first;
  assert.equal(controller.getState().items[0].sql_id, "new");
});

test("collect refresh uses current filters and duplicate collect is ignored", async () => {
  const post = deferred();
  const queries = [];
  let posts = 0;
  const controller = create({ getSqlProfile: async (id, query) => { queries.push({ id, ...query }); return archive(id); },
    collectSqlProfile: () => { posts += 1; return post.promise; } });
  await controller.load({ schema: "old" });
  const collecting = controller.collect();
  await controller.collect();
  assert.equal(sqlProfileView(controller.getState()).label, "采集中");
  await controller.load({ schema: "new", source: "awr", only_issues: true, days: 7 });
  post.resolve({ status: "partial", errors: ["disk reads failed"], count: 40 });
  await collecting;
  assert.equal(posts, 1);
  assert.deepEqual(queries.at(-1), { id: 1, schema: "new", source: "awr", only_issues: true, days: 7 });
  assert.equal(controller.getState().collection.type, "warning");
});

test("switching instance invalidates late GET, POST, detail and collect refresh", async () => {
  const get = deferred(), post = deferred(), detail = deferred();
  let reads = 0, notifications = 0;
  const old = create({ getSqlProfile: () => { reads += 1; return get.promise; }, collectSqlProfile: () => post.promise,
    getSqlProfileDetail: () => detail.promise, onDetailError: () => { notifications += 1; } });
  const unsubscribe = old.subscribe(() => { notifications += 1; });
  const pending = [old.load(), old.collect(), old.openDetail({ sql_id: "old", instance_id: 1 })];
  unsubscribe();
  old.dispose();
  const current = create({}, 2);
  await current.load();
  const before = notifications;
  get.resolve(archive()); post.resolve({ status: "success", count: 60 }); detail.reject(new Error("old detail"));
  await Promise.all(pending);
  assert.equal(reads, 1);
  assert.equal(notifications, before);
  assert.equal(current.getState().items[0].instance_id, 2);
  assert.equal(current.getState().collection, null);
});

test("StrictMode cleanup and reactivation invalidate prior generations", async () => {
  const old = deferred();
  let count = 0;
  const controller = create({ getSqlProfile: () => ++count === 1 ? old.promise : archive(1, "new") });
  const pending = controller.load();
  controller.dispose(); controller.activate();
  await controller.load();
  old.resolve(archive(1, "old"));
  await pending;
  assert.equal(controller.getState().items[0].sql_id, "new");
});

test("detail requests are latest-only and closing invalidates late completion", async () => {
  const old = deferred(), latest = deferred();
  let calls = 0;
  const controller = create({ getSqlProfileDetail: () => ++calls === 1 ? old.promise : latest.promise });
  const first = controller.openDetail({ sql_id: "old" });
  const second = controller.openDetail({ sql_id: "new" });
  old.resolve({ profile: { sql_id: "old" } }); await first;
  assert.equal(controller.getState().detailLoading, true);
  controller.closeDetail();
  latest.resolve({ profile: { sql_id: "new" } }); await second;
  assert.equal(controller.getState().drawerOpen, false);
  assert.equal(controller.getState().detail, null);
});

test("same SQL_ID across schema/plan requests the exact profile row and ignores late sibling detail", async () => {
  const firstRow = { instance_id: 1, id: 101, sql_id: "shared-sql", schema_name: "APP_A", plan_hash: "plan-a" };
  const secondRow = { instance_id: 1, id: 102, sql_id: "shared-sql", schema_name: "APP_B", plan_hash: "plan-b" };
  const old = deferred(), latest = deferred();
  const calls = [];
  const controller = create({ getSqlProfileDetail: (sqlId, instanceId, profileId) => {
    calls.push([sqlId, instanceId, profileId]);
    return profileId === firstRow.id ? old.promise : latest.promise;
  } });
  const first = controller.openDetail(firstRow);
  const second = controller.openDetail(secondRow);
  assert.deepEqual(calls, [["shared-sql", 1, 101], ["shared-sql", 1, 102]]);
  latest.resolve({ profile: secondRow }); await second;
  old.resolve({ profile: firstRow }); await first;
  assert.deepEqual(controller.getState().detail.profile, secondRow);
  assert.equal(controller.getState().detailLoading, false);
});

test("capture timestamps retain backend strings without historical timezone reinterpretation", async () => {
  for (const time of ["2026-09-08 09:58:00", "2026-09-08T09:58:00Z", "2026-09-08 09:58:00 UTC"]) {
    const payload = archive();
    payload.latest_capture_time = time;
    payload.items[0].capture_time = time;
    const controller = create({ getSqlProfile: async () => payload });
    await controller.load();
    assert.equal(controller.getState().latestCaptureTime, time);
    assert.equal(controller.getState().items[0].capture_time, time);
  }
});

test("not-collected plans stay null while measured risks are retained", async () => {
  const payload = { profile: { id: 101, sql_id: "shared-sql", instance_id: 1 },
    plan: null, plans: [], plan_status: "not_collected",
    risks: [{ severity: "warning", risk_type: "measured_elapsed", reason: "Measured elapsed 5000ms" }] };
  const controller = create({ getSqlProfileDetail: async () => payload });
  await controller.openDetail(payload.profile);
  assert.deepEqual(controller.getState().detail, payload);
  assert.equal(controller.getState().detail.plan, null);
  assert.equal(controller.getState().detail.risks.length, 1);
});

test("invalid archive contracts and foreign-instance rows fail closed", async () => {
  for (const payload of [{}, { items: [] }, archive(2)]) {
    const controller = create({ getSqlProfile: async () => payload });
    await controller.load();
    assert.equal(sqlProfileView(controller.getState()).label, "请求失败");
    assert.deepEqual(controller.getState().items, []);
  }
});

test("only Oracle/MySQL default to live; explicit capability overrides engine", () => {
  for (const type of ["oracle", "mysql", "MYSQL"]) assert.equal(liveCollectionSupported(null, type), true);
  for (const type of ["postgresql", "sqlserver", undefined]) assert.equal(liveCollectionSupported(null, type), false);
  assert.equal(liveCollectionSupported({ live_supported: false }, "oracle"), false);
  assert.equal(liveCollectionSupported({ live_supported: true }, "postgresql"), true);
});

test("successful POST followed by failed archive refresh retains separate read error", async () => {
  const controller = create({ getSqlProfile: async () => { throw new Error("refresh unavailable"); } });
  await controller.collect();
  const state = controller.getState();
  assert.equal(state.collection.status, "success");
  assert.equal(state.readError, "refresh unavailable");
  assert.equal(sqlProfileView(state).label, "请求失败");
  assert.equal(state.collecting, false);
});
