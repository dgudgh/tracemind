import React from "react";
import { Alert } from "antd";

export function PlanSafetyNotice({
  mode = "plan_only",
  approved = false,
  message,
}) {
  const executable = mode === "execute" && approved;
  return (
    <Alert
      showIcon
      type={executable ? "success" : "warning"}
      message={message || (executable ? "审批已通过，可按执行策略运行" : "当前处于方案准备阶段")}
      description={executable
        ? "执行仍受目标白名单、脚本哈希、超时和审计策略约束。"
        : "提交审批前不会执行外部变更；审批通过后仍需满足目标白名单、脚本哈希和审计策略。"}
    />
  );
}
