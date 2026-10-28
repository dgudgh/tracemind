import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Space, Tag } from "antd";
import { FileSearchOutlined } from "@ant-design/icons";
import { inspectionPlanResult } from "./modules/assistantPlanState";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { getInspectionReportStructuredDetail } from "./modules/reportApi";
import { InspectionReportDetailPage } from "./InspectionReportDetailPage";

const verdicts = { succeeded: "巡检执行成功", partial: "巡检部分完成", failed: "巡检失败",
  unknown: "巡检结果未知", cancelled: "巡检已取消", expired: "巡检已过期" };
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function reportFailure(error) {
  if ([401, 403].includes(error?.status)) return "当前登录或报告读取权限被拒绝，未重试。";
  if (error?.status === 404) return "原任务报告未找到或当前不可访问；不代表可以重新执行巡检。";
  if (error?.code === "inspection_report_timeout") return "报告读取超时；未重试，也未重新执行巡检。";
  if (error?.code === "inspection_report_mismatch") return "报告任务、实例或结果证据与原引用不符，未展示。";
  return "原任务报告读取失败，未重试，也未重新执行巡检。";
}

function waitForReport(promise, op) {
  let timer;
  const stopped = new Promise((_, reject) => {
    op.cancel = () => reject(new Error("Inactive report read"));
    timer = setTimeout(() => reject(Object.assign(new Error("Report timeout"), { code: "inspection_report_timeout" })), 90000);
  });
  return Promise.race([promise, stopped]).finally(() => { clearTimeout(timer); op.cancel = null; });
}

function matchingReport(value, reference) {
  // The existing detail route repeats the task and frozen target, and returns saved findings.
  return object(value) && object(value.task) && object(value.instance)
    && value.task_id === reference.job_id && value.task.task_id === reference.job_id
    && value.task.instance_id === reference.instance_id && value.instance.id === reference.instance_id
    && Number.isSafeInteger(value.task.total_count) && value.task.total_count > 0
    && value.task.total_count === reference.run.result_count
    && Array.isArray(value.all_findings) && value.all_findings.length === value.task.total_count
    && value.all_findings.every((item) => object(item) && item.task_id === reference.job_id && item.instance_id === reference.instance_id)
    && object(value.summary) && value.summary.total === value.task.total_count
    && [value.risk_items, value.modules, value.category_summary].every((items) => Array.isArray(items) && items.every(object));
}

export function AssistantInspectionResult({ plan, step, isActive = () => true }) {
  const reference = inspectionPlanResult(plan, step);
  const key = reference ? JSON.stringify([plan.plan_id, plan.revision, plan.binding_hash, step.seq, reference]) : "";
  const session = useRef(getAuthSession()).current;
  const alive = useRef(true), target = useRef(key), epoch = useRef(0), operation = useRef(null);
  const activity = useRef(isActive);
  target.current = key; activity.current = isActive;
  const [record, setRecord] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = () => {
    try { return alive.current && typeof activity.current === "function" && activity.current() === true; }
    catch { return false; }
  };
  const current = (op) => active() && target.current === op.key && epoch.current === op.epoch && isAuthSessionCurrent(op.session);
  const detail = record && record.key === key && active() && isAuthSessionCurrent(record.session) ? record.value : null;
  const readable = Boolean(reference?.run.results_published && reference.run.result_count > 0);
  const sessionCurrent = active() && Boolean(session.token) && isAuthSessionCurrent(session);
  const enabled = Boolean(key && readable && sessionCurrent);

  useEffect(() => {
    alive.current = true; epoch.current += 1;
    setRecord(null); setError(""); setBusy(false);
    return () => { alive.current = false; epoch.current += 1; operation.current?.cancel?.(); operation.current = null; };
  }, [key]);

  const readReport = async () => {
    if (!enabled || operation.current || !active() || !isAuthSessionCurrent(session)
      || target.current !== key || !key) return;
    const op = { key, epoch: ++epoch.current, session };
    operation.current = op; setBusy(true); setRecord(null); setError("");
    try {
      if (!current(op)) return;
      const value = await waitForReport(getInspectionReportStructuredDetail(reference.job_id, { instance_id: reference.instance_id }), op);
      if (!current(op)) return;
      if (!matchingReport(value, reference)) throw Object.assign(new Error("Wrong report reference"), { code: "inspection_report_mismatch" });
      // A rows-based report cannot upgrade an unknown/partial job verdict to success.
      if (reference.step_status !== "succeeded" && value.task.task_status === "success") {
        throw Object.assign(new Error("Contradictory report verdict"), { code: "inspection_report_mismatch" });
      }
      if (current(op)) setRecord({ key: op.key, session: op.session, value });
    } catch (failure) { if (current(op)) setError(reportFailure(failure)); }
    finally { if (operation.current === op) { operation.current = null; if (current(op)) setBusy(false); } }
  };

  return (
    <Space direction="vertical" size="small" className="full-width">
      {!sessionCurrent ? <Alert type="warning" showIcon message="窗口已关闭或登录会话已变化，请重新打开已授权的计划详情。" />
        : !reference ? <Alert type="warning" showIcon message="持久巡检结果引用不完整或关联不符，不能定位原报告。" /> : <>
        <Descriptions size="small" column={1}>
          <Descriptions.Item label="巡检任务">{reference.job_id}</Descriptions.Item>
          <Descriptions.Item label="巡检状态"><Tag color={reference.step_status === "succeeded" ? "success" : "warning"}>{verdicts[reference.step_status]}</Tag></Descriptions.Item>
          <Descriptions.Item label="已保存结果">{reference.run.result_count}</Descriptions.Item>
          {reference.error_code && <Descriptions.Item label="原因代码">{reference.error_code}</Descriptions.Item>}
        </Descriptions>
        {!readable && <Alert type="warning" showIcon message="该任务没有已发布的结果行，无法读取报告。" />}
        <Button icon={<FileSearchOutlined />} loading={busy} disabled={!enabled || busy} onClick={readReport}>读取原任务报告</Button>
      </>}
      {error && <Alert type="warning" showIcon message={error} />}
      {detail && <InspectionReportDetailPage detail={detail} exportsDisabled />}
    </Space>
  );
}

export default AssistantInspectionResult;
