import { api, buildQuery } from "../api";

// Control DTOs describe local scheduling only. Original-key recovery is GET-only;
// current projection versions may advance, but the original command must match exactly.
export const ASSISTANT_CONTROL_ACTIONS = Object.freeze(["pause", "resume", "stop", "probe"]);
const statuses = ["accepted", "sent", "acknowledged", "applied", "rejected", "expired", "superseded"];
const stages = ["received", "quiesced", "terminated", "not_started", "resumed", "observed", "completed", "rejected"];
const queues = ["queued", "running", "archived", "reconciliation_required", "cancelled", "expired", "blocked", "paused", "stopped"];
const observations = ["running", "paused", "stopped", "completed", "unknown"];
const targetEngines = ["oracle", "mysql", "postgresql", "sqlserver", "host"];
const targetFields = ["instance_id", "db_type", "capability_id"];
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const exact = (value, keys, optional = []) => record(value) && keys.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => keys.includes(key) || optional.includes(key));
const integer = (value, min = 0) => Number.isSafeInteger(value) && value >= min;
const matches = (value, pattern, length) => typeof value === "string" && value.length === length && pattern.test(value);
const planId = (value) => matches(value, /^pl[a-f0-9]{26}$/, 28);
const commandId = (value) => matches(value, /^CC[a-f0-9]{28}$/, 30);
const hash = (value) => matches(value, /^[a-f0-9]{64}$/, 64);
export const isAssistantControlExecutionId = (value) => integer(value, 1);
export const isAssistantControlLoginEpoch = (value) => matches(value, /^[a-f0-9]{32}$/, 32);
const keyValid = (value) => typeof value === "string" && value.length >= 1 && value.length <= 128
  && /^[A-Za-z0-9._:-]+$/.exec(value)?.[0] === value;
const actionValid = (value) => ASSISTANT_CONTROL_ACTIONS.includes(value);
const input = (valid) => { if (!valid) throw new TypeError("Invalid assistant control input"); };
const protocol = (valid) => {
  if (!valid) throw Object.assign(new Error("Invalid assistant control response"), { code: "assistant_control_response_invalid" });
};

function view(value, executionId, expected) {
  protocol(exact(value, ["schema", "execution_id", "plan_id", "control_enabled", "control_version", "desired_state", "observed_state", "queue_state",
    "checkpoint_hash", "fencing_epoch", "scope", "external_stop", "external_fence", "reused", "command"])
    && value.schema === "capability.control/v1" && value.execution_id === executionId && planId(value.plan_id)
    && typeof value.control_enabled === "boolean" && integer(value.control_version)
    && ["run", "pause", "stop"].includes(value.desired_state) && (value.observed_state === null || observations.includes(value.observed_state))
    && queues.includes(value.queue_state) && (value.checkpoint_hash === null || hash(value.checkpoint_hash)) && integer(value.fencing_epoch)
    && value.scope === "local_step_boundary" && value.external_stop === "unsupported" && value.external_fence === "unsupported"
    && typeof value.reused === "boolean");
  const command = value.command;
  protocol(command === null || (exact(command, ["command_id", "action", "status", "ack_stage", "control_version"])
    && commandId(command.command_id) && actionValid(command.action) && statuses.includes(command.status)
    && (command.ack_stage === null || stages.includes(command.ack_stage)) && integer(command.control_version, 1)
    && command.control_version <= value.control_version));
  if (command?.status === "applied") {
    protocol(({ pause: ["quiesced"], resume: ["resumed"], stop: ["terminated", "not_started"], probe: ["observed"] })[command.action].includes(command.ack_stage));
  }
  if (["paused", "stopped"].includes(value.queue_state)) protocol(value.control_enabled && value.checkpoint_hash !== null);
  if (expected) protocol(value.plan_id === expected.plan_id && command !== null && command.action === expected.action
    && command.control_version === expected.expected_control_version + 1
    && (expected.command_id === null || expected.command_id === undefined || command.command_id === expected.command_id));
  protocol(new globalThis.TextEncoder().encode(JSON.stringify(value)).length <= 8192);
  return value;
}

function expectationValid(value) {
  return exact(value, ["plan_id", "action", "expected_control_version"], ["command_id"])
    && planId(value.plan_id) && actionValid(value.action) && integer(value.expected_control_version)
    && value.expected_control_version < Number.MAX_SAFE_INTEGER
    && (value.command_id === undefined || value.command_id === null || commandId(value.command_id));
}

function failure(error) {
  const code = error?.responseData?.detail?.code;
  if (typeof code === "string" && code.length <= 96 && /^[a-z][a-z0-9_]*$/.exec(code)?.[0] === code) {
    throw Object.assign(new Error("Assistant control request rejected"), { status: error.status, code });
  }
  throw error;
}

export function getAssistantControl(executionId, { idempotencyKey, expected } = {}) {
  input(isAssistantControlExecutionId(executionId));
  input(idempotencyKey === undefined || keyValid(idempotencyKey));
  input(expected === undefined || (idempotencyKey !== undefined && expectationValid(expected)));
  return api.get(`/assistant/executions/${executionId}/control`, {
    ...(idempotencyKey === undefined ? {} : { headers: { "Idempotency-Key": idempotencyKey } }), preserveErrorData: true,
  }).then((value) => {
    view(value, executionId, expected);
    protocol(idempotencyKey === undefined || value.command !== null);
    return value;
  }).catch(failure);
}

export function requestAssistantControl(executionId, action, payload, { idempotencyKey, planId: expectedPlanId, authSession } = {}) {
  input(isAssistantControlExecutionId(executionId) && actionValid(action) && keyValid(idempotencyKey) && planId(expectedPlanId));
  input(exact(payload, ["expected_control_version", "reason"]) && integer(payload.expected_control_version)
    && payload.expected_control_version < Number.MAX_SAFE_INTEGER && typeof payload.reason === "string"
    && payload.reason.trim().length >= 1 && Array.from(payload.reason).length <= 300);
  input(new globalThis.TextEncoder().encode(JSON.stringify(payload)).length <= 4096);
  return api.request(`/assistant/executions/${executionId}/${action}`, {
    method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(payload), preserveErrorData: true,
    ...(authSession === undefined ? {} : { authSession }),
  }).then((value) => view(value, executionId, { plan_id: expectedPlanId, action, expected_control_version: payload.expected_control_version }))
    .catch(failure);
}

export async function getAssistantControlSessionFingerprint(token) {
  input(typeof token === "string" && token.length > 0 && token.length <= 16384);
  const bytes = await globalThis.crypto.subtle.digest("SHA-256", new globalThis.TextEncoder().encode(token));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// No reason, token, SQL, result or driver payload is persisted in this locator.
export function validateAssistantControlRecovery(value, now = Date.now()) {
  input(exact(value, ["schema", "execution_id", "plan_id", "action", "expected_control_version", "command_id", "idempotency_key",
    "login_epoch", "session_fingerprint", "generation", "navigation_epoch", "created_at", "expires_at", "uncertain"])
    && value.schema === "capability.control_recovery/v1" && isAssistantControlExecutionId(value.execution_id)
    && expectationValid({ plan_id: value.plan_id, action: value.action, expected_control_version: value.expected_control_version, command_id: value.command_id })
    && keyValid(value.idempotency_key) && /^control:[a-f0-9]{32}$/.exec(value.idempotency_key)?.[0] === value.idempotency_key
    && isAssistantControlLoginEpoch(value.login_epoch) && hash(value.session_fingerprint) && integer(value.generation)
    && typeof value.navigation_epoch === "number" && Number.isFinite(value.navigation_epoch) && value.navigation_epoch > 0
    && integer(value.created_at, 1) && value.created_at <= now + 60000
    && integer(value.expires_at, value.created_at + 1) && value.expires_at <= value.created_at + 86400000
    && typeof value.uncertain === "boolean" && (value.uncertain || commandId(value.command_id)));
  input(new globalThis.TextEncoder().encode(JSON.stringify(value)).length <= 2048);
  return value;
}

function utc(value) {
  if (typeof value !== "string" || value.length > 32
    || /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.exec(value)?.[0] !== value) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 19) === value.slice(0, 19);
}

const outcomes = ["succeeded", "partial", "failed", "cancelled", "expired", "unknown", "plan_only", "blocked", "unsupported", "needs_input"];
function resultReference(value, id) {
  return value === null || (exact(value, ["kind", "plan_id", "schema", "steps"])
    && value.kind === "capability_plan_steps" && value.plan_id === id && value.schema === "capability.plan_results/v1"
    && Array.isArray(value.steps) && value.steps.length <= 20 && new Set(value.steps).size === value.steps.length
    && value.steps.every((seq) => integer(seq, 1) && seq <= 20));
}

function batchMember(value, index) {
  return exact(value, ["plan_id", "seq", "queue_state", "reason", "status", "outcome", "outcome_kind", "bound_revision", "binding_hash", "binding_current",
    "result_ref", "attempt", "started_at", "finished_at", "execution_id", "control_enabled", "control_version", "desired_state", "observed_state", "checkpoint_hash", "fencing_epoch"], targetFields)
    && (targetFields.every((key) => Object.hasOwn(value, key)) || targetFields.every((key) => !Object.hasOwn(value, key)))
    && (!Object.hasOwn(value, "instance_id") || ((value.instance_id === null || integer(value.instance_id, 1))
      && (value.db_type === null || targetEngines.includes(value.db_type))
      && typeof value.capability_id === "string" && value.capability_id.length <= 64
      && /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}$/.exec(value.capability_id)?.[0] === value.capability_id))
    && planId(value.plan_id) && value.seq === index + 1 && queues.includes(value.queue_state)
    && (value.reason === null || (typeof value.reason === "string" && value.reason.length <= 96 && /^[a-z][a-z0-9_]*$/.exec(value.reason)?.[0] === value.reason))
    && ["draft", "validated", "awaiting_approval", "approved", "running", "settled"].includes(value.status)
    && (value.status === "settled" ? outcomes.includes(value.outcome) : value.outcome === null)
    && (value.outcome_kind === null || (typeof value.outcome_kind === "string" && value.outcome_kind.length <= 96))
    && integer(value.bound_revision, 1) && value.bound_revision <= 2147483647 && hash(value.binding_hash)
    && typeof value.binding_current === "boolean" && (value.binding_current || value.queue_state === "cancelled")
    && resultReference(value.result_ref, value.plan_id) && (value.queue_state !== "cancelled" || value.result_ref === null)
    && integer(value.attempt) && value.attempt <= 2147483647 && (value.started_at === null || utc(value.started_at))
    && (value.finished_at === null || utc(value.finished_at)) && isAssistantControlExecutionId(value.execution_id)
    && typeof value.control_enabled === "boolean" && integer(value.control_version) && integer(value.fencing_epoch)
    && ["run", "pause", "stop"].includes(value.desired_state) && (value.observed_state === null || observations.includes(value.observed_state))
    && (value.checkpoint_hash === null || hash(value.checkpoint_hash))
    && (!["paused", "stopped"].includes(value.queue_state) || (value.control_enabled && value.checkpoint_hash !== null));
}

function controlBatch(value, expectedId, detail = true) {
  protocol(exact(value, ["schema", "batch_id", "requested_by", "status", "outcome", "accepted", "reused", "total", "settled_plans", "queue_counts", "outcomes",
    "requires_attention", "cancel_requested", "created_at", "deadline_at", ...(detail ? ["items"] : [])], ["cancel_requested_at"])
    && value.schema === "capability.batch_read/v2" && matches(value.batch_id, /^CB[a-f0-9]{28}$/, 30)
    && (expectedId === undefined || value.batch_id === expectedId) && integer(value.requested_by, 1)
    && value.accepted === true && typeof value.reused === "boolean" && integer(value.total, 1) && value.total <= 100
    && integer(value.settled_plans) && value.settled_plans <= value.total
    && typeof value.requires_attention === "boolean" && typeof value.cancel_requested === "boolean"
    && utc(value.created_at) && utc(value.deadline_at) && new Date(value.deadline_at) > new Date(value.created_at)
    && (value.cancel_requested_at === undefined || value.cancel_requested_at === null || utc(value.cancel_requested_at)));
  const countsValid = (counts, allowed) => exact(counts, [], allowed)
    && Object.values(counts).every((count) => integer(count, 1) && count <= value.total);
  protocol(countsValid(value.queue_counts, queues) && countsValid(value.outcomes, outcomes)
    && Object.values(value.queue_counts).reduce((sum, count) => sum + count, 0) === value.total
    && Object.values(value.outcomes).reduce((sum, count) => sum + count, 0) === value.settled_plans);
  if (detail) {
    protocol(Array.isArray(value.items) && value.items.length === value.total && value.items.every(batchMember)
      && new Set(value.items.map((item) => item.execution_id)).size === value.total && new Set(value.items.map((item) => item.plan_id)).size === value.total);
    const actualQueue = {}, actualOutcomes = {};
    for (const item of value.items) {
      actualQueue[item.queue_state] = (actualQueue[item.queue_state] || 0) + 1;
      if (item.outcome !== null) actualOutcomes[item.outcome] = (actualOutcomes[item.outcome] || 0) + 1;
    }
    protocol(queues.every((key) => (value.queue_counts[key] || 0) === (actualQueue[key] || 0))
      && outcomes.every((key) => (value.outcomes[key] || 0) === (actualOutcomes[key] || 0))
      && value.settled_plans === value.items.filter((item) => item.status === "settled").length);
  }
  const count = value.queue_counts;
  const status = count.reconciliation_required || count.blocked ? "reconciliation_required" : count.running ? "running" : count.queued ? "queued"
    : count.paused ? "paused" : count.stopped ? "stopped" : "settled";
  let outcome = null;
  if (status === "settled") {
    outcome = count.archived === value.total && value.outcomes.succeeded === value.total ? "succeeded"
      : count.cancelled === value.total ? "cancelled" : count.expired === value.total ? "expired"
        : Object.keys(value.outcomes).length === 1 && value.settled_plans === value.total ? Object.keys(value.outcomes)[0] : "partial";
  }
  protocol(value.status === status && value.outcome === outcome
    && value.requires_attention === (status === "reconciliation_required" || Boolean(value.outcomes.unknown)));
  protocol(new globalThis.TextEncoder().encode(JSON.stringify(value)).length <= 262144);
  return value;
}

// Explicit independent v2 read; never a fallback or retry for the v1 workspace.
export function getAssistantControlBatch(batchId, { authSession } = {}) {
  input(matches(batchId, /^CB[a-f0-9]{28}$/, 30));
  return api.get(`/assistant/batches/${batchId}`, { headers: { "X-Assistant-Batch-Version": "2" }, preserveErrorData: true,
    ...(authSession === undefined ? {} : { authSession }),
  })
    .then((value) => controlBatch(value, batchId)).catch(failure);
}

export function listAssistantControlBatches(params = {}, { authSession } = {}) {
  input(exact(params, [], ["page", "page_size"]) && (params.page === undefined || (integer(params.page, 1) && params.page <= 1000000))
    && (params.page_size === undefined || (integer(params.page_size, 1) && params.page_size <= 100)));
  const query = { page: params.page ?? 1, page_size: params.page_size ?? 10 };
  return api.get(`/assistant/batches${buildQuery(query)}`, { headers: { "X-Assistant-Batch-Version": "2" }, preserveErrorData: true,
    ...(authSession === undefined ? {} : { authSession }),
  }).then((value) => {
    protocol(exact(value, ["schema", "items", "total", "page", "page_size"]) && value.schema === "capability.batch_list/v2"
      && integer(value.total) && value.page === query.page && value.page_size === query.page_size
      && Array.isArray(value.items) && value.items.length === Math.min(query.page_size, Math.max(0, value.total - (query.page - 1) * query.page_size)));
    value.items.forEach((item) => controlBatch(item, undefined, false));
    protocol(new Set(value.items.map((item) => item.batch_id)).size === value.items.length);
    protocol(new globalThis.TextEncoder().encode(JSON.stringify(value)).length <= 262144);
    return value;
  }).catch(failure);
}
