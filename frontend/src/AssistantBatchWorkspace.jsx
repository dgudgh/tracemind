import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Empty, Input, InputNumber, Modal, Progress, Select, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, FileSearchOutlined, PlusOutlined, ReloadOutlined, SaveOutlined, SendOutlined, StopOutlined } from "@ant-design/icons";
import { listAssistantCapabilities } from "./modules/assistantApi";
import { changeAssistantSteps } from "./modules/assistantPlanState";
import { AssistantBatchControlView } from "./AssistantBatchControlView";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { ASSISTANT_BATCH_READERS, isAssistantBatchId, prepareAssistantBatch, enqueueAssistantBatch, listAssistantBatches, cancelAssistantBatch, getAssistantBatchPlanSnapshot, getAssistantBatchPreparation, getAssistantBatchAdmission, getAssistantBatchRecoveryIdentity, validateAssistantBatchRecovery } from "./modules/assistantBatchApi";
import { getAssistantControlBatch } from "./modules/assistantControlApi";
import "./assistantBatch.css";

const NAMES = ASSISTANT_BATCH_READERS;
const PAGE_SIZE = 10;
const RECOVERY_STORAGE = "mesguard.assistant.batch-recovery.v1";
const navigationEpoch = () => globalThis.performance.timeOrigin;
const frozenBindings = (items) => items.map(({ plan_id, instance_id, revision, binding_hash }) => ({ plan_id, instance_id, revision, binding_hash }));
const queueLabels = { queued: "待投递", running: "在途", archived: "已归档", reconciliation_required: "待核对", cancelled: "已取消投递", expired: "投递已过期", blocked: "已阻止", paused: "已暂停", stopped: "已停止" };
const outcomeLabels = { succeeded: "批次成功", partial: "部分完成", failed: "失败", cancelled: "已取消投递", expired: "已过期", unknown: "结果未知", plan_only: "仅计划", blocked: "已阻止", unsupported: "不支持", needs_input: "待补参" };

function waitForBatch(promise) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error("Batch response deadline exceeded"), { code: "assistant_batch_timeout" })), 90000); });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function newBatchKey(kind) {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `${kind}:${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function batchError(error) {
  if (error?.code === "batch_control_requires_v2") return "此批次需要 v2 控制视图核对，保持 unknown；不会自动重放或在 v1 伪造暂停状态。";
  if (error?.code === "assistant_batch_storage_unavailable") return "恢复定位上下文无法安全保存，保持待核对；不会换 key 或重发。";
  if (error?.code === "assistant_batch_response_invalid") return "响应结构或冻结绑定不符合合同，请核对服务端状态。";
  if (error?.code === "assistant_batch_timeout") return "等待响应超时；未确认服务端停止，不会自动重发。";
  if (error instanceof TypeError) return "输入或连接未获确认，请核对明确目标、类型化参数与服务状态。";
  if ([401, 403].includes(error?.status)) return "当前会话或权限未通过服务端校验，请重新鉴权后查询。";
  if ([404, 409, 422].includes(error?.status)) return "服务端拒绝当前状态、冻结绑定或参数，请回读核对后处理。";
  return "请求未获确认，请通过历史和详情核对；不会自动重发。";
}

function uncertainError(error) {
  return !Number.isInteger(error?.status) || error.status >= 500 || error.code === "assistant_batch_response_invalid"
    || error.code === "batch_control_requires_v2";
}

function wired(item, targets) {
  return Boolean(item && Object.hasOwn(NAMES, item.capability_id) && item.version === 1 && item.definition_version === 1
    && item.plan_executable === true && item.execution === "server" && item.availability === "available"
    && Array.isArray(item.authorized_engines) && targets.every((target) => item.authorized_engines.includes(target.db_type)));
}

function compileBatchSteps(steps) {
  return steps.map((step, index) => ({ seq: index + 1, capability_id: step.capability_id, definition_version: 1,
    title: NAMES[step.capability_id], params: { ...step.params },
    depends_on: step.depends_on.map((key) => {
      const position = steps.findIndex((prior) => prior.key === key);
      if (position < 0 || position >= index) throw new TypeError("Invalid dependency");
      return position + 1;
    }), input_refs: {}, fanout: 1, retries: 0 }));
}

function readyForAdmission(item, now = Date.now()) {
  return item.outcome === null && new Date(item.expires_at).getTime() > now
    && ((item.status === "validated" && item.approval_state === "not_required")
      || (item.status === "approved" && item.approval_state === "approved" && item.approval_available === true));
}

function sameBatchBinding(value, prepared) {
  return Boolean(prepared && value.total === prepared.items.length && value.items.every((member, index) => {
    const frozen = prepared.items[index];
    return member.plan_id === frozen.plan_id && member.bound_revision === frozen.revision
      && member.binding_hash === frozen.binding_hash && member.binding_current === true;
  }));
}

function batchView(value) {
  if (value.status === "settled") return { label: outcomeLabels[value.outcome] || "结果待核对", color: value.outcome === "succeeded" ? "success" : "warning" };
  return { label: { queued: "已入队，尚未完成", running: "运行中", reconciliation_required: "待人工核对" }[value.status] || "待核对",
    color: value.status === "reconciliation_required" ? "warning" : "processing" };
}

const stamp = (value) => typeof value === "string" ? `${value.slice(0, 19).replace("T", " ")} UTC` : "未提供";

function batchMemberTarget(item, instances) {
  if (!["instance_id", "db_type", "capability_id"].every((key) => Object.hasOwn(item || {}, key))) return { name: "待回读", engine: "" };
  const instance = item.instance_id === null ? null : instances.find((candidate) => candidate.id === item.instance_id);
  const name = item.instance_id === null ? (item.db_type === "host" ? "主机范围" : "全局目标")
    : instance?.instance_name || instance?.name || `实例 ${item.instance_id}`;
  const engine = item.db_type === null ? "未指定引擎" : item.db_type === "host" ? "主机" : item.db_type.toUpperCase();
  return { name, engine };
}

export function AssistantBatchWorkspace({ open, onClose, instances = [], onOpenPlan, initialBatchId }) {
  const session = getAuthSession();
  const [tab, setTab] = useState("new");
  const [targetIds, setTargetIds] = useState([]);
  const [steps, setSteps] = useState([{ key: "step-1", capability_id: "inspection.run.query", params: { page: 1, page_size: 20 }, depends_on: [] }]);
  const [ttl, setTtl] = useState(3600);
  const [deadline, setDeadline] = useState(3600);
  const [requestApproval, setRequestApproval] = useState(false);
  const [confirmPrepare, setConfirmPrepare] = useState(false);
  const [confirmEnqueue, setConfirmEnqueue] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [catalogRecord, setCatalogRecord] = useState(null);
  const [preparedRecord, setPreparedRecord] = useState(null);
  const [batchRecord, setBatchRecord] = useState(null);
  const [historyRecord, setHistoryRecord] = useState(null);
  const [reviews, setReviews] = useState({});
  const [batchIdInput, setBatchIdInput] = useState("");
  const [busy, setBusy] = useState("");
  const [reading, setReading] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [rowLoading, setRowLoading] = useState({});
  const [error, setError] = useState("");
  const [catalogError, setCatalogError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [uncertainty, setUncertainty] = useState(null);
  const [ownerRecord, setOwnerRecord] = useState(null);
  const [recoveryRecord, setRecoveryRecord] = useState(null);
  const [recoveryChecking, setRecoveryChecking] = useState(false);
  const [recoveryGate, setRecoveryGate] = useState(true);
  const [manualPrepareKey, setManualPrepareKey] = useState("");
  const [manualEnqueueKey, setManualEnqueueKey] = useState("");
  const [manualReceipt, setManualReceipt] = useState(null);
  const alive = useRef(true);
  const visible = useRef(open);
  visible.current = open;
  const operation = useRef(null);
  const uncertainRef = useRef(null);
  const contextEpoch = useRef(0);
  const detailEpoch = useRef(0);
  const catalogEpoch = useRef(0);
  const historyEpoch = useRef(0);
  const rowEpoch = useRef(0);
  const rowsPending = useRef(new Map());
  const keyPair = useRef({ prepare: null, enqueue: null });
  const sequence = useRef(1);
  const dataSession = useRef(session);
  const recoveryRef = useRef(null);
  const identityEpoch = useRef(0);
  const hydratedIdentity = useRef(null);
  const selectedTargets = targetIds.map((id) => instances.find((item) => item.id === id)).filter(Boolean);
  const targetStamp = JSON.stringify(targetIds.map((id) => {
    const value = instances.find((item) => item.id === id);
    return [id, value?.db_type, value?.host, value?.port, value?.instance_name || value?.name];
  }));
  const previousTargets = useRef(targetStamp);
  const current = (savedSession) => alive.current && visible.current && isAuthSessionCurrent(savedSession);
  const catalog = catalogRecord && isAuthSessionCurrent(catalogRecord.session) ? catalogRecord.items : [];
  const prepared = preparedRecord && isAuthSessionCurrent(preparedRecord.session) ? preparedRecord.value : null;
  const admitted = Boolean(prepared && preparedRecord.batchId);
  const batch = batchRecord && isAuthSessionCurrent(batchRecord.session) ? batchRecord.value : null;
  const history = historyRecord && isAuthSessionCurrent(historyRecord.session) ? historyRecord.value : { items: [], page: 1, total: 0 };
  const unknown = uncertainty && isAuthSessionCurrent(uncertainty.session) ? uncertainty : null;
  const owner = ownerRecord && isAuthSessionCurrent(ownerRecord.session) ? ownerRecord.value : null;
  const recovery = recoveryRecord && isAuthSessionCurrent(recoveryRecord.session) ? recoveryRecord.value : null;
  const recoveryUsable = Boolean(recovery && owner && !recoveryGate && recovery.expires_at > Date.now());
  const manual = manualReceipt && isAuthSessionCurrent(manualReceipt.session) ? manualReceipt : null;
  const draftLocked = Boolean(busy || unknown || recoveryGate || !owner);
  const ready = targetIds.length >= 1 && targetIds.length <= 100 && selectedTargets.length === targetIds.length
    && steps.every((step) => wired(catalog.find((item) => item.capability_id === step.capability_id), selectedTargets));
  const canEnqueue = Boolean(prepared && !admitted && !unknown && !recoveryGate && owner && !busy && !reading && !Object.values(rowLoading).some(Boolean)
    && prepared.items.every((item) => readyForAdmission(item) && !reviews[item.plan_id]?.conflict));

  const invalidateReads = () => {
    contextEpoch.current += 1;
    detailEpoch.current += 1;
    rowEpoch.current += 1;
    rowsPending.current.clear();
    setReading(false);
    setRowLoading({});
  };
  const markUncertain = (op) => {
    if (!alive.current || !isAuthSessionCurrent(op.session)) return;
    const value = { kind: op.kind, batchId: op.batchId, session: op.session };
    uncertainRef.current = value;
    setUncertainty(value);
  };
  const writeRecovery = (value, savedSession = getAuthSession()) => {
    validateAssistantBatchRecovery(value);
    if (!isAuthSessionCurrent(savedSession)) throw new TypeError("Stale session");
    recoveryRef.current = { value, session: savedSession };
    setRecoveryRecord(recoveryRef.current);
    try { globalThis.sessionStorage.setItem(RECOVERY_STORAGE, JSON.stringify(value)); }
    catch { throw Object.assign(new Error("Recovery storage unavailable"), { code: "assistant_batch_storage_unavailable" }); }
  };
  const clearRecovery = () => {
    try { globalThis.sessionStorage.removeItem(RECOVERY_STORAGE); }
    catch { throw Object.assign(new Error("Recovery storage unavailable"), { code: "assistant_batch_storage_unavailable" }); }
    recoveryRef.current = null; setRecoveryRecord(null);
  };
  const closeWorkspace = () => {
    visible.current = false;
    invalidateReads();
    catalogEpoch.current += 1;
    historyEpoch.current += 1;
    identityEpoch.current += 1;
    if (operation.current?.submitted) markUncertain(operation.current);
    onClose();
  };
  const changeTab = (key) => {
    if (operation.current) return;
    invalidateReads();
    setTab(key);
  };
  const editDraft = (change) => {
    if (operation.current || uncertainRef.current || recoveryGate || !owner) return;
    try { clearRecovery(); } catch (failure) { markUncertain({ kind: "prepare", batchId: null, session: getAuthSession() }); setError(batchError(failure)); return; }
    invalidateReads();
    change();
    keyPair.current = { prepare: null, enqueue: null };
    setPreparedRecord(null);
    setBatchRecord(null);
    setReviews({});
    setConfirmPrepare(false);
    setConfirmEnqueue(false);
    setError("");
  };
  const editSteps = (action) => {
    try { const next = changeAssistantSteps(steps, action); editDraft(() => setSteps(next)); }
    catch { setError("请先解除前置依赖，再调整步骤；清单最多 20 步且至少保留一步。"); }
  };
  const newDraft = () => {
    editDraft(() => { setTargetIds([]); setConfirmCancel(false); setBatchIdInput(""); setTab("new"); });
  };

  const loadCatalog = async () => {
    const epoch = ++catalogEpoch.current, savedSession = getAuthSession();
    setCatalogLoading(true); setCatalogError("");
    try {
      const value = await waitForBatch(listAssistantCapabilities());
      if (current(savedSession) && epoch === catalogEpoch.current) setCatalogRecord({ items: value.items.filter((item) => wired(item, [])), session: savedSession });
    } catch {
      if (current(savedSession) && epoch === catalogEpoch.current) { setCatalogRecord(null); setCatalogError("能力目录读取失败；不能冻结或提交新批次。"); }
    } finally { if (current(savedSession) && epoch === catalogEpoch.current) setCatalogLoading(false); }
  };
  const loadHistory = async (page = 1) => {
    const epoch = ++historyEpoch.current, savedSession = getAuthSession();
    setHistoryLoading(true); setHistoryError("");
    try {
      const value = await waitForBatch(listAssistantBatches({ page, page_size: PAGE_SIZE }));
      if (current(savedSession) && epoch === historyEpoch.current) setHistoryRecord({ value, session: savedSession });
    } catch {
      if (current(savedSession) && epoch === historyEpoch.current) setHistoryError("批次历史读取失败，未推断为空或已结束。");
    } finally { if (current(savedSession) && epoch === historyEpoch.current) setHistoryLoading(false); }
  };
  const loadIdentity = async () => {
    const epoch = ++identityEpoch.current, savedSession = getAuthSession();
    setRecoveryGate(true); setRecoveryChecking(true);
    const valid = () => current(savedSession) && epoch === identityEpoch.current;
    try {
      const value = await waitForBatch(getAssistantBatchRecoveryIdentity(savedSession.token));
      if (!valid()) return;
      setOwnerRecord({ value, session: savedSession });
      if (typeof savedSession.loginEpoch !== "string" || savedSession.loginEpoch.length < 16) throw new TypeError("Persistent login epoch unavailable");
      if (hydratedIdentity.current && isAuthSessionCurrent(hydratedIdentity.current.session)
        && hydratedIdentity.current.value.user_id === value.user_id && hydratedIdentity.current.value.session_fingerprint === value.session_fingerprint) {
        setRecoveryGate(false); return;
      }
      hydratedIdentity.current = { value, session: savedSession };
      const raw = globalThis.sessionStorage.getItem(RECOVERY_STORAGE);
      if (raw === null) { setRecoveryGate(false); return; }
      if (typeof raw !== "string" || raw.length > 131072) throw new TypeError("Invalid recovery storage");
      const stored = validateAssistantBatchRecovery(JSON.parse(raw));
      const sameOwner = stored.owner.user_id === value.user_id && stored.owner.session_fingerprint === value.session_fingerprint && stored.login_epoch === savedSession.loginEpoch;
      const sameGeneration = stored.navigation_epoch === navigationEpoch()
        ? stored.generation === savedSession.generation : stored.login_epoch === savedSession.loginEpoch;
      if (!sameOwner || !sameGeneration) { clearRecovery(); setRecoveryGate(false); return; }
      const restored = { ...stored, generation: savedSession.generation, navigation_epoch: navigationEpoch() };
      writeRecovery(restored, savedSession);
      keyPair.current = { ...restored.keys };
      markUncertain({ kind: restored.phase, batchId: restored.batch_id, session: savedSession });
      setPreparedRecord(null); setBatchRecord(null); setConfirmPrepare(false); setConfirmEnqueue(false);
      if (restored.expires_at <= Date.now()) { setError("恢复上下文已过期，保持待核对；仅可人工按原 key 查询，不可新建重发。"); return; }
      setTargetIds(restored.prepare_payload.targets.map((target) => target.instance_id));
      setSteps(restored.prepare_payload.steps.map((step) => ({ key: `restored-${step.seq}`, capability_id: step.capability_id, params: { ...step.params }, depends_on: step.depends_on.map((seq) => `restored-${seq}`) })));
      setTtl(restored.prepare_payload.valid_for_seconds ?? 3600); setDeadline(restored.enqueue_payload?.deadline_seconds ?? 3600);
      setRequestApproval(restored.prepare_payload.request_approval ?? false);
      setRecoveryGate(false);
    } catch {
      if (valid()) {
        markUncertain({ kind: "prepare", batchId: null, session: savedSession });
        setError("会话身份或恢复存储读取未确认；保持 unknown，可人工使用已知原 key 进行只读核对。");
      }
    } finally { if (valid()) setRecoveryChecking(false); }
  };

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; contextEpoch.current += 1; detailEpoch.current += 1; catalogEpoch.current += 1; historyEpoch.current += 1; rowEpoch.current += 1; identityEpoch.current += 1; };
  }, []);
  useEffect(() => {
    invalidateReads(); catalogEpoch.current += 1; historyEpoch.current += 1;
    if (!isAuthSessionCurrent(dataSession.current)) {
      dataSession.current = getAuthSession();
      uncertainRef.current = null; setUncertainty(null); setPreparedRecord(null); setBatchRecord(null); setHistoryRecord(null); setCatalogRecord(null);
      setTargetIds([]); setBatchIdInput(""); setReviews({}); setError(""); setConfirmPrepare(false); setConfirmEnqueue(false); setConfirmCancel(false);
      keyPair.current = { prepare: null, enqueue: null };
      recoveryRef.current = null; setRecoveryRecord(null); setOwnerRecord(null); hydratedIdentity.current = null;
      setManualPrepareKey(""); setManualEnqueueKey(""); setManualReceipt(null); setRecoveryGate(true);
    }
    if (open) { loadCatalog(); loadHistory(); loadIdentity(); }
    return () => { contextEpoch.current += 1; detailEpoch.current += 1; rowEpoch.current += 1; catalogEpoch.current += 1; historyEpoch.current += 1; identityEpoch.current += 1; };
  }, [open, session.token, session.generation, session.loginEpoch]);
  useEffect(() => {
    if (previousTargets.current === targetStamp) return;
    previousTargets.current = targetStamp;
    invalidateReads(); setConfirmPrepare(false); setConfirmEnqueue(false);
    if (operation.current?.submitted || uncertainRef.current) {
      if (operation.current?.submitted) markUncertain(operation.current);
      return;
    }
    setPreparedRecord(null); setReviews({}); keyPair.current = { prepare: null, enqueue: null };
  }, [targetStamp]);

  const refreshPlan = async (item) => {
    if (!prepared || operation.current) return;
    const epoch = rowEpoch.current, context = contextEpoch.current, savedSession = getAuthSession();
    const token = ++sequence.current;
    rowsPending.current.set(item.plan_id, token);
    setRowLoading((old) => ({ ...old, [item.plan_id]: true })); setConfirmEnqueue(false);
    const valid = () => current(savedSession) && epoch === rowEpoch.current && context === contextEpoch.current && rowsPending.current.get(item.plan_id) === token;
    try {
      const value = await waitForBatch(getAssistantBatchPlanSnapshot(item.plan_id));
      if (!valid()) return;
      if (value.instance_id !== item.instance_id || value.revision !== item.revision || value.binding_hash !== item.binding_hash) {
        setReviews((old) => ({ ...old, [item.plan_id]: { conflict: true, message: "修订或目标已变化，必须重新冻结并确认。" } }));
      } else {
        setPreparedRecord((old) => old && isAuthSessionCurrent(old.session) ? { ...old, value: { ...old.value, items: old.value.items.map((row) => row.plan_id === item.plan_id ? value : row) } } : old);
        setReviews((old) => ({ ...old, [item.plan_id]: { conflict: false, message: "已通过普通详情回读审批状态" } }));
      }
    } catch {
      if (valid()) setReviews((old) => ({ ...old, [item.plan_id]: { conflict: true, message: "回读未确认，禁止提交；可单独重新查询。" } }));
    } finally {
      if (valid()) { rowsPending.current.delete(item.plan_id); setRowLoading((old) => ({ ...old, [item.plan_id]: false })); }
    }
  };

  useEffect(() => {
    if (!open || !initialBatchId) return;
    setTab("detail");
    readBatch(initialBatchId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialBatchId]);

  const readBatch = async (id) => {
    if (operation.current || !isAssistantBatchId(id)) return;
    invalidateReads();
    const epoch = detailEpoch.current, context = contextEpoch.current, savedSession = getAuthSession();
    setReading(true); setBatchRecord(null); setBatchIdInput(id); setConfirmCancel(false); setError(""); setTab("detail");
    const valid = () => current(savedSession) && epoch === detailEpoch.current && context === contextEpoch.current;
    try {
      const value = await waitForBatch(getAssistantControlBatch(id, { authSession: savedSession }));
      if (!valid()) return;
      setBatchRecord({ value, session: savedSession });
      const pending = uncertainRef.current;
      if (pending && isAuthSessionCurrent(pending.session)
        && pending.kind === "cancel" && pending.batchId === id && value.cancel_requested) {
        uncertainRef.current = null; setUncertainty(null);
      }
    } catch (failure) {
      if (valid()) {
        if (failure?.code === "batch_control_requires_v2") markUncertain({ kind: "control", batchId: id, session: savedSession });
        setError(batchError(failure));
      }
    }
    finally { if (valid()) setReading(false); }
  };

  const checkOriginal = async (kind) => {
    if (!open || operation.current || !["prepare", "enqueue"].includes(kind)) return;
    const saved = recoveryRef.current;
    const trusted = Boolean(saved && isAuthSessionCurrent(saved.session) && owner && !recoveryGate && saved.value.expires_at > Date.now()
      && saved.value.login_epoch === getAuthSession().loginEpoch && saved.value.owner.user_id === owner.user_id
      && saved.value.owner.session_fingerprint === owner.session_fingerprint);
    const data = trusted ? saved.value : null;
    const key = data ? data.keys[kind] : kind === "prepare" ? manualPrepareKey : manualEnqueueKey;
    if (!key) { setError("请输入已知原请求 key；只查询，不会生成新 key。"); return; }
    invalidateReads();
    const op = { kind: `check_${kind}`, session: getAuthSession(), epoch: contextEpoch.current, submitted: false };
    operation.current = op; setBusy(op.kind); setError("");
    const valid = () => current(op.session) && contextEpoch.current === op.epoch && operation.current === op;
    try {
      const value = await waitForBatch(kind === "prepare"
        ? getAssistantBatchPreparation({ idempotencyKey: key, ...(data ? { payload: data.prepare_payload, bindings: data.bindings } : {}) })
        : getAssistantBatchAdmission({ idempotencyKey: key, ...(data ? { payload: data.enqueue_payload, bindings: data.bindings, ownerId: data.owner.user_id, batchId: data.batch_id } : {}) }));
      if (!valid()) return;
      if (!data) {
        setManualReceipt({ value, kind, session: op.session });
        markUncertain({ kind, batchId: kind === "enqueue" ? value.batch_id : null, session: op.session });
        setError("原类型化上下文或会话绑定未确认；仅展示服务端只读回执，不解锁新建或提交。"); return;
      }
      const updated = kind === "prepare" ? { ...data, bindings: frozenBindings(value.items) } : { ...data, batch_id: value.batch_id };
      writeRecovery(updated, op.session);
      if (kind === "prepare") {
        setPreparedRecord({ value, payload: data.prepare_payload, session: op.session, ...(data.batch_id ? { batchId: data.batch_id } : {}) });
        setConfirmPrepare(false); setConfirmEnqueue(false); setReviews({}); setTab("new");
      } else {
        setBatchRecord({ value, session: op.session }); setBatchIdInput(value.batch_id); setTab("detail"); setConfirmEnqueue(false); setConfirmCancel(false);
        setPreparedRecord((old) => old ? { ...old, batchId: value.batch_id } : old);
      }
      const pending = uncertainRef.current;
      // Admission existence proves neither cancellation nor a v2 control outcome.
      if (pending && isAuthSessionCurrent(pending.session)
        && ((kind === data.phase && pending.kind === kind)
          || (kind === "enqueue" && pending.kind === "cancel" && pending.batchId === value.batch_id && value.cancel_requested))) {
        uncertainRef.current = null; setUncertainty(null);
      }
      setRecoveryGate(false); loadHistory();
    } catch (failure) {
      if (valid()) {
        const pending = uncertainRef.current;
        markUncertain({ kind: pending?.kind || data?.phase || kind, batchId: pending?.batchId || data?.batch_id || null, session: op.session });
        setError(`${batchError(failure)} 未找到回执也不代表可以重做，保持 unknown。`);
      }
    } finally {
      if (operation.current === op) { operation.current = null; if (alive.current) setBusy(""); }
    }
  };

  const mutate = async (kind) => {
    if (!["prepare", "enqueue", "cancel"].includes(kind)) return;
    if (!isAuthSessionCurrent(session) || !isAuthSessionCurrent(dataSession.current)
      || !ownerRecord || !isAuthSessionCurrent(ownerRecord.session)) return;
    if (!open || operation.current || uncertainRef.current || rowsPending.current.size || recoveryGate || !owner) return;
    if (kind === "prepare" && (!ready || !confirmPrepare || catalogLoading || Boolean(catalogError) || prepared)) return;
    if (kind === "enqueue" && (!canEnqueue || !confirmEnqueue)) return;
    if (kind === "cancel" && (!batch || !confirmCancel || batch.status === "settled" || batch.cancel_requested || reading)) return;
    invalidateReads(); historyEpoch.current += 1;
    const op = { kind, session: getAuthSession(), epoch: contextEpoch.current, batchId: kind === "cancel" ? batch.batch_id : null, submitted: false };
    operation.current = op; setBusy(kind); setError("");
    const valid = () => current(op.session) && op.epoch === contextEpoch.current && operation.current === op;
    try {
      let promise, payload;
      if (kind === "prepare") {
        const sharedSteps = compileBatchSteps(steps);
        payload = { capability_id: sharedSteps[0].capability_id, definition_version: 1, targets: targetIds.map((id) => ({ instance_id: id, params: {} })),
          steps: sharedSteps, valid_for_seconds: ttl, request_approval: requestApproval };
        keyPair.current.prepare ||= newBatchKey("prepare");
        const now = Date.now();
        writeRecovery({ schema: "capability.batch_recovery/v1", owner, login_epoch: op.session.loginEpoch, generation: op.session.generation, navigation_epoch: navigationEpoch(),
          created_at: now, expires_at: now + 86400000, phase: "prepare", keys: { prepare: keyPair.current.prepare, enqueue: null }, prepare_payload: payload,
          enqueue_payload: null, bindings: null, batch_id: null }, op.session);
        promise = prepareAssistantBatch(payload, { idempotencyKey: keyPair.current.prepare, authSession: op.session });
      } else if (kind === "enqueue") {
        payload = { plan_ids: prepared.items.map((item) => item.plan_id), deadline_seconds: deadline };
        keyPair.current.enqueue ||= newBatchKey("enqueue");
        if (keyPair.current.enqueue === keyPair.current.prepare) throw new TypeError("Independent keys required");
        if (!recoveryRef.current || !isAuthSessionCurrent(recoveryRef.current.session)) throw new TypeError("Original preparation context unavailable");
        writeRecovery({ ...recoveryRef.current.value, phase: "enqueue", keys: { ...keyPair.current }, enqueue_payload: payload,
          bindings: frozenBindings(prepared.items), batch_id: null }, op.session);
        promise = enqueueAssistantBatch(payload, { idempotencyKey: keyPair.current.enqueue, authSession: op.session });
      } else promise = cancelAssistantBatch(batch.batch_id, { authSession: op.session });
      op.submitted = true;
      const value = await waitForBatch(promise);
      if (!valid()) { markUncertain(op); return; }
      if (kind === "prepare") {
        writeRecovery({ ...recoveryRef.current.value, bindings: frozenBindings(value.items) }, op.session);
        setPreparedRecord({ value, payload, session: op.session }); setReviews({}); setConfirmPrepare(false); setConfirmEnqueue(false);
      } else {
        op.batchId = value.batch_id;
        if (kind === "enqueue") writeRecovery({ ...recoveryRef.current.value, batch_id: value.batch_id }, op.session);
        setBatchRecord({ value, session: op.session }); setBatchIdInput(value.batch_id); setTab("detail"); setConfirmCancel(false); setConfirmEnqueue(false);
        if (kind === "enqueue") setPreparedRecord((old) => old ? { ...old, batchId: value.batch_id } : old);
        if (kind === "enqueue" && !sameBatchBinding(value, prepared)) {
          markUncertain(op); setError("服务端已接收批次，但返回的冻结绑定变化，须人工核对；未自动重放。");
        }
      }
      loadHistory();
    } catch (failure) {
      if (valid()) {
        if ((op.submitted && uncertainError(failure)) || failure?.code === "assistant_batch_storage_unavailable") markUncertain(op);
        else if (op.submitted && kind === "prepare") {
          try { clearRecovery(); } catch { markUncertain(op); }
        } else if (op.submitted && kind === "enqueue" && recoveryRef.current) {
          try { writeRecovery({ ...recoveryRef.current.value, phase: "prepare", keys: { prepare: keyPair.current.prepare, enqueue: null }, enqueue_payload: null, batch_id: null }, op.session); }
          catch { markUncertain(op); }
        }
        setError(batchError(failure));
      } else if (op.submitted) markUncertain(op);
    } finally {
      if (operation.current === op) { operation.current = null; if (alive.current) setBusy(""); }
    }
  };

  const planButton = (id) => <Button type="link" icon={<FileSearchOutlined />} disabled={Boolean(busy) || typeof onOpenPlan !== "function"} onClick={() => onOpenPlan(id)}>打开计划</Button>;
  const preparedColumns = [
    { title: "明确目标", dataIndex: "instance_id", width: 100 },
    { title: "计划号", dataIndex: "plan_id", width: 250 },
    { title: "修订", dataIndex: "revision", width: 65 },
    { title: "完整冻结摘要", dataIndex: "binding_hash", width: 260, render: (value) => <Typography.Text code className="assistant-batch-hash">{value}</Typography.Text> },
    { title: "计划 / 审批", key: "state", width: 200, render: (_, row) => <><div>{row.status} · {row.outcome || "尚未执行"}</div><div>{row.approval_state}</div>{reviews[row.plan_id] && <Typography.Text type={reviews[row.plan_id].conflict ? "danger" : "secondary"}>{reviews[row.plan_id].message}</Typography.Text>}</> },
    { title: "清单到期（UTC）", dataIndex: "expires_at", width: 190, render: stamp },
    { title: "逐行处理", key: "actions", width: 210, render: (_, row) => <Space wrap>{planButton(row.plan_id)}<Button icon={<ReloadOutlined />} loading={rowLoading[row.plan_id]} disabled={Boolean(busy)} onClick={() => refreshPlan(row)}>回读</Button></Space> },
  ];
  const memberColumns = [
    { title: "序号", dataIndex: "seq", width: 60 }, { title: "计划号", dataIndex: "plan_id", width: 250 },
    { title: "目标实例 / 引擎", key: "target", width: 190, render: (_, row) => { const target = batchMemberTarget(row, instances); return <><div>{target.name}</div>{target.engine && <Typography.Text type="secondary">{target.engine}</Typography.Text>}</>; } },
    { title: "投递状态", dataIndex: "queue_state", width: 130, render: (value) => queueLabels[value] },
    { title: "计划状态 / 结果", key: "outcome", width: 180, render: (_, row) => `${row.status} · ${row.outcome || "未结算"}` },
    { title: "冻结修订", dataIndex: "bound_revision", width: 90 },
    { title: "冻结摘要", dataIndex: "binding_hash", width: 260, render: (value) => <Typography.Text code className="assistant-batch-hash">{value}</Typography.Text> },
    { title: "绑定", dataIndex: "binding_current", width: 100, render: (value) => value ? "一致" : "已变化" },
    { title: "原因", dataIndex: "reason", width: 200 }, { title: "投递次数", dataIndex: "attempt", width: 90 },
    { title: "计划详情", key: "detail", width: 130, render: (_, row) => planButton(row.plan_id) },
  ];

  return open ? <Modal open={open} centered title="批次任务" width={1140} className="assistant-batch-modal" onCancel={closeWorkspace} footer={<Button onClick={closeWorkspace}>关闭</Button>}>
    <div className="assistant-batch-workspace">
      {recoveryChecking && <Alert type="info" showIcon message="正在核对当前登录身份与恢复定位上下文" />}
      {unknown && <Alert type="warning" showIcon message="提交结果待核对，已禁止重复投递" description={`${unknown.kind}${unknown.batchId ? ` · ${unknown.batchId}` : ""}：关闭不是取消；仅查询历史和详情，不会自动重发。`} />}
      {error && <Alert type={unknown ? "warning" : "error"} showIcon message={error} />}
      {(unknown || recovery) && <div className="assistant-batch-recovery">
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={busy === "check_prepare"} disabled={Boolean(busy) || recoveryChecking || !(recoveryUsable ? recovery.keys.prepare : manualPrepareKey)} onClick={() => checkOriginal("prepare")}>核对原冻结请求</Button>
          <Button icon={<ReloadOutlined />} loading={busy === "check_enqueue"} disabled={Boolean(busy) || recoveryChecking || !(recoveryUsable ? recovery.keys.enqueue : manualEnqueueKey)} onClick={() => checkOriginal("enqueue")}>核对原入队请求</Button>
        </Space>
        {!recoveryUsable && <div className="assistant-batch-fields">
          <label>已知原冻结 key<Input aria-label="已知原冻结请求 key" maxLength={128} value={manualPrepareKey} disabled={Boolean(busy)} onChange={(event) => { invalidateReads(); setManualPrepareKey(event.target.value); setManualReceipt(null); }} /></label>
          <label>已知原入队 key<Input aria-label="已知原入队请求 key" maxLength={128} value={manualEnqueueKey} disabled={Boolean(busy)} onChange={(event) => { invalidateReads(); setManualEnqueueKey(event.target.value); setManualReceipt(null); }} /></label>
        </div>}
        {manual && <>
          <Alert type="warning" showIcon message="只读回执：缺少可信原上下文，仍禁止新建、重发或入队" />
          {manual.kind === "prepare" ? <Table size="small" rowKey="plan_id" dataSource={manual.value.items} scroll={{ x: 850 }} pagination={{ pageSize: 10 }} columns={[
            { title: "明确目标", dataIndex: "instance_id", width: 100 }, { title: "计划号", dataIndex: "plan_id", width: 250 },
            { title: "修订", dataIndex: "revision", width: 65 }, { title: "冻结摘要", dataIndex: "binding_hash", width: 260, render: (value) => <Typography.Text code className="assistant-batch-hash">{value}</Typography.Text> },
            { title: "当前状态", dataIndex: "status", width: 150 }, { title: "详情", key: "detail", render: (_, row) => planButton(row.plan_id) },
          ]} /> : <><Typography.Text code>{manual.value.batch_id}</Typography.Text><Table size="small" rowKey="plan_id" dataSource={manual.value.items} columns={memberColumns} scroll={{ x: 1800 }} pagination={{ pageSize: 10 }} /></>}
        </>}
      </div>}
      <Tabs activeKey={tab} onChange={changeTab} items={[
        { key: "new", label: "冻结与提交", children: <div className="assistant-batch-section">
          {catalogError && <Alert type="error" showIcon message={catalogError} />}
          <Space wrap><Button icon={<ReloadOutlined />} loading={catalogLoading} disabled={Boolean(busy)} onClick={loadCatalog}>刷新目录</Button>
            <Button icon={<PlusOutlined />} disabled={draftLocked} onClick={newDraft}>新建批次</Button></Space>
          <label className="assistant-batch-label">明确目标（{targetIds.length}/100）<Select mode="multiple" aria-label="批次明确目标" value={targetIds} placeholder="逐个选择实例" maxTagCount="responsive" disabled={draftLocked} showSearch optionFilterProp="label"
            onChange={(ids) => { if (ids.length > 100 || new Set(ids).size !== ids.length) { setError("最多选择 100 个不重复的明确目标。"); return; } editDraft(() => setTargetIds(ids)); }}
            options={instances.filter((item) => Number.isSafeInteger(item.id) && item.id > 0).map((item) => ({ value: item.id, label: `${item.instance_name || item.name || item.id} · ${item.db_type} · ${item.host || ""}:${item.port || ""}` }))} /></label>
          {steps.map((step, index) => <div key={step.key} className="assistant-batch-step">
            <Space wrap><Typography.Text strong>共享步骤 {index + 1}{index === 0 ? " · 主能力" : ""}</Typography.Text>
              <Select aria-label={`批次步骤 ${index + 1} 能力`} className="assistant-batch-capability" value={step.capability_id} disabled={draftLocked}
                options={Object.entries(NAMES).map(([id, title]) => ({ value: id, label: `${title} · v1`, disabled: !wired(catalog.find((item) => item.capability_id === id), selectedTargets) }))}
                onChange={(id) => editDraft(() => setSteps(steps.map((item) => item.key === step.key ? { ...item, capability_id: id, params: id === "operations.capability_matrix.read" ? {} : { page: 1, page_size: 20 } } : item)))} />
              <Tooltip title="上移步骤"><Button aria-label={`上移共享步骤 ${index + 1}`} icon={<ArrowUpOutlined />} disabled={draftLocked || index === 0} onClick={() => editSteps({ type: "move", key: step.key, direction: -1 })} /></Tooltip>
              <Tooltip title="下移步骤"><Button aria-label={`下移共享步骤 ${index + 1}`} icon={<ArrowDownOutlined />} disabled={draftLocked || index === steps.length - 1} onClick={() => editSteps({ type: "move", key: step.key, direction: 1 })} /></Tooltip>
              <Tooltip title="删除步骤"><Button aria-label={`删除共享步骤 ${index + 1}`} icon={<DeleteOutlined />} disabled={draftLocked || steps.length === 1} onClick={() => editSteps({ type: "remove", key: step.key })} /></Tooltip>
            </Space>
            <div className="assistant-batch-fields">
              {step.capability_id !== "operations.capability_matrix.read" && <>
                <label>页码<InputNumber aria-label={`批次步骤 ${index + 1} 页码`} min={1} max={1000000} precision={0} disabled={draftLocked} value={step.params.page} onChange={(value) => editDraft(() => setSteps(steps.map((item) => item.key === step.key ? { ...item, params: { ...item.params, page: value } } : item)))} /></label>
                <label>返回上限<InputNumber aria-label={`批次步骤 ${index + 1} 返回上限`} min={1} max={100} precision={0} disabled={draftLocked} value={step.params.page_size} onChange={(value) => editDraft(() => setSteps(steps.map((item) => item.key === step.key ? { ...item, params: { ...item.params, page_size: value } } : item)))} /></label>
              </>}
              {index > 0 && <label>前置步骤<Select mode="multiple" aria-label={`批次步骤 ${index + 1} 依赖`} value={step.depends_on} disabled={draftLocked}
                options={steps.slice(0, index).map((prior, i) => ({ value: prior.key, label: `${i + 1} · ${NAMES[prior.capability_id]}` }))}
                onChange={(value) => editDraft(() => setSteps(steps.map((item) => item.key === step.key ? { ...item, depends_on: value } : item)))} /></label>}
            </div>
          </div>)}
          <Button icon={<PlusOutlined />} disabled={draftLocked || steps.length >= 20} onClick={() => editSteps({ type: "add", step: { key: `step-${++sequence.current}`, capability_id: steps[0].capability_id, params: { ...steps[0].params } } })}>添加共享步骤 ({steps.length}/20)</Button>
          <div className="assistant-batch-fields"><label>清单有效期（秒）<InputNumber aria-label="批次清单有效期" min={60} max={86400} precision={0} value={ttl} disabled={draftLocked} onChange={(value) => editDraft(() => setTtl(value))} /></label>
            <Checkbox checked={requestApproval} disabled={draftLocked} onChange={(event) => editDraft(() => setRequestApproval(event.target.checked))}>需要独立审批</Checkbox></div>
          <Checkbox checked={confirmPrepare} disabled={draftLocked || !ready} onChange={(event) => setConfirmPrepare(event.target.checked)}>确认冻结所列 {targetIds.length} 个明确目标和全部共享步骤，尚不执行</Checkbox>
          <Button type="primary" icon={<SaveOutlined />} loading={busy === "prepare"} disabled={draftLocked || !ready || !confirmPrepare || Boolean(prepared) || catalogLoading || Boolean(catalogError)} onClick={() => mutate("prepare")}>冻结全部明确目标</Button>
          {prepared && <>
            <Alert type="info" showIcon message="全部清单已冻结，尚未提交执行" description={prepared.notice} />
            <Table rowKey="plan_id" size="small" dataSource={prepared.items} columns={preparedColumns} scroll={{ x: 1240 }} pagination={{ pageSize: 10 }} />
            <div className="assistant-batch-fields"><label>批次投递截止（秒）<InputNumber aria-label="批次投递截止秒数" min={1} max={86400} precision={0} value={deadline} disabled={draftLocked || admitted} onChange={(value) => { if (operation.current || uncertainRef.current) return; setDeadline(value); setConfirmEnqueue(false); keyPair.current.enqueue = null; }} /></label></div>
            {admitted && <Alert type="info" showIcon message={`已提交批次 ${preparedRecord.batchId}，不会再次提交这些冻结计划`} />}
            {!canEnqueue && !busy && !admitted && <Alert type="warning" showIcon message="存在未审批、不可执行、过期或待核对清单，不能提交批次" />}
            <Checkbox checked={confirmEnqueue} disabled={!canEnqueue} onChange={(event) => setConfirmEnqueue(event.target.checked)}>确认仅提交上述 {prepared.total} 个冻结计划；实际准入仍由服务端核验</Checkbox>
            <Button type="primary" icon={<SendOutlined />} loading={busy === "enqueue"} disabled={!canEnqueue || !confirmEnqueue} onClick={() => mutate("enqueue")}>提交批次</Button>
          </>}
        </div> },
        { key: "history", label: "批次历史", children: <div className="assistant-batch-section">
          <Button icon={<ReloadOutlined />} loading={historyLoading} disabled={Boolean(busy)} onClick={() => loadHistory(history.page)}>刷新历史</Button>
          {historyError && <Alert type="error" showIcon message={historyError} />}
          <Table rowKey="batch_id" size="small" loading={historyLoading} dataSource={history.items} scroll={{ x: 950 }} columns={[
            { title: "批次号", dataIndex: "batch_id", width: 280 }, { title: "目标数", dataIndex: "total", width: 80 },
            { title: "状态", key: "status", width: 180, render: (_, row) => { const view = batchView(row); return <Tag color={view.color}>{view.label}</Tag>; } },
            { title: "计划已结算", dataIndex: "settled_plans", width: 100 }, { title: "创建时间", dataIndex: "created_at", width: 190, render: stamp },
            { title: "详情", key: "detail", width: 90, render: (_, row) => <Button type="link" disabled={Boolean(busy)} onClick={() => readBatch(row.batch_id)}>查看</Button> },
          ]} pagination={{ current: history.page, pageSize: PAGE_SIZE, total: history.total, showSizeChanger: false, onChange: loadHistory }} />
        </div> },
        { key: "control", label: "运行控制", children: open && tab === "control" ? <AssistantBatchControlView initialBatchId={batchIdInput} isActive={() => visible.current} /> : null },
        { key: "detail", label: "批次详情", children: <div className="assistant-batch-section">
          <Space.Compact className="assistant-batch-id"><Input aria-label="批次号" value={batchIdInput} maxLength={30} placeholder="CB + 28 位小写十六进制字符" disabled={Boolean(busy)} onChange={(event) => { invalidateReads(); setBatchRecord(null); setBatchIdInput(event.target.value); setConfirmCancel(false); }} />
            <Button icon={<ReloadOutlined />} loading={reading} disabled={Boolean(busy) || !isAssistantBatchId(batchIdInput)} onClick={() => readBatch(batchIdInput)}>读取批次</Button></Space.Compact>
          {batch ? <>
            <Space wrap><Tag color={batchView(batch).color}>{batchView(batch).label}</Tag><Typography.Text code>{batch.batch_id}</Typography.Text>{batch.reused && <Tag>复用已接收批次</Tag>}</Space>
            {batch.requires_attention && <Alert type="warning" showIcon message="存在在途未知或阻断成员，需要人工核对；不自动重放" />}
            <Alert type="info" showIcon message={batch.cancel_requested ? "取消投递已登记；仅取消尚未投递成员，在途步骤未确认停止。" : "已由服务端接收；关闭窗口不会取消投递。"} />
            <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
              { key: "owner", label: "请求人", children: batch.requested_by }, { key: "created", label: "创建时间", children: stamp(batch.created_at) },
              { key: "deadline", label: "投递截止", children: stamp(batch.deadline_at) }, { key: "total", label: "计划已结算 / 总数", children: `${batch.settled_plans} / ${batch.total}` },
              { key: "queues", label: "投递账本", children: Object.entries(batch.queue_counts).map(([key, count]) => <Tag key={key}>{queueLabels[key]} {count}</Tag>) },
              { key: "outcomes", label: "计划结果计数", children: Object.entries(batch.outcomes).map(([key, count]) => <Tag key={key}>{key} {count}</Tag>) },
            ]} />
            <Progress percent={Math.floor(100 * batch.settled_plans / batch.total)} showInfo={false} />
            <Table rowKey="plan_id" size="small" dataSource={batch.items} columns={memberColumns} scroll={{ x: 1800 }} pagination={{ pageSize: 10 }} />
            <Checkbox checked={confirmCancel} disabled={Boolean(busy) || Boolean(unknown) || batch.status === "settled" || batch.cancel_requested} onChange={(event) => setConfirmCancel(event.target.checked)}>确认只取消尚未投递的成员，不宣称在途操作停止</Checkbox>
            <Button danger icon={<StopOutlined />} loading={busy === "cancel"} disabled={Boolean(busy) || Boolean(unknown) || !confirmCancel || batch.status === "settled" || batch.cancel_requested} onClick={() => mutate("cancel")}>取消投递</Button>
          </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={reading ? "正在读取批次" : "尚未选择批次"} />}
        </div> },
      ]} />
    </div>
  </Modal> : null;
}
