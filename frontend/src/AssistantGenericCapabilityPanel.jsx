import React, { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Checkbox, Collapse, Descriptions, Input, InputNumber, Modal, Select, Space, Tag, Typography } from "antd";
import { PlayCircleOutlined, SafetyCertificateOutlined, SaveOutlined } from "@ant-design/icons";
import { createAssistantPlan, executeAssistantPlan, requestAssistantPlanApproval } from "./modules/assistantApi";
import { schemaFields, initialValues, buildParams, isExecutable, effectNeedsApproval, targetProblem, planPayload,
  customerCapabilityName, customerCapabilityFieldLabel, customerCapabilityEnumLabel } from "./modules/assistantCapabilityForm";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";

// One manual entry for every server-executable capability. The form is generated from the
// capability's own parameter schema, so adding a handler does not need another bespoke page;
// the server still validates the submitted values and re-checks permission, target and
// approval. No prompt, no model and no free-text command is involved in this path.
export function AssistantGenericCapabilityPanel({ open, onClose, capabilities = [], instances = [], onOpenPlan,
  initialCapabilityId, initialInstanceId, initialParams, customerMode = false }) {
  const [capabilityId, setCapabilityId] = useState();
  const [target, setTarget] = useState();
  const [values, setValues] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const sessionRef = useRef(getAuthSession());
  const draftKey = useRef(null);

  const executable = useMemo(() => capabilities.filter(isExecutable), [capabilities]);
  const selected = executable.find((item) => item.capability_id === capabilityId);
  const fields = useMemo(() => schemaFields(selected?.params_schema), [selected]);
  const needsApproval = effectNeedsApproval(selected);
  const targetIssue = open && selected ? targetProblem(selected, instances, target) : "";
  const effectTargets = selected && Array.isArray(selected.authorized_engines) && selected.authorized_engines.length
    ? instances.filter((item) => selected.authorized_engines.includes(item.db_type)) : instances;

  useEffect(() => {
    if (!open) return;
    sessionRef.current = getAuthSession();
    draftKey.current = globalThis.crypto?.randomUUID?.() || `${Date.now()}`;
    setCapabilityId(initialCapabilityId || undefined);
    setTarget(initialInstanceId || undefined);
    setValues({}); setError(""); setUncertain(false);
  }, [open, initialCapabilityId, initialInstanceId, sessionRef.current?.token, sessionRef.current?.loginEpoch]);

  useEffect(() => {
    if (!selected) { setValues({}); return; }
    setValues({ ...initialValues(fields), ...(initialParams || {}) });
    if (selected.requires_instance && target && !effectTargets.some((item) => item.id === target)) setTarget(undefined);
    // The panel can open before the capability catalog resolves; reinitialize when the
    // selected capability finally arrives, not only when capabilityId changes.
  }, [capabilityId, selected?.capability_id, fields, initialParams]);

  const current = () => open && isAuthSessionCurrent(sessionRef.current);

  const submit = async (mode) => {
    if (!current() || busy || !selected || targetIssue) return;
    let params;
    try { params = buildParams(fields, values); } catch (problem) { setError(problem.message || String(problem)); return; }
    setBusy(true); setError("");
    const session = sessionRef.current;
    let created = null;
    try {
      created = await createAssistantPlan(planPayload(selected, {
        targetId: target, params, requestApproval: mode !== "save" || needsApproval, validForSeconds: 3600,
      }), { ...(draftKey.current ? { idempotencyKey: draftKey.current } : {}), authSession: session });
      if (!current()) return;
      draftKey.current = null;
      if (mode === "execute") {
        const done = await executeAssistantPlan(created.plan_id, { authSession: session });
        if (!current()) return;
        onOpenPlan?.(done.plan_id);
      } else if (mode === "approve") {
        await requestAssistantPlanApproval(created.plan_id, { expected_revision: created.revision }, { authSession: session });
        if (!current()) return;
        onOpenPlan?.(created.plan_id);
      } else {
        onOpenPlan?.(created.plan_id);
      }
      onClose?.();
    } catch (problem) {
      if (!current()) return;
      setUncertain(Boolean(created) || (problem?.status ?? 0) >= 500 || !problem?.status);
      setError(problem?.message || "请求结果待核对，请刷新清单；未自动重试");
      if (created) onOpenPlan?.(created.plan_id);
    } finally {
      if (isAuthSessionCurrent(sessionRef.current)) setBusy(false);
    }
  };

  const technicalDetails = selected && <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
    { key: "kind", label: "动作类别", children: <Tag color={needsApproval ? "orange" : "blue"}>{selected.kind}</Tag> },
    { key: "engines", label: "支持引擎", children: (selected.authorized_engines || selected.engines || []).join(" / ") || "全局" },
    { key: "risk", label: "风险", children: selected.risk },
    { key: "verify", label: "结果校验", children: selected.verification },
  ]} />;

  return <Modal open={open} centered width={880} title={customerMode ? "生成分析报告" : "通用能力清单"} onCancel={onClose}
    footer={<Button onClick={onClose}>关闭</Button>} className="assistant-plan-modal">
    <div className="assistant-plan-new">
      {error && <Alert showIcon type={uncertain ? "warning" : "error"} message={uncertain ? "请求结果待核对" : "请求未完成"} description={error} />}
      <Space wrap>
        <Select aria-label={customerMode ? "报告生成能力" : "通用能力"} placeholder={customerMode ? "选择报告类型" : "选择已登记的可执行能力"} value={capabilityId} disabled={busy}
          style={{ width: 420, maxWidth: "100%" }} showSearch optionFilterProp="label" onChange={setCapabilityId}
          options={executable.map((item) => ({ value: item.capability_id,
            label: customerMode ? customerCapabilityName(item.capability_id)
              : `${item.title || item.capability_id} · ${item.capability_id} · v${item.definition_version}` }))} />
      </Space>
      {selected && (customerMode
        ? <Collapse ghost size="small" items={[{ key: "technical", label: "技术说明", children: technicalDetails }]} />
        : technicalDetails)}
      {selected && <>
        <div className="assistant-plan-fields">
          <label>目标实例<Select aria-label="通用能力目标实例" placeholder={selected.requires_instance ? "必须选择实例" : "全局（可选实例）"}
            value={target} allowClear={!selected.requires_instance} disabled={busy} onChange={setTarget}
            options={effectTargets.map((item) => ({ value: item.id, label: `${item.instance_name || item.name} · ${item.db_type} · ${item.host}:${item.port}` }))} /></label>
        </div>
        {fields.length > 0 && <div className="assistant-plan-fields">
          {fields.map((field) => <label key={field.name}>{customerMode ? customerCapabilityFieldLabel(field) : field.label}{field.required ? " *" : ""}
            {field.enum ? <Select aria-label={`参数 ${field.name}`} value={values[field.name]} disabled={busy}
              onChange={(value) => setValues((old) => ({ ...old, [field.name]: value }))}
              options={field.enum.map((value) => ({ value,
                label: customerMode ? customerCapabilityEnumLabel(field.name, value) : String(value) }))} />
              : field.type === "boolean" ? <Checkbox aria-label={`参数 ${field.name}`} checked={Boolean(values[field.name])} disabled={busy}
                onChange={(event) => setValues((old) => ({ ...old, [field.name]: event.target.checked }))} />
                : field.type === "integer" ? <InputNumber aria-label={`参数 ${field.name}`} value={values[field.name]} disabled={busy}
                  min={field.minimum ?? undefined} max={field.maximum ?? undefined} precision={0}
                  onChange={(value) => setValues((old) => ({ ...old, [field.name]: value ?? undefined }))} />
                  : <Input aria-label={`参数 ${field.name}`} value={values[field.name] ?? ""} disabled={busy}
                    maxLength={field.maxLength ?? undefined}
                    onChange={(event) => setValues((old) => ({ ...old, [field.name]: event.target.value }))} />}
          </label>)}
        </div>}
        <Typography.Text type="secondary">
          {customerMode && needsApproval ? "提交后进入审批流程，审批通过后才能生成报告。"
            : needsApproval ? "该动作会改变状态，提交后需另一账号独立审批，本页不会代替审批。" : "读取已有数据，不改变目标状态。"}
        </Typography.Text>
        {targetIssue && <Alert showIcon type="warning" message={targetIssue} />}
        <Space wrap>
          <Button icon={<SaveOutlined />} loading={busy} disabled={busy || Boolean(targetIssue)} onClick={() => submit("save")}>{customerMode ? "保存任务" : "保存冻结清单"}</Button>
          {!needsApproval && <Button type="primary" icon={<PlayCircleOutlined />} loading={busy} disabled={busy || Boolean(targetIssue)} onClick={() => submit("execute")}>确认并执行读取</Button>}
          {needsApproval && <Button type="primary" icon={<SafetyCertificateOutlined />} loading={busy} disabled={busy || Boolean(targetIssue)} onClick={() => submit("approve")}>提交审批</Button>}
        </Space>
      </>}
    </div>
  </Modal>;
}
