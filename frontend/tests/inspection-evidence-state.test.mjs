import assert from "node:assert/strict";
import test from "node:test";
import { evidenceExecutionStatus as status, evidenceEmptyText as empty, evidenceSqlList } from "../src/InspectionExecutionState.js";

test("failed Windows collection cannot display a successful empty query", () => {
  const payload = { state: "collection_failed", status: "warning", message: "权限不足" };
  assert.equal(status({ severity: "warning" }, payload), "failed");
  assert.match(empty(status({}, payload), payload, {}, true), /未取得查询结果.*权限不足/);
});

test("unsupported and inapplicable observations are skipped, not healthy", () => {
  for (const state of ["not_supported", "not_applicable"]) {
    const payload = { state, message: "不适用" };
    assert.equal(status({}, payload), "skipped");
    assert.match(empty(status({}, payload), payload, {}, true), /本项未执行/);
  }
});

test("risk and execution outcome stay separate for measured observations", () => {
  assert.equal(status({ severity: "critical" }, { state: "collected" }), "success");
  assert.equal(status({ severity: "warning" }, { status: "normal", sql: "select 1" }), "success");
  assert.equal(empty("success", { rows: [] }, {}, true), "查询完成，返回 0 行");
});

test("normal risk and missing or blank SQL never prove execution", () => {
  for (const payload of [{}, { status: "normal" }, { status: "success", sql: [null, "", "  ", {}] }]) {
    assert.equal(status({ severity: "normal", check_status: "normal" }, payload), "unknown");
  }
  assert.deepEqual(evidenceSqlList({ sql: [null, " ", 0, {}, " select 1 "] }), ["select 1"]);
});

test("explicit nonexecution wins over success and is case insensitive", () => {
  for (const payload of [
    { status: "SUCCESS", state: "NOT_EXECUTED", sql: "select 1" },
    { status: "normal", executed: false, sql: "select 1" },
    { state: "COLLECTED", status: "SKIPPED", sql: "select 1" },
  ]) assert.equal(status({}, payload), "skipped");
  assert.equal(status({}, { state: "collected", status: "FAILED" }), "failed");
});

test("backend execution_status overrides collection and risk without discarding explicit failure", () => {
  for (const execution_status of ["partial", "unknown", "running"]) {
    assert.equal(status({}, { execution_status, state: "collected", status: "normal", sql: "select 1" }), execution_status);
  }
  assert.equal(status({}, { execution_status: "success", status: "failed", sql: "select 1" }), "failed");
  assert.equal(status({}, { execution_status: "connection_failed", sql: "select 1" }), "failed");
  assert.equal(status({}, { execution_status: "success", value: 3 }), "success");
});

test("reference SQL and execution summaries are not successful zero-row queries", () => {
  const payload = { status: "normal", sql: "select 1", rows: [], evidence_source: "legacy_saved_metric" };
  assert.equal(status({}, payload), "unknown");
  assert.equal(empty(status({}, payload), payload, {}, true), "暂无采集明细");
  assert.equal(status({}, { evidence_kind: "execution_summary", status: "normal" }), "summary");
});

test("missing saved rows are distinct from a confirmed zero-row result", () => {
  assert.match(empty("success", { row_count: 8, rows: [] }, {}, true), /返回 8 行.*未保存/);
  assert.equal(empty("success", {}, {}, true), "暂无采集明细");
  assert.equal(empty("success", { row_count: 0 }, {}, true), "查询完成，返回 0 行");
  assert.equal(empty("success", { rows: [] }, {}, false), "暂无采集明细");
});

test("legacy warning and missing execution state never manufacture success", () => {
  assert.match(empty("warning", { message: "ORA-01031" }, {}, true), /未取得查询结果/);
  assert.equal(status({}, {}), "unknown");
  assert.equal(empty("unknown", {}, {}, true), "暂无采集明细");
});
