import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Radio, Select, Space, Tag, Tooltip, Typography } from "antd";
import { BulbOutlined, ReloadOutlined, RobotOutlined } from "@ant-design/icons";
import { isAuthSessionCurrent } from "./modules/authStorage";
import { getProposal, listProposalProviders, newProposalKey, proposalAdoption,
  readStoredProposalKey, requestProposal, scopeFromTargets, storeProposalKey, clearProposalKey,
} from "./modules/assistantProposalApi";

const decisionLabels = { matched: "命中固定流程", needs_input: "待补参", not_matched: "未命中" };
const reasonLabels = {
  workflow_provider_context_not_approved: "未确认允许发送给该模型。",
  workflow_provider_not_authorized: "所选模型未启用或配置已变更，未调用模型。",
  workflow_provider_unavailable: "模型调用失败，未产生候选；可改用手工流程。",
  assistant_proposal_response_invalid: "响应不符合候选合同，保持未采用。",
  assistant_proposal_catalog_changed: "该流程已不在当前授权目录，不能采用。",
  422: "候选结构被拒（越权、越界或含不允许字段），未生成可提交草稿。",
  403: "权限、资产范围或模型发送授权被拒绝。",
  409: "模型配置或幂等身份冲突，请刷新后重新确认。",
  502: "模型不可用；本页不会伪造候选，可手工选流程。",
};
function message(error) {
  return reasonLabels[error?.code] || reasonLabels[error?.status] || "请求未获确认；候选不会被自动采用或执行。";
}

/**
 * C11: optional AI assistance, strictly as a proposal. The panel can only produce a
 * structure that the user explicitly adopts into the manual draft; it never freezes,
 * approves or enqueues anything, and works only with a provider the user consents to.
 */
export function AssistantProposalPanel({ session, instances = [], catalog = [], disabled = false, onAdopt }) {
  const [providerRecord, setProviderRecord] = useState(null);
  const [providers, setProviders] = useState(null);
  const [providerError, setProviderError] = useState("");
  const [providerId, setProviderId] = useState(null);
  const [text, setText] = useState("");
  const [scopeMode, setScopeMode] = useState("explicit");
  const [targetIds, setTargetIds] = useState([]);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [resultRecord, setResultRecord] = useState(null);
  const [keyRecord, setKeyRecord] = useState(null);
  const alive = useRef(true);
  const epoch = useRef(0);
  const operation = useRef(null);
  const current = (saved) => alive.current && isAuthSessionCurrent(saved);
  const providersValue = providerRecord && isAuthSessionCurrent(providerRecord.session) ? providerRecord.value : null;
  const result = resultRecord && isAuthSessionCurrent(resultRecord.session) ? resultRecord.value : null;
  const storedKey = keyRecord && isAuthSessionCurrent(keyRecord.session) ? keyRecord.key : null;
  const selected = providersValue?.items.find((item) => item.config_id === providerId) || null;

  const loadProviders = async () => {
    if (!session?.token) return;
    const saved = session, context = epoch.current;
    setProviderError("");
    try {
      const value = await listProposalProviders({ authSession: saved });
      if (!current(saved) || context !== epoch.current) return;
      setProviderRecord({ value, session: saved });
      setProviders(value.items);
      setProviderId((old) => (value.items.some((item) => item.config_id === old) ? old
        : value.items.find((item) => item.is_default)?.config_id ?? value.items[0]?.config_id ?? null));
    } catch (failure) {
      if (current(saved) && context === epoch.current) {
        setProviderRecord(null); setProviders(null); setProviders([]); setProviderError(message(failure));
      }
    }
  };

  useEffect(() => {
    alive.current = true;
    const stored = readStoredProposalKey();
    if (stored && session?.loginEpoch === stored.login_epoch) setKeyRecord({ key: stored.key, session });
    loadProviders();
    return () => { alive.current = false; epoch.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.token, session?.generation, session?.loginEpoch]);

  const scope = scopeMode === "global" ? { mode: "global" } : scopeFromTargets(targetIds);
  const canSubmit = Boolean(!disabled && !busy && selected && consent && text.trim() && scope && session?.token);

  const submit = async () => {
    if (!canSubmit || operation.current) return;
    const op = { session, epoch: epoch.current };
    operation.current = op; setBusy("propose"); setError("");
    const key = newProposalKey();
    const valid = () => current(op.session) && op.epoch === epoch.current && operation.current === op;
    try {
      storeProposalKey(key, op.session.loginEpoch);
      if (valid()) setKeyRecord({ key, session: op.session });
      const value = await requestProposal({
        text: text.trim(), scope, provider_config_id: selected.config_id,
        provider_revision: selected.revision, allow_model_context: true, confirmed_policy_id: null,
      }, { authSession: op.session, idempotencyKey: key });
      if (!valid()) return;
      setResultRecord({ value, session: op.session });
      clearProposalKey(); setKeyRecord(null);
    } catch (failure) {
      // The request may have reached the server: keep the original key for a read-only GET.
      if (valid()) setError(message(failure));
    } finally {
      if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); }
    }
  };

  const recover = async () => {
    if (!storedKey || operation.current || !session?.token) return;
    const op = { session, epoch: epoch.current };
    operation.current = op; setBusy("recover"); setError("");
    const valid = () => current(op.session) && op.epoch === epoch.current && operation.current === op;
    try {
      const value = await getProposal({ authSession: op.session, idempotencyKey: storedKey });
      if (!valid()) return;
      setResultRecord({ value, session: op.session });
      clearProposalKey(); setKeyRecord(null);
    } catch (failure) {
      if (valid()) setError(message(failure));
    } finally {
      if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); }
    }
  };

  const adopt = () => {
    if (!result || !onAdopt || disabled) return;
    try {
      onAdopt(proposalAdoption(result, catalog));
    } catch (failure) {
      setError(message(failure));
    }
  };

  return <section className="assistant-proposal">
    <div className="assistant-proposal-heading">
      <Typography.Title level={5}><RobotOutlined /> AI 候选（仅结构）</Typography.Title>
      <Tooltip title="刷新可选模型"><Button aria-label="刷新可选模型" icon={<ReloadOutlined />} disabled={Boolean(busy)} onClick={loadProviders} /></Tooltip>
    </div>
    <Alert type="info" showIcon message="模型只提议结构；采用后仍需人工冻结、复核与提交，不会自动执行。" />
    {providerError && <Alert type="warning" showIcon message={providerError} />}
    {providers && providers.length === 0 && !providerError && <Typography.Text type="secondary">没有已启用的模型；可在下方手工选择固定流程。</Typography.Text>}
    {providers && providers.length > 0 && <>
      <label className="assistant-proposal-field"><span>模型</span>
        <Select aria-label="候选模型" value={providerId ?? undefined} disabled={disabled || Boolean(busy)} onChange={setProviderId}
          options={providers.map((item) => ({ value: item.config_id, label: `${item.name} · ${item.model || item.provider_type}` }))} />
      </label>
      <label className="assistant-proposal-field"><span>意图</span>
        <Input.TextArea aria-label="流程意图文本" value={text} maxLength={4000} rows={2} disabled={disabled || Boolean(busy)}
          onChange={(event) => setText(event.target.value)} placeholder="例如：对所选实例运行巡检采集" />
      </label>
      <fieldset className="assistant-workflow-group"><legend>明确范围</legend>
        <Radio.Group aria-label="候选范围" value={scopeMode} disabled={disabled || Boolean(busy)}
          onChange={(event) => { setScopeMode(event.target.value); setTargetIds([]); }}
          options={[{ value: "explicit", label: "明确实例" }, { value: "global", label: "全局读取" }]} />
        {scopeMode === "explicit" && <Select aria-label="候选目标实例" mode="multiple" maxCount={100} value={targetIds}
          disabled={disabled || Boolean(busy)} onChange={setTargetIds} placeholder="未选择目标"
          options={instances.filter((item) => Number.isSafeInteger(item.id) && item.id > 0).map((item) => ({
            value: item.id, label: `${item.instance_name || item.name || item.id} · ${item.db_type} · #${item.id}` }))} />}
      </fieldset>
      <Checkbox checked={consent} disabled={disabled || Boolean(busy)} onChange={(event) => setConsent(event.target.checked)}>
        允许将本条意图发送到所选模型（不含凭据与实例地址）
      </Checkbox>
      <Space wrap>
        <Button type="primary" icon={<BulbOutlined />} loading={busy === "propose"} disabled={!canSubmit} onClick={submit}>请求结构候选</Button>
        {storedKey && <Button icon={<ReloadOutlined />} loading={busy === "recover"} disabled={Boolean(busy)} onClick={recover}>原 key 只读核对</Button>}
      </Space>
      {storedKey && <Alert type="warning" showIcon message="上次请求未获回包；只能用原 key 回读，不会自动重发。" />}
    </>}
    {error && <Alert type="error" showIcon message={error} />}
    {result && <div className="assistant-proposal-result">
      <Space wrap><Tag color={result.decision === "matched" ? "success" : "default"}>{decisionLabels[result.decision]}</Tag>
        {result.workflow && <Tag>{result.workflow.workflow_id} v{result.workflow.workflow_version}</Tag>}
        <Typography.Text type="secondary">{result.proposal_id}</Typography.Text></Space>
      {result.missing_fields.length > 0 && <Typography.Text>待补：{result.missing_fields.join("、")}</Typography.Text>}
      {result.candidate && <>
        <ol className="assistant-workflow-steps">{result.candidate.plan_inputs.length > 0 && result.candidate.plan_inputs[0].steps.map((step) => (
          <li key={step.seq}><span>{step.capability_id}@{step.definition_version}</span><small>{step.seq > 1 ? `依赖步骤 ${step.depends_on.join("、")}` : "起始步骤"}</small></li>
        ))}</ol>
        <Typography.Text type="secondary">该候选未被接受，也没有创建计划或批次。</Typography.Text>
        <Button disabled={disabled || !onAdopt} onClick={adopt}>采用为手工草稿</Button>
      </>}
    </div>}
  </section>;
}
