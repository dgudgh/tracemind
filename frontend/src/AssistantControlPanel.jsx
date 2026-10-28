import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Input, Space, Tag, Typography } from "antd";
import { PauseOutlined, PlayCircleOutlined, StopOutlined, FileSearchOutlined, ReloadOutlined } from "@ant-design/icons";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { getAssistantControl, requestAssistantControl, getAssistantControlSessionFingerprint, validateAssistantControlRecovery,
  isAssistantControlExecutionId, isAssistantControlLoginEpoch } from "./modules/assistantControlApi";
import "./assistantControl.css";

// Mount/refresh read only the current projection; original-key lookup is explicit.
// Persist intent before POST, never replay it, and unlock unknown only for that exact command.
// A target/session/unmount epoch gates every late response and storage publication.
// The parent also supplies dynamic activity during the close-to-unmount interval.
const actions = { pause: "请求本地暂停", resume: "请求本地续跑", stop: "请求本地停止", probe: "提交观察命令" };
const queues = { queued: "待投递", running: "在途", archived: "已归档", reconciliation_required: "待对账", cancelled: "已取消投递", expired: "投递已过期", blocked: "已阻止", paused: "本地已暂停", stopped: "本地已停止后续下发" };
const observations = { running: "运行中", paused: "本地静止", stopped: "本地后续下发已停止", completed: "本地执行已完成", unknown: "未知，待对账" };
const commandStates = { accepted: "已受理，未证明应用", sent: "已投递，未证明应用", acknowledged: "已确认收件，未证明应用", applied: "已应用至本地调度", rejected: "已拒绝", expired: "命令已过期", superseded: "已被后续命令或完成状态取代" };
const storageKey = (id) => `mesguard.assistant.control-recovery.v1:${id}`;
const navigationEpoch = () => globalThis.performance.timeOrigin;

function waitForControl(promise) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error("Control response timeout"), { code: "assistant_control_timeout" })), 90000); });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function newControlKey() {
  const bytes = new Uint8Array(16); globalThis.crypto.getRandomValues(bytes);
  return `control:${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function controlError(error) {
  if (error?.code === "auth_session_changed") return "确认时的登录会话已变化，控制命令未发送；请重新读取状态并确认。";
  if ([401, 403].includes(error?.status)) return "当前会话或控制权限被服务端拒绝；不会自动重试。";
  if (error?.code === "assistant_control_storage_unavailable") return "控制定位上下文无法安全保存，保持 unknown；不会重发或换 key。";
  if (error?.code === "assistant_control_timeout") return "控制回包超时，未证明命令失败或目标已停止；保持 unknown。";
  if (error?.code === "assistant_control_response_invalid") return "控制响应结构或原命令关联不符，保持待核对；未放宽校验。";
  if (error?.code === "control_version_conflict") return "控制版本已变化；请先刷新当前状态，再重新明确决定。";
  if (error?.code === "execution_requires_reconciliation") return "执行仍需人工对账；不能以暂停、续跑或停止清除未知。";
  if (error?.code === "control_unsupported") return "此执行不支持当前控制；未承诺远端强停或 fence。";
  if (error?.status === 404) return "未找到执行或原命令回执；查不到不代表可以重做。";
  if (error?.status === 409) return "服务端拒绝当前控制版本、绑定、证据或状态；请只读核对。";
  if ([400, 422].includes(error?.status)) return "控制请求未通过服务端校验；请核对原因与当前状态。";
  return "控制结果未确认；只读核对原 key，不会自动重发。";
}

function actionAllowed(value, action) {
  if (!value) return false;
  if (action === "probe") return true;
  if (value.observed_state === "unknown" || value.queue_state === "reconciliation_required") return false;
  if (action === "resume") return value.control_enabled && value.queue_state === "paused" && value.desired_state === "pause" && value.checkpoint_hash !== null;
  if (!["queued", "running", "paused"].includes(value.queue_state) || value.desired_state === "stop") return false;
  return action === "stop" || (action === "pause" && value.desired_state !== "pause");
}

export function AssistantControlPanel({ executionId, onChanged, isActive = () => true }) {
  const session = getAuthSession();
  const [record, setRecord] = useState(null);
  const [journalRecord, setJournalRecord] = useState(null);
  const [identityRecord, setIdentityRecord] = useState(null);
  const [unknownRecord, setUnknownRecord] = useState(null);
  const [manualRecord, setManualRecord] = useState(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [manualKey, setManualKey] = useState("");
  const [busy, setBusy] = useState("");
  const [fresh, setFresh] = useState(false);
  const [gate, setGate] = useState(true);
  const [error, setError] = useState("");
  const alive = useRef(true), target = useRef(executionId), epoch = useRef(0), operation = useRef(null), journalRef = useRef(null);
  const uncertainRef = useRef(null), confirmation = useRef(false);
  const activity = useRef(isActive);
  activity.current = isActive;
  target.current = executionId;
  const active = () => {
    try { return typeof activity.current === "function" && activity.current() === true; }
    catch { return false; }
  };
  const current = (op) => active() && alive.current && target.current === op.executionId && epoch.current === op.epoch && isAuthSessionCurrent(op.session);
  const visible = (item) => active() && item && item.executionId === executionId && isAuthSessionCurrent(item.session);
  const control = visible(record) ? record.value : null;
  const journal = visible(journalRecord) ? journalRecord.value : null;
  const identity = visible(identityRecord) ? identityRecord.value : null;
  const unknown = visible(unknownRecord);
  const manual = visible(manualRecord) ? manualRecord.value : null;
  const recoveryUsable = Boolean(journal && identity && !gate && journal.expires_at > Date.now());
  const canSubmit = Boolean(control && identity && !gate && fresh && !unknown && !busy && confirmed
    && reason.trim() && Array.from(reason).length <= 300);

  const markUnknown = (op) => {
    if (current(op)) { uncertainRef.current = { executionId: op.executionId, session: op.session }; setUnknownRecord(uncertainRef.current); }
  };
  const setConfirmation = (value) => { confirmation.current = value === true; setConfirmed(value === true); };
  const persist = (value, op) => {
    validateAssistantControlRecovery(value);
    if (!current(op)) throw new TypeError("Stale control session");
    const entry = { value, executionId: op.executionId, session: op.session };
    journalRef.current = entry; setJournalRecord(entry);
    try { globalThis.sessionStorage.setItem(storageKey(op.executionId), JSON.stringify(value)); }
    catch { throw Object.assign(new Error("Control recovery storage unavailable"), { code: "assistant_control_storage_unavailable" }); }
  };
  const forget = (op) => {
    if (!current(op)) return;
    try { globalThis.sessionStorage.removeItem(storageKey(op.executionId)); }
    catch { throw Object.assign(new Error("Control recovery storage unavailable"), { code: "assistant_control_storage_unavailable" }); }
    journalRef.current = null; setJournalRecord(null);
  };
  const publish = (value, op) => {
    if (!current(op)) return;
    setRecord({ value, executionId: op.executionId, session: op.session }); setFresh(true); setConfirmation(false);
  };
  const notify = (value, op) => {
    if (current(op) && typeof onChanged === "function") {
      try { onChanged(value); }
      catch { if (current(op)) setError("服务端控制回执已保存；父视图更新未确认，请手动刷新。"); }
    }
  };

  const initialize = async () => {
    if (!active()) return;
    const op = { executionId, session: getAuthSession(), epoch: ++epoch.current, kind: "read" };
    operation.current = op; setBusy("read"); setGate(true); setFresh(false); setError("");
    let cacheFailure = false;
    try {
      if (!isAssistantControlExecutionId(executionId)) throw new TypeError("Invalid execution ID");
      try {
        if (!isAssistantControlLoginEpoch(op.session.loginEpoch)) throw new TypeError("No durable session identity");
        const fingerprint = await waitForControl(getAssistantControlSessionFingerprint(op.session.token));
        if (!current(op)) return;
        setIdentityRecord({ value: fingerprint, executionId, session: op.session });
        const raw = globalThis.sessionStorage.getItem(storageKey(executionId));
        if (raw !== null) {
          if (typeof raw !== "string" || raw.length > 2048) throw new TypeError("Invalid control locator");
          const saved = validateAssistantControlRecovery(JSON.parse(raw));
          if (saved.execution_id !== executionId) throw new TypeError("Wrong execution locator");
          const matches = saved.session_fingerprint === fingerprint && saved.login_epoch === op.session.loginEpoch
            && (saved.navigation_epoch !== navigationEpoch() || saved.generation === op.session.generation);
          if (!matches) forget(op);
          else {
            persist({ ...saved, generation: op.session.generation, navigation_epoch: navigationEpoch() }, op);
            if (saved.uncertain || saved.expires_at <= Date.now()) markUnknown(op);
            if (saved.expires_at <= Date.now()) throw new TypeError("Expired control locator");
          }
        }
      } catch { if (!current(op)) return; cacheFailure = true; markUnknown(op); }
      if (!current(op)) return;
      setGate(cacheFailure);
      const value = await waitForControl(getAssistantControl(executionId));
      if (!current(op)) return;
      const saved = journalRef.current?.value;
      if (saved && saved.plan_id !== value.plan_id) { cacheFailure = true; markUnknown(op); }
      publish(value, op); setGate(cacheFailure);
      if (cacheFailure) setError("会话或恢复定位未确认，保持只读；可人工按已知原 key 核对，不解锁重发。");
    } catch (failure) { if (current(op)) setError(controlError(failure)); }
    finally { if (current(op) && operation.current === op) { operation.current = null; setBusy(""); } }
  };

  useEffect(() => { alive.current = true; return () => { alive.current = false; epoch.current += 1; operation.current = null; }; }, []);
  useEffect(() => {
    setRecord(null); setJournalRecord(null); setIdentityRecord(null); setUnknownRecord(null); setManualRecord(null);
    journalRef.current = null; uncertainRef.current = null; setReason(""); setConfirmation(false); setManualKey("");
    initialize();
    return () => { epoch.current += 1; operation.current = null; };
  }, [executionId, session.token, session.generation, session.loginEpoch]);

  const refresh = async () => {
    if (!active() || operation.current || target.current !== executionId || !isAuthSessionCurrent(session) || !isAssistantControlExecutionId(executionId)) return;
    const op = { executionId, session: getAuthSession(), epoch: ++epoch.current, kind: "read" };
    operation.current = op; setBusy("read"); setFresh(false); setConfirmation(false); setError("");
    try {
      const value = await waitForControl(getAssistantControl(executionId));
      if (!current(op)) return;
      if (journal && journal.plan_id !== value.plan_id) { markUnknown(op); setGate(true); throw Object.assign(new Error("Plan changed"), { code: "assistant_control_response_invalid" }); }
      publish(value, op);
    } catch (failure) { if (current(op)) setError(controlError(failure)); }
    finally { if (current(op) && operation.current === op) { operation.current = null; setBusy(""); } }
  };

  const recover = async () => {
    if (!active() || operation.current || target.current !== executionId || !isAuthSessionCurrent(session) || !isAssistantControlExecutionId(executionId)) return;
    const saved = journalRef.current;
    const candidate = saved && saved.executionId === executionId && isAuthSessionCurrent(saved.session) ? saved.value : null;
    const expired = candidate && Date.now() >= candidate.expires_at;
    const data = candidate && !expired && identity && !gate && candidate.execution_id === executionId
      && candidate.login_epoch === getAuthSession().loginEpoch && candidate.session_fingerprint === identity
      && (!control || candidate.plan_id === control.plan_id) ? candidate : null;
    if (expired) {
      markUnknown({ executionId, session: getAuthSession(), epoch: epoch.current }); setGate(true); setConfirmation(false);
      setError("原控制定位上下文已过期，保持 unknown；仅可人工按原 key 只读核对。");
    }
    const key = data?.idempotency_key || manualKey;
    if (!key) { if (!expired) setError("请输入已知原 key；不会生成新 key。"); return; }
    const op = { executionId, session: getAuthSession(), epoch: ++epoch.current, kind: "recover" };
    operation.current = op; setBusy("recover"); setConfirmation(false); setError("");
    try {
      const value = await waitForControl(getAssistantControl(executionId, { idempotencyKey: key, ...(data ? { expected: {
        plan_id: data.plan_id, action: data.action, expected_control_version: data.expected_control_version, command_id: data.command_id,
      } } : {}) }));
      if (!current(op)) return;
      if (!value.command) throw Object.assign(new Error("Original command missing"), { code: "assistant_control_response_invalid" });
      if (!data || Date.now() >= data.expires_at) {
        if (data) setGate(true);
        setManualRecord({ value, executionId, session: op.session }); markUnknown(op);
        setError("仅展示服务端只读回执；原关联上下文未确认，不解锁控制提交。"); return;
      }
      persist({ ...data, command_id: value.command.command_id, uncertain: false }, op);
      publish(value, op); uncertainRef.current = null; setUnknownRecord(null); notify(value, op);
    } catch (failure) { if (current(op)) { markUnknown(op); setError(controlError(failure)); } }
    finally { if (current(op) && operation.current === op) { operation.current = null; setBusy(""); } }
  };

  const issue = async (action) => {
    if (!active() || operation.current || target.current !== executionId || !isAuthSessionCurrent(session) || uncertainRef.current || !confirmation.current
      || !canSubmit || !actionAllowed(control, action) || !isAssistantControlLoginEpoch(getAuthSession().loginEpoch)) return;
    const op = { executionId, session: getAuthSession(), epoch: ++epoch.current, kind: action, submitted: false };
    operation.current = op; setBusy(action); setFresh(false); setConfirmation(false); setError("");
    const previous = journalRef.current?.value;
    try {
      const now = Date.now(), key = newControlKey();
      const locator = { schema: "capability.control_recovery/v1", execution_id: executionId, plan_id: control.plan_id, action,
        expected_control_version: control.control_version, command_id: null, idempotency_key: key,
        login_epoch: op.session.loginEpoch, session_fingerprint: identity, generation: op.session.generation, navigation_epoch: navigationEpoch(),
        created_at: now, expires_at: now + 86400000, uncertain: true };
      persist(locator, op);
      if (!current(op)) return;
      const promise = requestAssistantControl(executionId, action, { expected_control_version: control.control_version, reason }, { idempotencyKey: key, planId: control.plan_id, authSession: op.session });
      op.submitted = true;
      const value = await waitForControl(promise);
      if (!current(op)) return;
      persist({ ...locator, command_id: value.command.command_id, uncertain: false }, op);
      publish(value, op); setReason(""); notify(value, op);
    } catch (failure) {
      if (!current(op)) return;
      setFresh(false);
      if (failure?.code === "assistant_control_storage_unavailable" || (op.submitted
        && (!Number.isInteger(failure?.status) || failure.status >= 500 || failure.code === "assistant_control_response_invalid"))) markUnknown(op);
      else {
        try { if (previous) persist(previous, op); else forget(op); }
        catch { markUnknown(op); }
      }
      setError(controlError(failure));
    } finally { if (current(op) && operation.current === op) { operation.current = null; setBusy(""); } }
  };

  return <section className="assistant-control-panel" aria-label={`执行 ${executionId} 本地控制`}>
    <div className="assistant-control-toolbar"><Typography.Title level={5}>执行 #{executionId} 本地控制</Typography.Title>
      <Button icon={<ReloadOutlined />} loading={busy === "read"} disabled={Boolean(busy)} onClick={refresh}>刷新状态</Button></div>
    <Alert type="info" showIcon message="local_step_boundary · external_stop / external_fence: unsupported"
      description="仅控制本地步骤安全点；在途调用可能继续。受理或收件不等于静止，不承诺远端强停或外部 fence，不改变业务结果。" />
    {unknown && <Alert type="warning" showIcon message="控制结果 unknown，禁止重发或新控制命令" description="刷新只读取当前状态；只有原 key 对应的同一命令回执可以解除提交未知。" />}
    {error && <Alert type={unknown ? "warning" : "error"} showIcon message={error} />}
    {control && <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
      <Descriptions.Item label="计划号"><Typography.Text code>{control.plan_id}</Typography.Text></Descriptions.Item>
      <Descriptions.Item label="控制版本">{control.control_version}</Descriptions.Item>
      <Descriptions.Item label="本地代次">{control.fencing_epoch}</Descriptions.Item>
      <Descriptions.Item label="控制已启用">{control.control_enabled ? "是" : "否（可由明确控制请求启用）"}</Descriptions.Item>
      <Descriptions.Item label="期望状态">{control.desired_state}</Descriptions.Item>
      <Descriptions.Item label="投递状态"><Tag>{queues[control.queue_state]}</Tag></Descriptions.Item>
      <Descriptions.Item label="服务端观察">{observations[control.observed_state] || "未观察"}</Descriptions.Item>
      <Descriptions.Item label="checkpoint 摘要" span={2}><Typography.Text code>{control.checkpoint_hash || "尚无安全点证据"}</Typography.Text></Descriptions.Item>
      {control.command && <>
        <Descriptions.Item label="当前回执命令"><Typography.Text code>{control.command.command_id}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="命令动作 / 版本">{control.command.action} / {control.command.control_version}</Descriptions.Item>
        <Descriptions.Item label="命令状态">{commandStates[control.command.status]}</Descriptions.Item>
        <Descriptions.Item label="确认阶段">{control.command.ack_stage || "尚未确认"}</Descriptions.Item>
      </>}
    </Descriptions>}
    {(journal || unknown) && <div className="assistant-control-recovery">
      {journal && <Typography.Text code>{journal.idempotency_key}</Typography.Text>}
      {!recoveryUsable && <label>已知原命令 key<Input aria-label="已知原命令 key" value={manualKey} maxLength={128} disabled={Boolean(busy)} onChange={(event) => { setManualKey(event.target.value); setManualRecord(null); }} /></label>}
      <Button icon={<FileSearchOutlined />} loading={busy === "recover"} disabled={Boolean(busy) || !(recoveryUsable ? journal.idempotency_key : manualKey)} onClick={recover}>核对原控制请求</Button>
      {manual && <Alert type="warning" showIcon message="只读原命令回执，未解除未知"
        description={`${manual.command.command_id} · ${manual.command.action} · v${manual.command.control_version} · ${commandStates[manual.command.status]} · ${manual.command.ack_stage || "尚未确认"}`} />}
    </div>}
    <label className="assistant-control-reason">操作原因<Input aria-label="控制操作原因" value={reason} maxLength={300} disabled={Boolean(busy) || unknown || gate}
      onChange={(event) => { setReason(event.target.value); setConfirmation(false); }} /></label>
    <Checkbox checked={confirmed} disabled={Boolean(busy) || unknown || gate || !fresh} onChange={(event) => setConfirmation(event.target.checked)}>
      确认对执行 #{executionId} 按当前控制版本 {control?.control_version ?? "未读取"} 提交所点命令
    </Checkbox>
    <Space wrap className="assistant-control-actions">
      <Button icon={<PauseOutlined />} loading={busy === "pause"} disabled={!canSubmit || !actionAllowed(control, "pause")} onClick={() => issue("pause")}>{actions.pause}</Button>
      <Button icon={<PlayCircleOutlined />} loading={busy === "resume"} disabled={!canSubmit || !actionAllowed(control, "resume")} onClick={() => issue("resume")}>{actions.resume}</Button>
      <Button danger icon={<StopOutlined />} loading={busy === "stop"} disabled={!canSubmit || !actionAllowed(control, "stop")} onClick={() => issue("stop")}>{actions.stop}</Button>
      <Button icon={<FileSearchOutlined />} loading={busy === "probe"} disabled={!canSubmit} onClick={() => issue("probe")}>{actions.probe}</Button>
    </Space>
  </section>;
}
