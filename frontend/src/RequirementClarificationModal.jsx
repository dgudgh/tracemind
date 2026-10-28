import React, { useEffect, useState } from "react";
import { Alert, Button, Input, InputNumber, Modal, Select, Space, Typography } from "antd";
import { buildClarificationSubmission, normalizeClarificationAnswers } from "./modules/requirementClarification";

const empty = (value) => value === undefined || value === null || (typeof value === "string" && !value.trim());

export function RequirementClarificationModal({ open, questions = [], initialAnswers = {}, goal, reason,
  requirementText = "", instances = [], selectedInstanceId, loading, onCancel, onSubmit }) {
  const [values, setValues] = useState({});
  const [error, setError] = useState("");
  const originalRequirement = typeof requirementText === "string" ? requirementText.trim().slice(0, 2000) : "";
  const visibleGoal = typeof goal === "string" && !/[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2}/i.test(goal)
    ? goal.trim().slice(0, 200) : "";

  useEffect(() => {
    if (!open) return;
    const normalized = normalizeClarificationAnswers(initialAnswers, questions);
    const next = { ...(normalized.answers || {}) };
    questions.forEach((question) => {
      if (question.type === "instance" && empty(next[question.key])) next[question.key] = selectedInstanceId;
    });
    setValues(next);
    setError(normalized.error || "");
  }, [open, questions, initialAnswers, selectedInstanceId]);

  const submit = () => {
    const submission = buildClarificationSubmission({ questions, values, initialAnswers, selectedInstanceId, instances });
    if (submission.error) {
      setError(submission.error);
      return;
    }
    setError("");
    onSubmit?.(submission);
  };

  return <Modal open={open} centered title="继续完善任务" onCancel={onCancel}
    footer={<Space><Button onClick={onCancel} disabled={loading}>取消</Button><Button type="primary" loading={loading} onClick={submit}>继续生成任务</Button></Space>}>
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {originalRequirement && <Alert showIcon type="info" message="原始需求" description={originalRequirement} />}
      <div>
        <Typography.Title level={5} style={{ margin: 0 }}>补齐以下信息</Typography.Title>
        {visibleGoal && <Typography.Text type="secondary">用于继续完成“{visibleGoal}”任务。</Typography.Text>}
      </div>
      {reason && <Alert showIcon type="info" message={reason} />}
      {questions.map((question) => <label key={question.key} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <Typography.Text strong>{question.label}{question.required ? " *" : ""}</Typography.Text>
        {question.type === "number" ? <InputNumber style={{ width: "100%" }} precision={0} value={values[question.key]}
          onChange={(value) => setValues((current) => ({ ...current, [question.key]: value }))} />
          : question.type === "instance" ? <Select showSearch optionFilterProp="label" value={values[question.key]}
            placeholder="选择目标实例" options={instances.map((item) => ({ value: item.id,
              label: `${item.instance_name || item.name || `实例 ${item.id}`} · ${(item.db_type || "").toUpperCase()}` }))}
            onChange={(value) => setValues((current) => ({ ...current, [question.key]: value }))} />
            : <Input type={question.type === "datetime" ? "datetime-local" : "text"} value={values[question.key] || ""}
              onChange={(event) => setValues((current) => ({ ...current, [question.key]: event.target.value }))} />}
      </label>)}
      {error && <Alert showIcon type="error" message={error} />}
    </Space>
  </Modal>;
}
