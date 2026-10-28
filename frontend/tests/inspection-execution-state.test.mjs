import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@babel/parser";
import { inspectionOutcome, inspectionNotice } from "../src/InspectionExecutionState.js";
import { runAssistantInspectionSequence } from "../src/modules/assistantInspection.js";

const page = readFileSync(new URL("../src/InspectionPages.jsx", import.meta.url), "utf8");
const reportPage = readFileSync(new URL("../src/InspectionReportDetailPage.jsx", import.meta.url), "utf8");
const ast = parse(page, { sourceType: "module", plugins: ["jsx"] });
const findNode = (node, predicate) => {
  if (!node || typeof node !== "object") return undefined;
  if (predicate(node)) return node;
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      const found = findNode(child, predicate);
      if (found) return found;
    }
  }
};
const viewNode = findNode(ast, (node) => node.type === "FunctionDeclaration" && node.id.name === "buildTaskView");
const buildTaskView = vm.runInNewContext(`(${page.slice(viewNode.start, viewNode.end)})`, {
  inspectionOutcome, normalizeRiskLevel: (value) => value || "normal", formatDuration: () => "-",
});

test("HTTP success cannot override failed metadata at any supported response level", () => {
  for (const key of ["task", "report", "summary"]) {
    for (const statusKey of ["task_status", "execution_status", "status"]) {
      const payload = {
        task: { task_id: "test", task_status: "success", overall_score: 100 },
        [key]: { [statusKey]: " FAILED ", failure_reason: "collector unavailable", overall_score: 100 },
      };
      const outcome = inspectionOutcome(payload);
      assert.equal(outcome.status, "failed");
      assert.equal(outcome.score, null);
      assert.equal(inspectionNotice(payload).type, "error");
      assert.match(inspectionNotice(payload).message, /collector unavailable/);
    }
  }
});

test("missing, partial, empty and nonexecuted results cannot manufacture health scores", () => {
  for (const task_status of [undefined, "partial", "skipped", "plan_only", "running", "pending", "canceled"]) {
    const payload = { task: { task_status, overall_score: 100 } };
    assert.equal(inspectionOutcome(payload).score, null);
    assert.notEqual(inspectionNotice(payload).type, "success");
  }
  for (const extra of [{ findings: [] }, { all_findings: [] }, { summary: { total: 0 } }]) {
    const payload = { task: { task_status: "success", overall_score: 100 }, ...extra };
    assert.equal(inspectionOutcome(payload).status, "empty");
    assert.equal(inspectionOutcome(payload).score, null);
  }
});

test("evidence failures or all-skipped evidence override stale success metadata", () => {
  const payload = (states) => ({
    task: { task_status: "success", overall_score: 100 },
    findings: states.map((state) => ({ evidence: { state } })),
  });
  assert.equal(inspectionOutcome(payload(["not_applicable", "not_supported"])).status, "empty");
  assert.equal(inspectionOutcome(payload(["collection_failed"])).status, "failed");
  assert.equal(inspectionOutcome(payload(["collection_failed", "collected"])).status, "partial");
});

test("list, one-click and structured detail agree on failure and preserve a real zero score", () => {
  for (const task_status of ["failed", "partial", "unknown", "success"]) {
    const task = { task_status, overall_score: 0, total_count: 1 };
    const list = buildTaskView(task);
    const detail = inspectionOutcome({ task, summary: { score: 100, total: 1 } });
    assert.equal(list.task_status, detail.status);
    assert.equal(list.overall_score, detail.score);
    assert.equal(list.score, detail.score);
    if (task_status !== "success") assert.equal(list.risk_level, "unknown");
  }
  assert.equal(inspectionOutcome({ task_status: "success", overall_score: 0 }).score, 0);
  for (const score of [null, "", " ", "NaN", -1, 101]) {
    assert.equal(inspectionOutcome({ task_status: "success", overall_score: score }).score, null);
  }
});

test("one-click handler uses business outcome instead of a fulfilled POST", async () => {
  const node = findNode(ast, (item) => item.type === "VariableDeclarator" && item.id.name === "runInspect");
  for (const response of [
    { task: { task_status: "success", overall_score: 100 }, report: { status: "failed" } },
    { task: { task_status: "partial", overall_score: 100 } },
    { task: { task_status: "success", overall_score: 100 }, findings: [] },
    {},
  ]) {
    const messages = [];
    const handler = vm.runInNewContext(`(${page.slice(node.init.start, node.init.end)})`, {
      instanceId: 1, templateKey: "__auto__", AUTO_TEMPLATE_KEY: "__auto__", selectedTemplate: null,
      dbType: "mysql", selectedInstance: { db_type: "mysql" },
      runInspectionTask: async () => response, setLoading() {}, setResult() {}, loadHistory: async () => {},
      inspectionOutcome, inspectionNotice,
      message: Object.fromEntries(["success", "warning", "error"].map((key) => [key, (value) => messages.push([key, value])])),
    });
    assert.equal(await handler(), false);
    assert.equal(messages.length, 1);
    assert.notEqual(messages[0][0], "success");
  }
});

test("mixed batch counts rejected, failed, partial and unconfirmed JSON once each", async () => {
  const responses = [
    { task: { task_id: "ok", task_status: "success", overall_score: 95 } },
    { task: { task_id: "bad", overall_score: 100 }, report: { status: "failed" } },
    { task: { task_id: "partial", task_status: "partial", overall_score: 100 } },
    { task: { task_id: "empty", task_status: "success", overall_score: 100 }, findings: [] },
    { task: { task_id: "unknown", overall_score: 100 } },
    null,
    new Error("network unavailable"),
  ];
  const progress = [];
  const result = await runAssistantInspectionSequence({
    targets: responses.map((_, id) => ({ id, db_type: "mysql" })),
    runInspection: async (_, id) => {
      if (responses[id] instanceof Error) throw responses[id];
      return responses[id];
    },
    onProgress: (value) => progress.push(value),
  });
  assert.equal(result.total, 7);
  assert.equal(result.completed, 7);
  assert.equal(result.succeeded, 1);
  assert.equal(result.failed, 6);
  assert.equal(result.status, "partial");
  assert.ok(result.items.slice(1).every((item) => item.score === null));
  assert.ok(progress.every((state) => state.completed === state.succeeded + state.failed));
  assert.equal(result.items[1].taskId, "bad");
});

test("detail parses and binds score, risk and categories to the shared outcome", () => {
  parse(reportPage, { sourceType: "module", plugins: ["jsx"] });
  assert.match(reportPage, /const outcome = inspectionOutcome\(detail\)/);
  assert.match(reportPage, /value=\{outcome.score \?\? "未生成"\}/);
  assert.match(reportPage, /complete=\{outcome.complete\}/);
  assert.doesNotMatch(reportPage, /executionPartial|task.task_status === "failed"/);
});
