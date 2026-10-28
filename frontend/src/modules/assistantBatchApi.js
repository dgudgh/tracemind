import { api, buildQuery } from "../api";

export const ASSISTANT_BATCH_READERS = Object.freeze({
  "inspection.run.query": "巡检任务查询",
  "inspection.template.read": "巡检模板查询",
  "operations.capability_matrix.read": "运维能力矩阵",
});
const base = "/assistant";
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const integer = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;
const exact = (value, required, optional = []) => record(value) && required.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
const matches = (value, pattern, length) => typeof value === "string" && value.length === length && pattern.test(value);
export const isAssistantBatchId = (value) => matches(value, /^CB[a-f0-9]{28}$/, 30);
const planId = (value) => matches(value, /^pl[a-f0-9]{26}$/, 28);
const binding = (value) => matches(value, /^[a-f0-9]{64}$/, 64);
const reader = (value) => typeof value === "string" && Object.hasOwn(ASSISTANT_BATCH_READERS, value);
const planStates = ["draft", "validated", "awaiting_approval", "approved", "running", "settled"];
const outcomes = ["succeeded", "partial", "failed", "cancelled", "expired", "unknown", "plan_only", "blocked", "unsupported", "needs_input"];
const queueStates = ["queued", "running", "archived", "reconciliation_required", "cancelled", "expired", "blocked"];
const approvalStates = ["not_required", "pending", "approved", "revoked"];
const preparedFields = ["plan_id", "instance_id", "revision", "binding_hash", "status", "outcome", "approval_state", "approval_available", "expires_at", "reused"];

function utc(value) {
  if (typeof value !== "string" || value.length > 32
    || /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.exec(value)?.[0] !== value) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 19) === value.slice(0, 19);
}

function protocol(value, valid) {
  if (!valid) throw Object.assign(new Error("Invalid assistant batch API response"), { code: "assistant_batch_response_invalid" });
  return value;
}

function input(valid) {
  if (!valid) throw new TypeError("Invalid assistant batch input");
}

function keyHeaders(key) {
  input(typeof key === "string" && key.length >= 1 && key.length <= 128 && /^[A-Za-z0-9._:-]+$/.exec(key)?.[0] === key);
  return { "Idempotency-Key": key };
}

function receiptFailure(error) {
  if (error?.status === 409 && error?.responseData?.detail?.code === "batch_control_requires_v2") {
    throw Object.assign(new Error("Controlled batch requires v2"), { status: 409, code: "batch_control_requires_v2" });
  }
  throw error;
}

function paramsValid(capability, params) {
  return exact(params, [], capability === "operations.capability_matrix.read" ? [] : ["page", "page_size"])
    && Object.entries(params).every(([key, value]) => integer(value, 1, key === "page" ? 1000000 : 100));
}

function stepsValid(steps, capability, version) {
  return Array.isArray(steps) && steps.length >= 1 && steps.length <= 20
    && steps[0]?.capability_id === capability && steps[0]?.definition_version === version
    && steps.every((step, index) => exact(step, ["seq", "capability_id", "definition_version", "params", "depends_on"], ["title", "input_refs", "fanout", "retries"])
      && step.seq === index + 1 && reader(step.capability_id) && step.definition_version === 1
      && (step.title === undefined || (typeof step.title === "string" && step.title.length >= 1 && step.title.length <= 200))
      && paramsValid(step.capability_id, step.params) && Array.isArray(step.depends_on)
      && new Set(step.depends_on).size === step.depends_on.length
      && step.depends_on.every((seq) => integer(seq, 1, index))
      && (step.input_refs === undefined || exact(step.input_refs, []))
      && (step.fanout === undefined || step.fanout === 1) && (step.retries === undefined || step.retries === 0));
}

function prepareInput(payload) {
  input(exact(payload, ["capability_id", "definition_version", "targets"], ["steps", "valid_for_seconds", "request_approval"])
    && reader(payload.capability_id) && payload.definition_version === 1
    && Array.isArray(payload.targets) && payload.targets.length >= 1 && payload.targets.length <= 100
    && payload.targets.every((target) => exact(target, ["instance_id"], ["params"])
      && integer(target.instance_id, 1, Number.MAX_SAFE_INTEGER) && paramsValid(payload.capability_id, target.params === undefined ? {} : target.params))
    && new Set(payload.targets.map((target) => target.instance_id)).size === payload.targets.length
    && (payload.valid_for_seconds === undefined || integer(payload.valid_for_seconds, 60, 86400))
    && (payload.request_approval === undefined || typeof payload.request_approval === "boolean")
    && (payload.steps === undefined || (stepsValid(payload.steps, payload.capability_id, payload.definition_version)
      && payload.targets.every((target) => !target.params || Object.keys(target.params).length === 0))));
  input(new globalThis.TextEncoder().encode(JSON.stringify(payload)).length <= 65536);
}

function planStatus(value) {
  return planStates.includes(value.status) && (value.outcome === null || outcomes.includes(value.outcome))
    && (value.status === "settled" ? value.outcome !== null : value.outcome === null);
}

function preparedItem(value) {
  return exact(value, preparedFields) && planId(value.plan_id) && integer(value.instance_id, 1, Number.MAX_SAFE_INTEGER)
    && integer(value.revision, 1, 2147483647) && binding(value.binding_hash) && planStatus(value)
    && approvalStates.includes(value.approval_state) && typeof value.approval_available === "boolean"
    && utc(value.expires_at) && typeof value.reused === "boolean";
}

function preparation(value, payload) {
  return protocol(value, exact(value, ["schema", "accepted", "status", "total", "items", "notice"])
    && value.schema === "capability.batch_preparation/v1" && value.accepted === false && value.status === "prepared"
    && integer(value.total, 1, 100) && (!payload || value.total === payload.targets.length) && Array.isArray(value.items) && value.items.length === value.total
    && value.items.every((item, index) => preparedItem(item) && (!payload || item.instance_id === payload.targets[index].instance_id))
    && new Set(value.items.map((item) => item.plan_id)).size === value.total
    && new Set(value.items.map((item) => item.instance_id)).size === value.total
    && typeof value.notice === "string" && value.notice.length <= 1000);
}

function counts(value, allowed, total) {
  return exact(value, [], allowed) && Object.values(value).every((count) => integer(count, 1, total));
}

function resultReference(value, id) {
  return value === null || (exact(value, ["kind", "plan_id", "schema", "steps"])
    && value.kind === "capability_plan_steps" && value.schema === "capability.plan_results/v1" && value.plan_id === id
    && Array.isArray(value.steps) && value.steps.length <= 20 && value.steps.every((seq) => integer(seq, 1, 20))
    && new Set(value.steps).size === value.steps.length);
}

function member(value, index) {
  return exact(value, ["plan_id", "seq", "queue_state", "reason", "status", "outcome", "outcome_kind", "bound_revision", "binding_hash", "binding_current", "result_ref", "attempt", "started_at", "finished_at"])
    && planId(value.plan_id) && value.seq === index + 1 && queueStates.includes(value.queue_state) && planStatus(value)
    && (value.reason === null || (typeof value.reason === "string" && value.reason.length <= 96 && /^[a-z][a-z0-9_]*$/.exec(value.reason)?.[0] === value.reason))
    && (value.outcome_kind === null || (typeof value.outcome_kind === "string" && value.outcome_kind.length <= 96))
    && integer(value.bound_revision, 1, 2147483647) && binding(value.binding_hash) && typeof value.binding_current === "boolean"
    && (value.binding_current || value.queue_state === "cancelled") && integer(value.attempt, 0, 2147483647)
    && (value.started_at === null || utc(value.started_at)) && (value.finished_at === null || utc(value.finished_at))
    && resultReference(value.result_ref, value.plan_id) && (value.queue_state !== "cancelled" || value.result_ref === null);
}

function batch(value, expectedId, detail = true) {
  const fields = ["schema", "batch_id", "requested_by", "status", "outcome", "accepted", "reused", "total", "settled_plans", "queue_counts", "outcomes", "requires_attention", "cancel_requested", "created_at", "deadline_at"];
  protocol(value, exact(value, [...fields, ...(detail ? ["items"] : [])], ["cancel_requested_at"])
    && value.schema === "capability.batch_read/v1" && isAssistantBatchId(value.batch_id)
    && (expectedId === undefined || value.batch_id === expectedId) && integer(value.requested_by, 1, Number.MAX_SAFE_INTEGER)
    && ["queued", "running", "reconciliation_required", "settled"].includes(value.status)
    && (value.status === "settled" ? outcomes.includes(value.outcome) : value.outcome === null)
    && value.accepted === true && typeof value.reused === "boolean" && integer(value.total, 1, 100)
    && integer(value.settled_plans, 0, value.total) && counts(value.queue_counts, queueStates, value.total)
    && Object.values(value.queue_counts).reduce((sum, count) => sum + count, 0) === value.total
    && counts(value.outcomes, outcomes, value.total)
    && Object.values(value.outcomes).reduce((sum, count) => sum + count, 0) === value.settled_plans
    && typeof value.requires_attention === "boolean" && typeof value.cancel_requested === "boolean"
    && utc(value.created_at) && utc(value.deadline_at) && new Date(value.deadline_at) > new Date(value.created_at)
    && (value.cancel_requested_at === undefined || value.cancel_requested_at === null || utc(value.cancel_requested_at)));
  const expectedStatus = value.queue_counts.reconciliation_required || value.queue_counts.blocked ? "reconciliation_required"
    : value.queue_counts.running ? "running" : value.queue_counts.queued ? "queued" : "settled";
  protocol(value, value.status === expectedStatus
    && value.requires_attention === (value.status === "reconciliation_required" || Boolean(value.outcomes.unknown))
    && (value.outcome !== "succeeded" || (value.queue_counts.archived === value.total && value.outcomes.succeeded === value.total)));
  if (detail) {
    protocol(value, Array.isArray(value.items) && value.items.length === value.total
      && value.items.every(member) && new Set(value.items.map((item) => item.plan_id)).size === value.total);
    const actualQueue = {}, actualOutcomes = {};
    for (const item of value.items) {
      actualQueue[item.queue_state] = (actualQueue[item.queue_state] || 0) + 1;
      if (item.outcome !== null) actualOutcomes[item.outcome] = (actualOutcomes[item.outcome] || 0) + 1;
    }
    protocol(value, queueStates.every((key) => (actualQueue[key] || 0) === (value.queue_counts[key] || 0))
      && outcomes.every((key) => (actualOutcomes[key] || 0) === (value.outcomes[key] || 0)));
  }
  return value;
}

export function prepareAssistantBatch(payload, { idempotencyKey, authSession } = {}) {
  prepareInput(payload);
  const headers = keyHeaders(idempotencyKey);
  return api.request(`${base}/batches/prepare`, { method: "POST", headers, authSession, body: JSON.stringify(payload) })
    .then((value) => preparation(value, payload));
}

function enqueueInput(payload) {
  input(exact(payload, ["plan_ids"], ["deadline_seconds"]) && Array.isArray(payload.plan_ids)
    && payload.plan_ids.length >= 1 && payload.plan_ids.length <= 100 && payload.plan_ids.every(planId)
    && new Set(payload.plan_ids).size === payload.plan_ids.length
    && (payload.deadline_seconds === undefined || integer(payload.deadline_seconds, 1, 86400)));
}

export function enqueueAssistantBatch(payload, { idempotencyKey, authSession } = {}) {
  enqueueInput(payload);
  const headers = keyHeaders(idempotencyKey);
  return api.request(`${base}/batches`, { method: "POST", headers, authSession, preserveErrorData: true, body: JSON.stringify(payload) }).then((value) => {
    batch(value);
    return protocol(value, value.total === payload.plan_ids.length && value.items.every((item, index) => item.plan_id === payload.plan_ids[index]));
  }).catch(receiptFailure);
}

function bindingsValid(values, payload) {
  return Array.isArray(values) && values.length === payload.targets.length
    && values.every((item, index) => exact(item, ["plan_id", "instance_id", "revision", "binding_hash"])
      && planId(item.plan_id) && item.instance_id === payload.targets[index].instance_id
      && integer(item.revision, 1, 2147483647) && binding(item.binding_hash))
    && new Set(values.map((item) => item.plan_id)).size === values.length;
}

export function getAssistantBatchPreparation({ idempotencyKey, payload, bindings } = {}) {
  const headers = keyHeaders(idempotencyKey);
  if (payload !== undefined) prepareInput(payload);
  if (bindings !== undefined && bindings !== null) input(Boolean(payload) && bindingsValid(bindings, payload));
  return api.get(`${base}/batches/preparation`, { headers: { ...headers, "X-Assistant-Batch-Version": "1" }, preserveErrorData: true }).then((value) => {
    preparation(value, payload);
    return protocol(value, !bindings || value.items.every((item, index) => item.plan_id === bindings[index].plan_id
      && item.instance_id === bindings[index].instance_id && item.revision === bindings[index].revision
      && item.binding_hash === bindings[index].binding_hash));
  }).catch(receiptFailure);
}

export function getAssistantBatchAdmission({ idempotencyKey, payload, bindings, ownerId, batchId } = {}) {
  const headers = keyHeaders(idempotencyKey);
  if (payload !== undefined) enqueueInput(payload);
  if (ownerId !== undefined) input(integer(ownerId, 1, Number.MAX_SAFE_INTEGER));
  if (batchId !== undefined && batchId !== null) input(isAssistantBatchId(batchId));
  if (bindings !== undefined && bindings !== null) input(Array.isArray(bindings) && Boolean(payload)
    && bindings.length === payload.plan_ids.length && bindings.every((item, index) => record(item)
      && item.plan_id === payload.plan_ids[index] && integer(item.revision, 1, 2147483647) && binding(item.binding_hash)));
  return api.get(`${base}/batches/admission`, { headers: { ...headers, "X-Assistant-Batch-Version": "1" }, preserveErrorData: true }).then((value) => {
    batch(value, batchId ?? undefined);
    return protocol(value, (ownerId === undefined || value.requested_by === ownerId)
      && (!payload || (value.total === payload.plan_ids.length && value.items.every((item, index) => item.plan_id === payload.plan_ids[index])))
      && (!bindings || value.items.every((item, index) => item.binding_current === true
        && item.bound_revision === bindings[index].revision && item.binding_hash === bindings[index].binding_hash)));
  }).catch(receiptFailure);
}

export function getAssistantBatchRecoveryIdentity(token) {
  input(typeof token === "string" && token.length > 0 && token.length <= 16384);
  return api.get("/auth/current-user").then(async (value) => {
    protocol(value, record(value) && integer(value.id, 1, Number.MAX_SAFE_INTEGER));
    const bytes = await globalThis.crypto.subtle.digest("SHA-256", new globalThis.TextEncoder().encode(token));
    return { user_id: value.id, session_fingerprint: Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("") };
  });
}

// Only typed read inputs and receipt locators survive a reload, never credentials or results.
export function validateAssistantBatchRecovery(value, now = Date.now()) {
  input(exact(value, ["schema", "owner", "login_epoch", "generation", "navigation_epoch", "created_at", "expires_at", "phase", "keys", "prepare_payload", "enqueue_payload", "bindings", "batch_id"])
    && value.schema === "capability.batch_recovery/v1"
    && exact(value.owner, ["user_id", "session_fingerprint"]) && integer(value.owner.user_id, 1, Number.MAX_SAFE_INTEGER) && binding(value.owner.session_fingerprint)
    && typeof value.login_epoch === "string" && value.login_epoch.length >= 16 && value.login_epoch.length <= 128
    && /^[A-Za-z0-9._:-]+$/.exec(value.login_epoch)?.[0] === value.login_epoch
    && integer(value.generation, 0, Number.MAX_SAFE_INTEGER) && typeof value.navigation_epoch === "number" && Number.isFinite(value.navigation_epoch) && value.navigation_epoch > 0
    && integer(value.created_at, 1, Number.MAX_SAFE_INTEGER) && integer(value.expires_at, value.created_at + 1, value.created_at + 86400000)
    && value.created_at <= now + 60000 && ["prepare", "enqueue"].includes(value.phase)
    && exact(value.keys, ["prepare", "enqueue"]));
  keyHeaders(value.keys.prepare);
  input(/^prepare:[a-f0-9]{32}$/.exec(value.keys.prepare)?.[0] === value.keys.prepare);
  input(value.keys.enqueue === null || (/^enqueue:[a-f0-9]{32}$/.exec(value.keys.enqueue)?.[0] === value.keys.enqueue));
  prepareInput(value.prepare_payload);
  input(Array.isArray(value.prepare_payload.steps) && value.prepare_payload.targets.every((target) => target.params === undefined || Object.keys(target.params).length === 0)
    && value.prepare_payload.steps.every((step) => step.title === undefined || step.title === ASSISTANT_BATCH_READERS[step.capability_id]));
  input(value.bindings === null || bindingsValid(value.bindings, value.prepare_payload));
  input(value.batch_id === null || isAssistantBatchId(value.batch_id));
  if (value.phase === "prepare") input(value.enqueue_payload === null && value.keys.enqueue === null && value.batch_id === null);
  else {
    keyHeaders(value.keys.enqueue); enqueueInput(value.enqueue_payload);
    input(Boolean(value.bindings) && value.enqueue_payload.plan_ids.length === value.bindings.length
      && value.enqueue_payload.plan_ids.every((id, index) => id === value.bindings[index].plan_id));
  }
  input(new globalThis.TextEncoder().encode(JSON.stringify(value)).length <= 131072);
  return value;
}

export function getAssistantBatch(id) {
  input(isAssistantBatchId(id));
  return api.get(`${base}/batches/${id}`, { headers: { "X-Assistant-Batch-Version": "1" }, preserveErrorData: true })
    .then((value) => batch(value, id)).catch(receiptFailure);
}

export function cancelAssistantBatch(id, { authSession } = {}) {
  input(isAssistantBatchId(id));
  return api.request(`${base}/batches/${id}/cancel`, { method: "POST", headers: { "X-Assistant-Batch-Version": "1" }, authSession, preserveErrorData: true, body: "{}" }).then((value) => {
    batch(value, id);
    return protocol(value, value.cancel_requested === true);
  }).catch(receiptFailure);
}

export function listAssistantBatches(params = {}) {
  input(exact(params, [], ["page", "page_size"]) && (params.page === undefined || integer(params.page, 1, 1000000))
    && (params.page_size === undefined || integer(params.page_size, 1, 100)));
  return api.get(`${base}/batches${buildQuery(params)}`, { headers: { "X-Assistant-Batch-Version": "1" }, preserveErrorData: true }).then((value) => {
    protocol(value, exact(value, ["schema", "items", "total", "page", "page_size"])
      && value.schema === "capability.batch_list/v1" && integer(value.total, 0, Number.MAX_SAFE_INTEGER)
      && integer(value.page, 1, 1000000) && integer(value.page_size, 1, 100)
      && (params.page === undefined || value.page === params.page) && (params.page_size === undefined || value.page_size === params.page_size)
      && Array.isArray(value.items) && value.items.length <= value.page_size && value.items.length <= value.total
      && new Set(value.items.map((item) => item?.batch_id)).size === value.items.length);
    value.items.forEach((item) => batch(item, undefined, false));
    return value;
  }).catch(receiptFailure);
}

// A single explicit owner read refreshes approval metadata, never privileged approval results.
export function getAssistantBatchPlanSnapshot(id) {
  input(planId(id));
  return api.get(`${base}/plans/${id}`).then((value) => {
    protocol(value, record(value) && value.schema === "capability.plan_read/v1" && value.plan_id === id);
    const summary = Object.fromEntries(preparedFields.map((key) => [key, value[key]]));
    protocol(value, preparedItem(summary) && reader(value.capability_id) && value.definition_version === 1
      && Array.isArray(value.steps) && value.steps.length >= 1 && value.steps.length <= 20
      && value.steps.every((step) => record(step) && reader(step.capability_id) && step.definition_version === 1));
    return summary;
  });
}
