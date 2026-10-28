import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { assistantPlanApprovalExpired, customerPlanPresentation } from "../src/modules/assistantPlanCustomer.js";

const planId = `pl${"a".repeat(26)}`;
const otherPlanId = `pl${"b".repeat(26)}`;

function plan(overrides = {}) {
  return {
    plan_id: planId,
    capability_id: "diagnosis.root_cause.execute",
    instance_id: 9,
    status: "approved",
    outcome: null,
    approval_state: "approved",
    steps: [{ seq: 1, capability_id: "diagnosis.investigation.read" },
      { seq: 2, capability_id: "diagnosis.root_cause.execute" }],
    ...overrides,
  };
}

function presentation(overrides = {}) {
  return {
    status: "approved",
    label: "已审批，尚未执行",
    color: "default",
    summary: "计划尚未执行；执行必须再次明确确认。",
    steps: [{ seq: 1, label: "读取成功", color: "success", resultStatus: "available" },
      { seq: 2, label: "待执行", color: "default", resultStatus: "not_applicable" }],
    ...overrides,
  };
}

test("customer requirement is shown only for the exact entry plan and remains bounded", () => {
  const matched = customerPlanPresentation(plan(), presentation(), {
    requestPlanId: planId, requirementText: `  ${"现".repeat(2100)}  `, targetLabel: "生产库 · mysql",
  });
  assert.equal(matched.requirementMatched, true);
  assert.equal(matched.requirement.length, 2000);
  assert.equal(matched.target, "生产库 · mysql");
  const history = customerPlanPresentation(plan({ plan_id: otherPlanId }), presentation(), {
    requestPlanId: planId, requirementText: "不能套到另一条历史清单",
  });
  assert.equal(history.requirementMatched, false);
  assert.equal(history.requirement, "");
});

test("customer flow uses persisted approval, queue and result states without inventing success", () => {
  const queued = customerPlanPresentation(plan({ execution_queue_state: "queued" }), presentation(), {});
  assert.deepEqual(queued.stages.map((item) => [item.key, item.state]), [
    ["identified", "done"], ["approval", "done"], ["execution", "current"], ["result", "pending"],
  ]);
  assert.match(queued.nextAction, /已进入队列/);
  assert.equal(queued.goal, "定位问题根因并形成可核验的诊断结论");
  assert.equal(queued.statusLabel, "排队中");
  assert.deepEqual(queued.steps.map((item) => item.title), ["读取已有诊断上下文", "执行根因分析"]);

  const failed = customerPlanPresentation(plan({ status: "settled", outcome: "failed" }),
    presentation({ status: "failed", label: "执行失败", color: "error", summary: "计划执行失败，不推断缺失结果。" }), {});
  assert.equal(failed.stages.find((item) => item.key === "execution").state, "error");
  assert.equal(failed.stages.find((item) => item.key === "result").state, "error");
  assert.equal(failed.nextAction, "计划执行失败，不推断缺失结果。");

  const cancelled = customerPlanPresentation(plan({ status: "settled", outcome: "cancelled" }),
    presentation({ status: "cancelled", label: "已取消", color: "default", summary: "计划已在运行前取消。" }), {});
  assert.equal(cancelled.stages.find((item) => item.key === "execution").state, "warning");
  assert.notEqual(cancelled.stages.find((item) => item.key === "result").state, "done");
});

test("customer report steps reflect the confirmed report variant", () => {
  const afterSales = customerPlanPresentation(plan({ capability_id: "report.document.generate",
    steps: [{ seq: 1, capability_id: "report.document.generate", params: { report_type: "after_sales" } }] }),
  presentation({ steps: [{ seq: 1, label: "待执行", color: "default", resultStatus: "not_applicable" }] }), {});
  assert.equal(afterSales.goal, "按已确认的报告类型和时间范围生成报告");
  assert.equal(afterSales.steps[0].title, "生成售后分析报告");
  const unified = customerPlanPresentation(plan({ capability_id: "report.document.generate",
    steps: [{ seq: 1, capability_id: "report.document.generate", params: { report_type: "unified" } }] }),
  presentation({ steps: [{ seq: 1, label: "待执行", color: "default", resultStatus: "not_applicable" }] }), {});
  assert.equal(unified.steps[0].title, "生成统一运维报告");
});

test("customer approval copy distinguishes requesting approval from waiting for an approver", () => {
  const validated = customerPlanPresentation(plan({ status: "validated", approval_state: "pending" }),
    presentation({ status: "validated", label: "待确认执行" }), {});
  assert.equal(validated.stages.find((item) => item.key === "approval").detail, "等待请求人发起审批");
  assert.match(validated.nextAction, /请求人发起审批/);

  const awaiting = customerPlanPresentation(plan({ status: "awaiting_approval", approval_state: "pending" }),
    presentation({ status: "awaiting_approval", label: "等待审批" }), {});
  assert.equal(awaiting.stages.find((item) => item.key === "approval").detail, "等待授权用户审批");
  assert.match(awaiting.nextAction, /等待授权用户审批/);

  for (const [approvalState, expected] of [["expired", "审批已过期"], ["revoked", "审批已撤销，需先修订任务"]]) {
    const item = customerPlanPresentation(plan({ status: "validated", approval_state: approvalState }), presentation(), {});
    assert.equal(item.stages.find((entry) => entry.key === "approval").detail, expected);
    assert.match(item.nextAction, approvalState === "revoked" ? /先修订任务，再申请审批/ : /重新发起/);
  }
});

test("an approved plan whose persisted approval deadline passed is presented as expired", () => {
  const expired = plan({
    status: "approved",
    approval_state: "approved",
    approval_expires_at: "2000-01-01T00:00:00Z",
  });
  assert.equal(assistantPlanApprovalExpired(expired, Date.parse("2026-10-23T00:00:00Z")), true);
  const item = customerPlanPresentation(expired, presentation(), {});
  assert.equal(item.stages.find((entry) => entry.key === "approval").detail, "审批已过期，需先修订任务");
  assert.match(item.nextAction, /先修订任务，再申请审批/);
  assert.equal(assistantPlanApprovalExpired(plan({ approval_expires_at: "2099-01-01T00:00:00Z" }),
    Date.parse("2026-10-23T00:00:00Z")), false);
  assert.equal(assistantPlanApprovalExpired(plan({ approval_expires_at: "invalid" }),
    Date.parse("2026-10-23T00:00:00Z")), false);
});

test("an archived successful task keeps its terminal result after the old approval deadline", () => {
  const settled = plan({
    status: "settled",
    outcome: "succeeded",
    approval_state: "approved",
    approval_expires_at: "2000-01-01T00:00:00Z",
    execution_batch_id: `CB${"c".repeat(28)}`,
    execution_queue_state: "archived",
  });
  const item = customerPlanPresentation(settled, presentation({
    status: "succeeded",
    label: "计划成功",
    color: "success",
    summary: "计划已成功执行并保存结果。",
  }), {});
  assert.equal(assistantPlanApprovalExpired(settled, Date.parse("2026-10-23T00:00:00Z")), false);
  assert.equal(item.statusLabel, "计划成功");
  assert.equal(item.stages.find((entry) => entry.key === "approval").detail, "审批已通过");
  assert.equal(item.stages.find((entry) => entry.key === "result").state, "done");
  assert.equal(item.nextAction, "计划已成功执行并保存结果。");

  const unresolved = customerPlanPresentation(plan({ status: "running", execution_queue_state: "archived" }),
    presentation({ status: "running", label: "运行中" }), {});
  assert.match(unresolved.nextAction, /计划尚无终态/);
});

test("durable queue truth overrides a stale running plan presentation", () => {
  const runningView = presentation({ status: "running", label: "运行中", color: "processing" });
  const reconciliation = customerPlanPresentation(plan({ status: "running", execution_queue_state: "reconciliation_required" }), runningView, {});
  assert.equal(reconciliation.statusLabel, "结果待核对");
  assert.equal(reconciliation.stages.find((item) => item.key === "execution").detail, "队列结果需要人工核对");
  assert.match(reconciliation.nextAction, /人工核对/);

  const paused = customerPlanPresentation(plan({ status: "running", execution_queue_state: "paused" }), runningView, {});
  assert.equal(paused.statusLabel, "已暂停");
  assert.equal(paused.stages.find((item) => item.key === "execution").detail, "队列执行已暂停");
  assert.match(paused.nextAction, /暂停原因/);

  const queued = customerPlanPresentation(plan({ status: "approved", execution_queue_state: "queued" }), presentation(), {});
  assert.equal(queued.statusLabel, "排队中");
  assert.equal(queued.stages.find((item) => item.key === "execution").state, "current");
});

test("customer mode stays opt-in, hides technical progress metadata, and retains result permission guards", () => {
  const source = readFileSync(new URL("../src/AssistantPlanWorkspace.jsx", import.meta.url), "utf8");
  const clarification = readFileSync(new URL("../src/RequirementClarificationModal.jsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/assistantPlan.css", import.meta.url), "utf8");
  assert.match(source, /onIncidentReady, customerMode = false/);
  assert.match(source, /requestPlanId: request\?\.planId/);
  assert.match(source, /requirementText: request\?\.requirementText/);
  assert.match(source, /\{!customerMode && <Progress/);
  assert.match(source, /\{!customerMode && <Alert type=\{view\.status === "succeeded"/);
  assert.match(source, /<details className="assistant-plan-technical-details">/);
  assert.match(source, /disabled=\{approvalMode \|\| !row\.result_ref\}/);
  assert.match(source, /request\?\.initialTab === "history" && !request\.capability && !request\.planId && !request\.batchId/);
  assert.match(source, /label: customerMode \? "新建任务" : "新建清单"/);
  assert.match(source, /label: customerMode \? "审批任务" : "审批读取"/);
  assert.match(source, /label: customerMode \? "任务记录" : "历史清单"/);
  assert.match(source, /label: customerMode \? "任务进展" : "执行结果"/);
  assert.match(source, /customerMode \? "确认执行任务" : "执行已冻结清单"/);
  assert.match(source, /customerMode \? "修改任务" : "修订清单"/);
  assert.match(source, /customerMode \? "重新创建任务" : "重跑为新清单"/);
  assert.match(source, /"report\.document\.generate": \{ version: 3, kind: "collect", outputSchema: "report\.document_result\/v1", requiresInstance: true \}/);
  assert.match(source, /downloadReportArtifact\(value\.artifact_id, value\.format/);
  assert.match(source, />下载报告<\/Button>/);
  assert.match(clarification, /requirementText = ""/);
  assert.match(clarification, /requirementText\.trim\(\)\.slice\(0, 2000\)/);
  assert.match(clarification, />补齐以下信息<\/Typography\.Title>/);
  assert.match(clarification, /title="继续完善任务"/);
  assert.match(styles, /\.assistant-plan-customer-flow/);
  assert.match(styles, /@media \(max-width: 600px\)/);
});
