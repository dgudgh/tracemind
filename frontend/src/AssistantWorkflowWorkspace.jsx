import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Empty, Input, InputNumber, Modal, Radio, Select, Space, Table, Tag, Tooltip, Typography } from "antd";
import { FileSearchOutlined, FolderOpenOutlined, PlusOutlined, ReloadOutlined, SaveOutlined, SendOutlined } from "@ant-design/icons";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { onAuthExpired } from "./modules/authEvents";
import { AssistantProposalPanel } from "./AssistantProposalPanel";
import { enqueueAssistantBatch, getAssistantBatch, getAssistantBatchAdmission, getAssistantBatchRecoveryIdentity } from "./modules/assistantBatchApi";
import { WORKFLOW_NAMES, listAssistantWorkflows, getAssistantWorkflow, matchAssistantWorkflow, prepareAssistantWorkflow, getAssistantWorkflowPreparation,
  getAssistantWorkflowPlan, workflowDefaults, validateWorkflowInput, validateWorkflowRecovery, workflowRecoveryMatches, workflowBindings,
  workflowReference, sameWorkflow, workflowReady, newWorkflowKey, getAssistantWorkflowSource, reviseAssistantWorkflow,
  getAssistantWorkflowRevision, workflowCandidateHash } from "./modules/assistantWorkflowApi";
import "./assistantWorkflow.css";

const STORAGE = "mesguard.assistant.workflow-recovery.v1";
const navigationEpoch = () => globalThis.performance.timeOrigin;
const labels = { page: "页码", page_size: "每页条数", engine: "引擎", db_type: "数据库类型", enabled: "启用状态", policy_id: "模板 ID", timeout_seconds: "总超时（秒）", status: "作业状态", outcome: "业务结论", trigger_type: "触发来源", matrix: "引擎能力", templates: "模板", jobs: "已保存作业" };
const planLabels = { draft: "草稿", validated: "已校验", awaiting_approval: "待审批", approved: "已审批", running: "运行中", settled: "已终结" };
const outcomeLabels = { succeeded: "成功", partial: "部分完成", failed: "失败", cancelled: "已取消", expired: "已过期", unknown: "未知", blocked: "已阻止", needs_input: "待补参" };
function workflowError(error) {
  if (error?.code === "auth_session_changed") return "登录会话已变化，请重新打开并回读。";
  if (error?.code === "batch_control_requires_v2") return "该批次需在 v2 控制视图核对，不会重发或重放。";
  if (error?.code === "assistant_workflow_storage_unavailable") return "恢复定位无法安全保存；禁止提交，可用原 key 只读核对。";
  if (error?.code === "assistant_workflow_response_invalid" || error?.code === "assistant_batch_response_invalid") return "响应或冻结绑定不符合合同，保持待核对。";
  if (error?.code === "assistant_workflow_timeout") return "等待回包超时；服务端是否受理未知，只能用原 key 回读。";
  return ({ 401: "登录已失效，请重新鉴权。", 403: "权限或资产范围被拒绝。", 404: "对象或原请求不可见，未推断为未受理。", 409: "版本、幂等身份或当前状态冲突，请回读核对。", 422: "类型化参数或明确范围未通过校验。", 503: "服务或审计存储不可用，未确认成功。" })[error?.status]
    || (error instanceof TypeError ? "请核对必填参数、类型、范围及模板 ID。" : "请求未获确认，不会自动重发。");
}
function waitForWorkflow(promise) {
  let timer;
  const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error("Workflow deadline exceeded"), { code: "assistant_workflow_timeout" })), 90000); });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}
function SlotFields({ schema, value, onChange, disabled, prefix = "" }) {
  return <div className="assistant-workflow-fields">{Object.entries(schema.properties).map(([name, field]) => {
    const path = `${prefix}${name}`, label = labels[name] || field.title || name;
    if (field.type === "object") return <fieldset key={path} className="assistant-workflow-group"><legend>{label}</legend><SlotFields schema={field} value={value[name] || {}} onChange={(next) => onChange({ ...value, [name]: next })} disabled={disabled} prefix={`${path}.`} /></fieldset>;
    const nullable = field.anyOf?.some((part) => part.type === "null");
    const set = (next) => { const copy = { ...value }; if (next === undefined && !nullable) delete copy[name]; else copy[name] = next ?? null; onChange(copy); };
    let control;
    if (field.type === "integer") control = <InputNumber aria-label={path} min={field.minimum ?? 1} max={field.maximum ?? Number.MAX_SAFE_INTEGER} precision={0} value={value[name]} onChange={(next) => set(next ?? undefined)} disabled={disabled} />;
    else if (name === "enabled") control = <Select aria-label={path} value={value[name] === null || value[name] === undefined ? "any" : value[name] ? "enabled" : "disabled"} onChange={(next) => set(next === "any" ? null : next === "enabled")} disabled={disabled} options={[{ value: "any", label: "不限" }, { value: "enabled", label: "已启用" }, { value: "disabled", label: "已停用" }]} />;
    else if (field["x-csv-values"]) control = <Select aria-label={path} mode="multiple" value={value[name]?.split(",").map((part) => part.trim()).filter(Boolean) || []} onChange={(next) => set(next.length ? next.join(",") : null)} disabled={disabled} options={field["x-csv-values"].map((item) => ({ value: item, label: item }))} />;
    else control = <Select aria-label={path} allowClear value={value[name] ?? undefined} placeholder="不限" onChange={set} disabled={disabled} options={(name === "trigger_type" ? ["manual", "scheduled", "retry", "capability_plan"] : ["oracle", "mysql", "postgresql", "sqlserver", ...(name === "db_type" ? ["host"] : [])]).map((item) => ({ value: item, label: item }))} />;
    return <label key={path}><span>{label}{schema.required?.includes(name) && <span className="assistant-workflow-required"> *</span>}</span>{control}{Object.hasOwn(field, "default") && <span className="assistant-workflow-default">目录默认：{field.default === null ? "不限" : String(field.default)}</span>}</label>;
  })}</div>;
}

export function AssistantWorkflowWorkspace({ open, onClose, instances = [], onOpenPlan, initialPlan = null, initialAction = "prepare" }) {
  const session = getAuthSession();
  const [, setAuthTick] = useState(0);
  const [catalogRecord, setCatalogRecord] = useState(null);
  const [definitionRecord, setDefinitionRecord] = useState(null);
  const [slots, setSlots] = useState({});
  const [scopeMode, setScopeMode] = useState(null);
  const [targetIds, setTargetIds] = useState([]);
  const [perTargetPolicy, setPerTargetPolicy] = useState(false);
  const [policies, setPolicies] = useState({});
  const [operationKind, setOperationKind] = useState("prepare");
  const [sourcePlanId, setSourcePlanId] = useState("");
  const [matchText, setMatchText] = useState("");
  const [suggestion, setSuggestion] = useState(null);
  const [preparedRecord, setPreparedRecord] = useState(null);
  const [batchRecord, setBatchRecord] = useState(null);
  const [reviews, setReviews] = useState({});
  const [recoveryRecord, setRecoveryRecord] = useState(null);
  const [ownerRecord, setOwnerRecord] = useState(null);
  const [gate, setGate] = useState(true);
  const [pending, setPending] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [catalogError, setCatalogError] = useState("");
  const [confirmPrepare, setConfirmPrepare] = useState(false);
  const [confirmEnqueue, setConfirmEnqueue] = useState(false);
  const [confirmNewDraft, setConfirmNewDraft] = useState(false);
  const [manualKey, setManualKey] = useState("");
  const [actionMode, setActionMode] = useState(initialAction);
  const [sourceRecord, setSourceRecord] = useState(null);
  const [revisionRecord, setRevisionRecord] = useState(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceRequired, setSourceRequired] = useState(Boolean(initialPlan));
  const alive = useRef(true), visible = useRef(open), epoch = useRef(0), selection = useRef(0), operation = useRef(null);
  const recoveryRef = useRef(null), frozenRef = useRef(null), pendingRef = useRef(false), dataSession = useRef(session);
  const launch = JSON.stringify([typeof initialPlan === "string" ? initialPlan : initialPlan?.plan_id, initialPlan?.revision, initialPlan?.binding_hash, initialAction]);
  const surface = useRef({ open, token: session.token, generation: session.generation, loginEpoch: session.loginEpoch, launch });
  const lifecycle = useRef(0), catalogEpoch = useRef(0), identityEpoch = useRef(0), batchEpoch = useRef(0);
  if (surface.current.open !== open || surface.current.token !== session.token || surface.current.generation !== session.generation || surface.current.loginEpoch !== session.loginEpoch || surface.current.launch !== launch) {
    lifecycle.current += 1; surface.current = { open, token: session.token, generation: session.generation, loginEpoch: session.loginEpoch, launch };
  }
  const view = lifecycle.current;
  visible.current = open;
  const current = (saved) => alive.current && visible.current && view === lifecycle.current && isAuthSessionCurrent(saved);
  const renderContext = epoch.current;
  const canAct = () => current(session) && renderContext === epoch.current;
  const sameRecord = (record) => record && isAuthSessionCurrent(record.session) ? record.value : null;
  const catalog = sameRecord(catalogRecord);
  const definition = sameRecord(definitionRecord);
  const prepared = sameRecord(preparedRecord);
  const source = sameRecord(sourceRecord), revised = sameRecord(revisionRecord);
  const planView = prepared || (revised ? { workflow: revised.workflow, total: 1, items: [revised.summary] } : null);
  const batch = sameRecord(batchRecord);
  const recovery = sameRecord(recoveryRecord);
  const owner = sameRecord(ownerRecord);
  const validAction = ["prepare", "revise", "rerun"].includes(actionMode);
  const locked = Boolean(!validAction || busy || gate || pending || planView || sourceLoading || ((sourceRequired || actionMode !== "prepare") && !source));
  const sourceLocked = Boolean(source && actionMode === "rerun");
  const revisable = Boolean(source && source.summary.outcome === null && ["draft", "validated", "awaiting_approval", "approved"].includes(source.summary.status));
  const chosenTargets = targetIds.map((id) => instances.find((item) => item.id === id));
  const targetStamp = JSON.stringify(targetIds.map((id) => { const item = instances.find((value) => value.id === id); return [id, item?.db_type, item?.host, item?.port, item?.instance_name || item?.name]; }));
  const previousTargets = useRef(targetStamp);
  let payload = null;
  if (definition && scopeMode) {
    const common = { ...slots }; if (perTargetPolicy) delete common.policy_id;
    const proposed = { operation: operationKind, slots: common, scope: scopeMode === "global" ? { mode: "global" } : {
      mode: "explicit", targets: targetIds.map((id) => ({ instance_id: id, ...(perTargetPolicy ? { slots: { policy_id: policies[id] } } : {}) })),
    }, ...(operationKind === "rerun" ? { source_plan_id: sourcePlanId } : {}) };
    try { validateWorkflowInput(definition, proposed); if (scopeMode === "global" || chosenTargets.every(Boolean)) payload = proposed; }
    catch { payload = null; }
  }
  const canEnqueue = Boolean(planView && recovery && !recovery.batch_id && !gate && !pending && !busy
    && planView.items.every((item) => reviews[item.plan_id]?.checked && workflowReady(item)));
  const remember = (value, saved) => {
    validateWorkflowRecovery(value);
    if (!isAuthSessionCurrent(saved)) throw Object.assign(new Error("Session changed"), { code: "auth_session_changed" });
    recoveryRef.current = { value, session: saved }; setRecoveryRecord(recoveryRef.current);
    try { globalThis.sessionStorage.setItem(STORAGE, JSON.stringify(value)); if (globalThis.sessionStorage.getItem(STORAGE) !== JSON.stringify(value)) throw new Error("Not persisted"); }
    catch { throw Object.assign(new Error("Storage unavailable"), { code: "assistant_workflow_storage_unavailable" }); }
  };
  const forget = () => { globalThis.sessionStorage.removeItem(STORAGE); if (globalThis.sessionStorage.getItem(STORAGE) !== null) throw new Error("Storage not cleared"); recoveryRef.current = null; setRecoveryRecord(null); };
  const markPending = () => { pendingRef.current = true; setPending(true); setConfirmEnqueue(false); };
  const storePrepared = (value, saved) => { frozenRef.current = { value, session: saved }; setPreparedRecord(frozenRef.current); setReviews({}); setConfirmPrepare(false); setConfirmEnqueue(false); };
  const closeWorkspace = () => { visible.current = false; lifecycle.current += 1; epoch.current += 1; selection.current += 1; onClose(); };
  const editDraft = (change) => {
    if (!canAct() || operation.current || pendingRef.current || frozenRef.current || gate || sourceLocked) return;
    selection.current += 1; change(); setConfirmPrepare(false); setConfirmEnqueue(false); setError("");
  };
  // Adopting a model candidate only fills this draft. The catalog entry is the authority:
  // its definition hash was already matched against the proposal, and freezing stays a
  // separate, explicit confirmation step.
  const adoptProposal = (adoption) => {
    if (!canAct() || operation.current || pendingRef.current || frozenRef.current || gate || source || planView || batch) return;
    selection.current += 1;
    setDefinitionRecord({ value: adoption.definition, session });
    setSlots(adoption.slots); setScopeMode(adoption.scope.mode);
    setTargetIds(adoption.scope.mode === "global" ? [] : adoption.scope.targets.map((item) => item.instance_id));
    setPerTargetPolicy(false); setPolicies({}); setOperationKind("prepare"); setSourcePlanId("");
    setConfirmPrepare(false); setConfirmEnqueue(false); setReviews({}); setSuggestion(null); setError("");
  };
  const loadCatalog = async () => {
    const saved = session, context = epoch.current, read = ++catalogEpoch.current;
    setCatalogError("");
    try {
      const value = await waitForWorkflow(listAssistantWorkflows({ authSession: saved }));
      if (current(saved) && context === epoch.current && read === catalogEpoch.current) setCatalogRecord({ value: value.items, session: saved });
    } catch (failure) { if (current(saved) && context === epoch.current && read === catalogEpoch.current) { setCatalogRecord(null); setCatalogError(workflowError(failure)); } }
  };
  const loadIdentity = async () => {
    if (!current(session) || operation.current) return;
    const saved = session, context = epoch.current, read = ++identityEpoch.current;
    setGate(true);
    try {
      const identity = await waitForWorkflow(getAssistantBatchRecoveryIdentity(saved.token));
      if (!current(saved) || context !== epoch.current || read !== identityEpoch.current) return;
      setOwnerRecord({ value: identity, session: saved });
      if (typeof saved.loginEpoch !== "string" || saved.loginEpoch.length < 16) throw new TypeError("Login epoch unavailable");
      const raw = globalThis.sessionStorage.getItem(STORAGE);
      if (raw !== null) {
        if (raw.length > 32768) throw new TypeError("Invalid recovery locator");
        const stored = validateWorkflowRecovery(JSON.parse(raw));
        if (!workflowRecoveryMatches(stored, identity, saved, navigationEpoch())) {
          forget(); pendingRef.current = false; setPending(false);
        } else {
          const restored = { ...stored, generation: saved.generation, navigation_epoch: navigationEpoch() };
          remember(restored, saved); markPending();
        }
      } else if (recoveryRef.current && isAuthSessionCurrent(recoveryRef.current.session)) markPending();
      setGate(false);
    } catch (failure) { if (current(saved) && context === epoch.current && read === identityEpoch.current) { markPending(); setError(workflowError(failure)); } }
  };
  const chooseWorkflow = async (reference) => {
    if (!canAct() || operation.current || pendingRef.current || frozenRef.current || gate || sourceLoading || source) return;
    const seq = ++selection.current, context = epoch.current, saved = session;
    setDefinitionRecord(null); setSlots({}); setScopeMode(null); setTargetIds([]); setPolicies({}); setPerTargetPolicy(false); setConfirmPrepare(false); setError("");
    try {
      const value = await waitForWorkflow(getAssistantWorkflow(reference.workflow_id, reference.workflow_version, { authSession: saved }));
      if (!current(saved) || seq !== selection.current || context !== epoch.current) return;
      if (!sameWorkflow(value, reference)) throw Object.assign(new Error("Definition changed"), { status: 409 });
      setDefinitionRecord({ value, session: saved }); setSlots(workflowDefaults(value.slots_schema));
    } catch (failure) { if (current(saved) && seq === selection.current && context === epoch.current) setError(workflowError(failure)); }
  };
  const suggestWorkflow = async () => {
    if (!canAct() || operation.current || pendingRef.current || frozenRef.current || gate || source || sourceLoading || !matchText.trim()) return;
    const op = { session, epoch: epoch.current }; operation.current = op; setBusy("match"); setError(""); setSuggestion(null);
    try {
      const value = await waitForWorkflow(matchAssistantWorkflow(matchText, { authSession: op.session }));
      if (current(op.session) && op.epoch === epoch.current && operation.current === op) setSuggestion({ value, session: op.session });
    } catch (failure) { if (current(op.session) && op.epoch === epoch.current) setError(workflowError(failure)); }
    finally { if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); } }
  };
  const mutate = async (kind) => {
    if (!canAct() || !validAction || operation.current || gate || pendingRef.current || !owner || (kind !== "enqueue" && sourceRequired && !source)) return;
    if ((kind === "prepare" || kind === "revise") && (!payload || !definition || !confirmPrepare || frozenRef.current || sourceLoading)) return;
    if (kind === "prepare" && (actionMode === "revise" || (source && actionMode === "rerun" && !source.rerunnable))) return;
    if (kind === "revise" && (!revisable || actionMode !== "revise" || (payload.scope.mode === "explicit" && payload.scope.targets.length !== 1))) return;
    if (kind === "enqueue" && (!canEnqueue || !confirmEnqueue || !planView.items.every((item) => workflowReady(item)))) return;
    if (!["prepare", "enqueue", "revise"].includes(kind)) return;
    const op = { kind, session, epoch: epoch.current, submitted: false }; operation.current = op; setBusy(kind); setError("");
    const valid = () => current(op.session) && op.epoch === epoch.current && operation.current === op;
    try {
      let value;
      if (kind === "revise") {
        const candidateHash = await workflowCandidateHash(definition, payload.slots, payload.scope);
        if (!valid()) return;
        const now = Date.now(), locator = { schema: "capability.workflow_recovery/v1", owner, login_epoch: op.session.loginEpoch, generation: op.session.generation,
          navigation_epoch: navigationEpoch(), created_at: now, expires_at: now + 86400000, phase: "revise", workflow: source.workflow,
          operation: "prepare", source_plan_id: null, target_ids: [payload.scope.mode === "global" ? null : payload.scope.targets[0].instance_id],
          keys: { prepare: null, enqueue: null }, preparation_id: null, bindings: null, batch_id: null,
          revision: { plan_id: source.summary.plan_id, expected_revision: source.summary.revision, candidate_hash: candidateHash } };
        remember(locator, op.session); markPending();
        const promise = reviseAssistantWorkflow(source, { expected_revision: source.summary.revision, slots: payload.slots, scope: payload.scope }, { authSession: op.session });
        op.submitted = true; value = await waitForWorkflow(promise);
        if (!valid()) return;
        remember({ ...locator, bindings: workflowBindings([value.summary]) }, op.session);
        frozenRef.current = { value, session: op.session }; setRevisionRecord(frozenRef.current); setReviews({}); setConfirmPrepare(false);
      } else if (kind === "prepare") {
        validateWorkflowInput(definition, payload);
        const now = Date.now();
        const locator = { schema: "capability.workflow_recovery/v1", owner, login_epoch: op.session.loginEpoch, generation: op.session.generation,
          navigation_epoch: navigationEpoch(), created_at: now, expires_at: now + 86400000, phase: "prepare", workflow: workflowReference(definition),
          operation: payload.operation, source_plan_id: payload.source_plan_id ?? null, target_ids: payload.scope.mode === "global" ? [null] : payload.scope.targets.map((item) => item.instance_id),
          keys: { prepare: newWorkflowKey("prepare"), enqueue: null }, preparation_id: null, bindings: null, batch_id: null };
        remember(locator, op.session); markPending();
        const promise = prepareAssistantWorkflow(definition, payload, { idempotencyKey: locator.keys.prepare, authSession: op.session });
        op.submitted = true; value = await waitForWorkflow(promise);
        if (!valid()) return;
        remember({ ...locator, preparation_id: value.preparation_id, bindings: workflowBindings(value.items) }, op.session);
        storePrepared(value, op.session);
      } else {
        const locator = { ...recoveryRef.current.value, phase: "enqueue", keys: { ...recoveryRef.current.value.keys, enqueue: newWorkflowKey("enqueue") } };
        remember(locator, op.session); markPending();
        const promise = enqueueAssistantBatch({ plan_ids: planView.items.map((item) => item.plan_id) }, { idempotencyKey: locator.keys.enqueue, authSession: op.session });
        op.submitted = true; value = await waitForWorkflow(promise);
        if (!valid()) return;
        if (!value.items.every((item, index) => item.bound_revision === planView.items[index].revision && item.binding_hash === planView.items[index].binding_hash && item.binding_current)) throw Object.assign(new Error("Batch binding mismatch"), { code: "assistant_workflow_response_invalid" });
        remember({ ...locator, batch_id: value.batch_id }, op.session); setBatchRecord({ value, session: op.session });
      }
      pendingRef.current = false; setPending(false); setConfirmEnqueue(false);
    } catch (failure) { if (valid()) { if (op.submitted || recoveryRef.current) markPending(); setError(workflowError(failure)); } }
    finally { if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); } }
  };
  const recoverOriginal = async () => {
    if (!canAct() || operation.current || !owner) return;
    const savedLocator = recoveryRef.current && isAuthSessionCurrent(recoveryRef.current.session) ? recoveryRef.current.value : null;
    if (!savedLocator && !manualKey) return;
    const op = { session, epoch: epoch.current }; operation.current = op; setBusy("recover"); setError("");
    const valid = () => current(op.session) && op.epoch === epoch.current && operation.current === op;
    try {
      if (savedLocator?.phase === "revise") {
        const value = await waitForWorkflow(getAssistantWorkflowRevision(savedLocator.revision, { authSession: op.session }));
        if (!valid()) return;
        if (!sameWorkflow(value.workflow, savedLocator.workflow) || value.summary.instance_id !== savedLocator.target_ids[0]) throw Object.assign(new Error("Revision mismatch"), { code: "assistant_workflow_response_invalid" });
        remember({ ...savedLocator, bindings: workflowBindings([value.summary]) }, op.session);
        frozenRef.current = { value, session: op.session }; setRevisionRecord(frozenRef.current); setReviews({});
      } else if (savedLocator?.phase === "enqueue") {
        const value = await waitForWorkflow(getAssistantBatchAdmission({ idempotencyKey: savedLocator.keys.enqueue,
          payload: { plan_ids: savedLocator.bindings.map((item) => item.plan_id) }, bindings: savedLocator.bindings, ownerId: owner.user_id, batchId: savedLocator.batch_id }));
        if (!valid()) return;
        remember({ ...savedLocator, batch_id: value.batch_id }, op.session); setBatchRecord({ value, session: op.session });
      } else {
        const value = await waitForWorkflow(getAssistantWorkflowPreparation({ idempotencyKey: savedLocator?.keys.prepare || manualKey, expected: savedLocator || undefined, authSession: op.session }));
        if (!valid()) return;
        const now = Date.now();
        const locator = savedLocator || { schema: "capability.workflow_recovery/v1", owner, login_epoch: op.session.loginEpoch, generation: op.session.generation,
          navigation_epoch: navigationEpoch(), created_at: now, expires_at: now + 86400000, phase: "prepare", workflow: value.workflow,
          operation: value.operation, source_plan_id: value.source_plan_id, target_ids: value.items.map((item) => item.instance_id),
          keys: { prepare: manualKey, enqueue: null }, preparation_id: null, bindings: null, batch_id: null };
        remember({ ...locator, preparation_id: value.preparation_id, bindings: workflowBindings(value.items) }, op.session); storePrepared(value, op.session);
      }
      pendingRef.current = false; setPending(false); setGate(false);
    } catch (failure) { if (valid()) { markPending(); setError(workflowError(failure)); } }
    finally { if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); } }
  };
  const refreshPlans = async () => {
    if (!canAct() || operation.current || pendingRef.current || !planView) return;
    const op = { session, epoch: epoch.current }; operation.current = op; setBusy("plans"); setConfirmEnqueue(false); setReviews({});
    const valid = () => current(op.session) && op.epoch === epoch.current && operation.current === op;
    try {
      const refreshed = [];
      for (let offset = 0; offset < planView.items.length; offset += 4) {
        if (!valid()) return;
        const values = await Promise.all(planView.items.slice(offset, offset + 4).map(async (item) => {
          try { return { item: await waitForWorkflow(getAssistantWorkflowPlan(item, planView.workflow, { authSession: op.session })) }; }
          catch (failure) { return { item, error: workflowError(failure) }; }
        }));
        refreshed.push(...values);
      }
      if (!valid()) return;
      if (prepared) { frozenRef.current = { value: { ...prepared, items: refreshed.map((value) => value.item) }, session: op.session }; setPreparedRecord(frozenRef.current); }
      else { frozenRef.current = { value: { ...revised, summary: refreshed[0].item }, session: op.session }; setRevisionRecord(frozenRef.current); }
      setReviews(Object.fromEntries(refreshed.map((value) => [value.item.plan_id, { checked: !value.error, message: value.error || "已回读当前修订与审批" }])));
    } finally { if (operation.current === op) { operation.current = null; if (current(op.session)) setBusy(""); } }
  };
  const refreshBatch = async () => {
    if (!canAct() || operation.current || !batch) return;
    const saved = session, context = epoch.current, read = ++batchEpoch.current;
    try { const value = await waitForWorkflow(getAssistantBatch(batch.batch_id)); if (current(saved) && context === epoch.current && read === batchEpoch.current) setBatchRecord({ value, session: saved }); }
    catch (failure) { if (current(saved) && context === epoch.current && read === batchEpoch.current) setError(workflowError(failure)); }
  };
  const newDraft = () => {
    if (!canAct() || operation.current || pendingRef.current || !confirmNewDraft) return;
    try { forget(); } catch { setError(workflowError({ code: "assistant_workflow_storage_unavailable" })); return; }
    epoch.current += 1; selection.current += 1; frozenRef.current = null; setPreparedRecord(null); setBatchRecord(null); setDefinitionRecord(null);
    setSlots({}); setScopeMode(null); setTargetIds([]); setPolicies({}); setReviews({}); setSuggestion(null); setOperationKind("prepare"); setSourcePlanId("");
    setSourceRecord(null); setRevisionRecord(null); setActionMode("prepare"); setSourceRequired(false);
    setConfirmPrepare(false); setConfirmEnqueue(false); setConfirmNewDraft(false); setError("");
  };
  useEffect(() => {
    alive.current = true;
    const notify = () => setAuthTick((value) => value + 1);
    globalThis.addEventListener?.("storage", notify); const stopAuth = onAuthExpired(notify);
    return () => { alive.current = false; visible.current = false; epoch.current += 1; selection.current += 1;
      globalThis.removeEventListener?.("storage", notify); stopAuth(); };
  }, []);
  useEffect(() => {
    epoch.current += 1; selection.current += 1; operation.current = null; setBusy(""); setConfirmPrepare(false); setConfirmEnqueue(false); setConfirmNewDraft(false); setReviews({});
    frozenRef.current = null; setPreparedRecord(null); setRevisionRecord(null); setBatchRecord(null); setGate(true);
    setSourceRecord(null); setDefinitionRecord(null); setSourceLoading(false); setSourceRequired(Boolean(initialPlan)); setError("");
    setSlots({}); setScopeMode(null); setTargetIds([]); setPolicies({}); setPerTargetPolicy(false); setOperationKind("prepare"); setSourcePlanId(""); setSuggestion(null);
    if (!isAuthSessionCurrent(dataSession.current)) {
      dataSession.current = session; recoveryRef.current = null; frozenRef.current = null; pendingRef.current = false;
      setRecoveryRecord(null); setPreparedRecord(null); setBatchRecord(null); setCatalogRecord(null); setDefinitionRecord(null); setOwnerRecord(null);
      setSourceRecord(null); setRevisionRecord(null);
      setSlots({}); setScopeMode(null); setTargetIds([]); setPolicies({}); setSourcePlanId(""); setMatchText(""); setSuggestion(null); setPending(false); setManualKey(""); setError("");
    }
    if (open) {
      loadCatalog(); loadIdentity();
      setActionMode(initialAction);
      if (!["prepare", "revise", "rerun"].includes(initialAction) || (!initialPlan && initialAction !== "prepare")) setError("流程入口缺少有效动作或原计划，请重新打开。");
      if (initialPlan && ["prepare", "revise", "rerun"].includes(initialAction)) {
        const saved = session, context = epoch.current; setSourceLoading(true); setSourceRecord(null); setDefinitionRecord(null);
        waitForWorkflow(getAssistantWorkflowSource(initialPlan, { authSession: saved })).then((value) => {
          if (!current(saved) || context !== epoch.current) return;
          setSourceRecord({ value, session: saved }); setDefinitionRecord({ value: value.definition, session: saved });
          setSlots(value.slots); setScopeMode(value.scope.mode); setTargetIds(value.scope.mode === "global" ? [] : value.scope.targets.map((target) => target.instance_id));
          setPerTargetPolicy(false); setPolicies({}); setOperationKind(initialAction === "rerun" ? "rerun" : "prepare"); setSourcePlanId(initialAction === "rerun" ? value.summary.plan_id : "");
          if (initialAction === "rerun" && !value.rerunnable) setError("原计划未安全终结，不能重跑；请在原计划核对。");
        }).catch((failure) => { if (current(saved) && context === epoch.current) setError(workflowError(failure)); })
          .finally(() => { if (current(saved) && context === epoch.current) setSourceLoading(false); });
      } else setSourceLoading(false);
    }
    return () => { epoch.current += 1; selection.current += 1; };
  }, [open, session.token, session.generation, session.loginEpoch, typeof initialPlan === "string" ? initialPlan : initialPlan?.plan_id, initialPlan?.revision, initialPlan?.binding_hash, initialAction]);
  useEffect(() => {
    if (previousTargets.current === targetStamp) return;
    previousTargets.current = targetStamp; setConfirmPrepare(false); setConfirmEnqueue(false); setReviews({});
  }, [targetStamp]);

  if (!open) return null;
  const match = sameRecord(suggestion);
  return <Modal open centered width={1120} title="固定流程库" className="assistant-workflow-modal" onCancel={closeWorkspace} footer={<Button onClick={closeWorkspace}>关闭</Button>}>
    <div className="assistant-workflow-workspace">
      {error && <Alert type="error" showIcon message={error} />}
      {(pending || recovery || gate) && <section className="assistant-workflow-recovery">
        {pending && <Alert type="warning" showIcon message="原请求待核对，不会自动重发" />}
        {recovery && <Descriptions size="small" column={1} items={[{ key: "phase", label: "原请求", children: recovery.phase === "enqueue" ? "批次准入" : recovery.phase === "revise" ? "固定流程修订" : "冻结计划" }, { key: "key", label: recovery.phase === "revise" ? "计划 / 原修订" : "Idempotency-Key", children: <code>{recovery.phase === "revise" ? `${recovery.revision.plan_id} / r${recovery.revision.expected_revision}` : recovery.keys[recovery.phase === "enqueue" ? "enqueue" : "prepare"]}</code> }]} />}
        {!recovery && <Input aria-label="原冻结请求 key" value={manualKey} onChange={(event) => setManualKey(event.target.value)} placeholder="原冻结请求 Idempotency-Key" maxLength={128} />}
        <Space wrap><Button icon={<ReloadOutlined />} loading={busy === "recover"} disabled={Boolean(busy) || !owner || (!recovery && !manualKey)} onClick={recoverOriginal}>{recovery?.phase === "revise" ? "GET 核对原计划修订" : "原 key 只读核对"}</Button>
          <Button icon={<ReloadOutlined />} disabled={Boolean(busy)} onClick={loadIdentity}>重新核对会话</Button></Space>
      </section>}
      {!planView && !batch && <div className="assistant-workflow-layout">
        <aside className="assistant-workflow-library"><div className="assistant-workflow-heading"><Typography.Title level={5}>流程目录</Typography.Title><Tooltip title="刷新授权目录"><Button aria-label="刷新授权目录" icon={<ReloadOutlined />} onClick={loadCatalog} disabled={Boolean(busy)} /></Tooltip></div>
          {catalogError && <Alert type="error" message={catalogError} />}
          {!catalog && !catalogError && <Typography.Text type="secondary">读取授权目录中</Typography.Text>}
          {catalog?.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有当前可见流程" />}
          {catalog?.map((item) => <Button key={`${item.workflow_id}:${item.workflow_version}`} type={definition?.workflow_id === item.workflow_id ? "primary" : "text"} disabled={locked || Boolean(source)} onClick={() => chooseWorkflow(item)} className="assistant-workflow-recipe"><span>{WORKFLOW_NAMES[item.workflow_id]}</span><small>v{item.workflow_version} · {item.steps.length} 步</small></Button>)}
          <div className="assistant-workflow-suggestion"><Input aria-label="流程建议文本" maxLength={4000} value={matchText} disabled={locked || Boolean(source)} onChange={(event) => { setMatchText(event.target.value); setSuggestion(null); }} placeholder="明确的流程意图" />
            <Button icon={<FileSearchOutlined />} disabled={locked || Boolean(source) || !matchText.trim()} loading={busy === "match"} onClick={suggestWorkflow}>查找建议</Button>
            {match && <><Tag>{match.decision}</Tag><Typography.Text type="secondary">{match.reason_code}</Typography.Text>{match.missing_fields.length > 0 && <span>待补：{match.missing_fields.join("、")}</span>}
              {match.workflow && match.decision === "matched" && ["prepare", "rerun"].includes(match.operation) && catalog?.some((item) => sameWorkflow(item, match.workflow)) && <Button disabled={locked} onClick={() => { setOperationKind(match.operation); chooseWorkflow(match.workflow); }}>采用建议，继续填参</Button>}</>}
          </div>
          <AssistantProposalPanel session={session} instances={instances} catalog={catalog || []}
            disabled={locked || Boolean(planView) || Boolean(source)} onAdopt={adoptProposal} />
        </aside>
        <section className="assistant-workflow-draft">
          {!definition ? <Empty description={sourceLoading ? "读取并核验原流程计划" : "选择固定版本流程"} /> : <>
            <div className="assistant-workflow-heading"><Typography.Title level={5}>{WORKFLOW_NAMES[definition.workflow_id]}</Typography.Title><Tag>v{definition.workflow_version}</Tag>{definition.request_approval && <Tag color="warning">需审批</Tag>}</div>
            <code className="assistant-workflow-hash">{definition.definition_hash}</code>
            <ol className="assistant-workflow-steps">{definition.steps.map((step) => <li key={step.seq}><span>{step.capability_id}@{step.definition_version}</span><small>{step.seq > 1 ? `依赖步骤 ${step.depends_on.join("、")}` : "起始步骤"}</small></li>)}</ol>
            {definition.release_gate && <Alert type="warning" showIcon message="采集发布开关与当前权限由服务端复核" />}
            {source ? <Alert type="info" message={`${actionMode === "revise" ? "固定流程修订" : actionMode === "rerun" ? "同业务输入重新准备" : "基于原流程的新草稿"} · ${source.summary.plan_id} · r${source.summary.revision}`} /> : <Radio.Group aria-label="流程操作" value={operationKind} onChange={(event) => editDraft(() => { setOperationKind(event.target.value); setSourcePlanId(""); })} disabled={locked} options={[{ value: "prepare", label: "新建准备" }, { value: "rerun", label: "明确重新运行" }]} />}
            {operationKind === "rerun" && <Input aria-label="原流程计划 ID" value={sourcePlanId} maxLength={28} placeholder="明确的 source_plan_id" disabled={locked || Boolean(source)} onChange={(event) => editDraft(() => setSourcePlanId(event.target.value))} />}
            {definition.workflow_id === "inspection.collect" && <Checkbox checked={perTargetPolicy} disabled={locked || sourceLocked} onChange={(event) => editDraft(() => { setPerTargetPolicy(event.target.checked); setPolicies({}); })}>逐目标填写模板 ID</Checkbox>}
            <SlotFields schema={perTargetPolicy ? { ...definition.slots_schema, properties: Object.fromEntries(Object.entries(definition.slots_schema.properties).filter(([name]) => name !== "policy_id")), required: [] } : definition.slots_schema} value={slots} disabled={locked || sourceLocked} onChange={(next) => { if (!sourceLocked) editDraft(() => setSlots(next)); }} />
            <fieldset className="assistant-workflow-group"><legend>明确范围</legend><Radio.Group aria-label="目标范围" value={scopeMode} disabled={locked || sourceLocked} onChange={(event) => editDraft(() => { setScopeMode(event.target.value); setTargetIds([]); setPolicies({}); })} options={definition.scope_modes.map((mode) => ({ value: mode, label: mode === "global" ? "全局读取（当前授权范围）" : "明确实例" }))} />
              {scopeMode === "explicit" && <Select aria-label="明确目标实例" mode="multiple" value={targetIds} maxCount={actionMode === "revise" ? 1 : 100} disabled={locked || sourceLocked} onChange={(ids) => editDraft(() => setTargetIds(ids))} placeholder="未选择目标" options={instances.filter((item) => Number.isSafeInteger(item.id) && item.id > 0).map((item) => ({ value: item.id, label: `${item.instance_name || item.name || item.id} · ${item.db_type} · #${item.id}` }))} />}
              {scopeMode === "global" && <Tag>全局读取，不采集</Tag>}
              {perTargetPolicy && scopeMode === "explicit" && targetIds.map((id) => <label className="assistant-workflow-target-policy" key={id}>#{id} 模板 ID<InputNumber aria-label={`目标 ${id} 模板 ID`} min={1} precision={0} value={policies[id]} disabled={locked || sourceLocked} onChange={(value) => editDraft(() => setPolicies((old) => ({ ...old, [id]: value })))} /></label>)}
            </fieldset>
            <div className="assistant-workflow-consent"><Checkbox checked={confirmPrepare} disabled={locked || !payload || (actionMode === "revise" && !revisable)} onChange={(event) => setConfirmPrepare(event.target.checked)}>{actionMode === "revise" ? "确认保存下一修订；原审批失效，需重新复核" : "确认固定版本、参数及所列范围；冻结后尚不执行"}</Checkbox>
              <Button type="primary" icon={<SaveOutlined />} disabled={locked || !payload || !confirmPrepare || (actionMode === "revise" && !revisable) || (source && actionMode === "rerun" && !source.rerunnable)} loading={busy === "prepare" || busy === "revise"} onClick={() => mutate(actionMode === "revise" ? "revise" : "prepare")}>{actionMode === "revise" ? "保存固定流程修订" : "冻结计划"}</Button></div>
          </>}
        </section>
      </div>}
      {planView && <section className="assistant-workflow-frozen">
        <div className="assistant-workflow-heading"><Typography.Title level={5}>{revised ? "当前修订" : "冻结计划"} · {planView.total}</Typography.Title><Tag>{WORKFLOW_NAMES[planView.workflow.workflow_id]} v{planView.workflow.workflow_version}</Tag><Button icon={<ReloadOutlined />} loading={busy === "plans"} disabled={Boolean(busy) || pending} onClick={refreshPlans}>回读全部状态</Button></div>
        {prepared && <Typography.Text type="secondary">{prepared.preparation_id}</Typography.Text>}
        <Table rowKey="plan_id" size="small" dataSource={planView.items} scroll={{ x: 840 }} pagination={{ pageSize: 10 }} columns={[
          { title: "计划 / 目标", key: "plan", render: (_, item) => <><code>{item.plan_id}</code><div>{item.instance_id === null ? "全局读取" : `实例 #${item.instance_id}`}</div></> },
          { title: "修订", dataIndex: "revision", width: 72 }, { title: "状态", key: "status", render: (_, item) => <><Tag>{planLabels[item.status] || item.status}</Tag><span>{outcomeLabels[item.outcome] || item.outcome || "未执行"}</span></> },
          { title: "审批", dataIndex: "approval_state" }, { title: "当前回读", key: "review", render: (_, item) => <span>{reviews[item.plan_id]?.message || "待回读"}</span> },
          { title: "操作", key: "action", render: (_, item) => <Button icon={<FolderOpenOutlined />} disabled={Boolean(busy) || !onOpenPlan} onClick={() => { if (current(session)) onOpenPlan(item.plan_id); }}>打开计划 / 审批</Button> },
        ]} />
        {!recovery?.batch_id && <div className="assistant-workflow-consent"><Checkbox checked={confirmEnqueue} disabled={!canEnqueue} onChange={(event) => setConfirmEnqueue(event.target.checked)}>确认按当前冻结修订提交 {planView.total} 项批次</Checkbox><Button type="primary" icon={<SendOutlined />} loading={busy === "enqueue"} disabled={!canEnqueue || !confirmEnqueue} onClick={() => mutate("enqueue")}>提交批次准入</Button></div>}
      </section>}
      {batch && <section className="assistant-workflow-frozen"><div className="assistant-workflow-heading"><Typography.Title level={5}>批次状态</Typography.Title><Button icon={<ReloadOutlined />} disabled={Boolean(busy)} onClick={refreshBatch}>回读批次</Button></div>
        <code>{batch.batch_id}</code><Space wrap><Tag>{batch.status}</Tag><Tag color={batch.outcome === "succeeded" ? "success" : "default"}>{outcomeLabels[batch.outcome] || batch.outcome || "尚未完成"}</Tag>{batch.requires_attention && <Tag color="warning">待核对</Tag>}</Space>
        <Table rowKey="plan_id" size="small" scroll={{ x: 680 }} dataSource={batch.items} pagination={{ pageSize: 10 }} columns={[{ title: "计划", dataIndex: "plan_id" }, { title: "投递状态", dataIndex: "queue_state" }, { title: "计划结论", dataIndex: "outcome", render: (value) => outcomeLabels[value] || value || "未终结" }, { title: "操作", key: "open", render: (_, item) => <Button icon={<FolderOpenOutlined />} disabled={!onOpenPlan} onClick={() => { if (current(session)) onOpenPlan(item.plan_id); }}>打开原计划</Button> }]} />
      </section>}
      {(planView || batch || source) && !pending && <section className="assistant-workflow-new-draft"><Alert type="info" showIcon message="新草稿不会修改原计划或沿用其审批" /><Checkbox checked={confirmNewDraft} disabled={Boolean(busy)} onChange={(event) => setConfirmNewDraft(event.target.checked)}>保留原计划，另建独立草稿</Checkbox><Button icon={<PlusOutlined />} disabled={Boolean(busy) || !confirmNewDraft} onClick={newDraft}>新建独立草稿</Button></section>}
    </div>
  </Modal>;
}
