/*
 * C10 frontend contract, 2026-10-20.
 * Manual, permission-filtered fixed recipes only. Definitions supply typed slot
 * schemas/defaults and fixed steps; the client never supplies a DAG or script.
 * Catalog/detail/match are pure discovery. Match is an explicit suggestion POST,
 * never preparation/admission/execution. Every POST carries its captured session.
 * Prepare is one atomic request for global or 1..100 explicit ordered targets.
 * Its strict workflow_preparation receipt contains only the ten C06 summaries.
 * Fresh ordinary plan GETs recheck revision/hash/approval before explicit batch
 * admission. Admission reuses assistantBatchApi, not a browser execution loop.
 * Lost POST replies lock the draft; recovery uses only the original-key GET.
 * Session storage holds bounded keys/IDs/hashes and owner/login-epoch identity,
 * never tokens, raw text, slots, result bodies, SQL, paths or arbitrary payloads.
 * Reload recovery is owner + login-epoch gated (generation within a navigation).
 * Closing/unmounting/auth changes invalidate asynchronous UI updates; reopening
 * cannot dispatch old confirmations. Explicit new drafts are independent of the
 * original plan/approval. Dedicated workflow revisions use typed
 * slots/scope + expected_revision only; lost CAS replies are reconciled by GET,
 * next revision + canonical candidate hash, never by repeating the POST.
 * Revision recovery reads only the saved plan/frozen binding, not today's catalog;
 * choosing a source for new revision/rerun still checks the current definition.
 * Workspace props: open, onClose, instances=[], onOpenPlan(planId), initialPlan=null
 * (plan ID or plan-read hint), initialAction='prepare'|'revise'|'rerun'. Source
 * launches are read-only until explicit confirmation; hints never replace GET.
 * Main owns parent wiring/browser acceptance; backend owners own authorization,
 * binding, atomicity and workflow revision. No optimistic success or retry POST.
 */
import { api } from "../api";

export const WORKFLOW_NAMES = Object.freeze({
  "inspection.jobs.list": "已保存巡检作业",
  "inspection.templates.list": "巡检模板",
  "operations.capabilities.list": "引擎能力",
  "inspection.context.read": "巡检上下文",
  "inspection.collect": "巡检采集",
});
export const WORKFLOW_SUMMARY_FIELDS = Object.freeze(["plan_id", "instance_id", "revision", "binding_hash", "status", "outcome", "approval_state", "approval_available", "expires_at", "reused"]);
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const exact = (value, fields, optional = []) => record(value) && fields.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => fields.includes(key) || optional.includes(key));
const integer = (value, min = 1, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
const matches = (value, regex) => typeof value === "string" && regex.exec(value)?.[0] === value;
const hash = (value) => matches(value, /^[a-f0-9]{64}$/);
export const isWorkflowPlanId = (value) => matches(value, /^pl[a-f0-9]{26}$/);
const key = (value) => matches(value, /^[A-Za-z0-9._:-]{1,128}$/);
const states = ["draft", "validated", "awaiting_approval", "approved", "running", "settled"];
const outcomes = ["succeeded", "partial", "failed", "cancelled", "expired", "unknown", "plan_only", "blocked", "unsupported", "needs_input"];
const approvals = ["not_required", "pending", "approved", "revoked"];
const fields = ["page", "page_size", "status", "outcome", "trigger_type", "db_type", "enabled", "engine", "policy_id", "timeout_seconds"];
const recipeSteps = {
  "inspection.jobs.list": ["inspection.run.query"], "inspection.templates.list": ["inspection.template.read"],
  "operations.capabilities.list": ["operations.capability_matrix.read"],
  "inspection.context.read": ["operations.capability_matrix.read", "inspection.template.read", "inspection.run.query"],
  "inspection.collect": ["inspection.run.execute"],
};
const reference = (value) => exact(value, ["workflow_id", "workflow_version", "definition_hash"])
  && Object.hasOwn(WORKFLOW_NAMES, value.workflow_id) && integer(value.workflow_version, 1, 2147483647) && hash(value.definition_hash);
export const workflowReference = (definition) => Object.fromEntries(["workflow_id", "workflow_version", "definition_hash"].map((field) => [field, definition[field]]));
export const sameWorkflow = (left, right) => Boolean(left && right && ["workflow_id", "workflow_version", "definition_hash"].every((name) => left[name] === right[name]));
function input(valid) { if (!valid) throw new TypeError("Invalid workflow input"); }
function protocol(value, valid) {
  if (!valid) throw Object.assign(new Error("Invalid workflow response"), { code: "assistant_workflow_response_invalid" });
  return value;
}
function utc(value) {
  return matches(value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/)
    && Number.isFinite(new Date(value).getTime()) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
}
function summary(value) {
  return exact(value, WORKFLOW_SUMMARY_FIELDS) && isWorkflowPlanId(value.plan_id)
    && (value.instance_id === null || integer(value.instance_id)) && integer(value.revision, 1, 2147483647) && hash(value.binding_hash)
    && states.includes(value.status) && (value.outcome === null || outcomes.includes(value.outcome))
    && ((value.status === "settled") === (value.outcome !== null)) && approvals.includes(value.approval_state)
    && typeof value.approval_available === "boolean" && typeof value.reused === "boolean" && utc(value.expires_at);
}
function schemaValid(schema, grouped = false) {
  return record(schema) && schema.type === "object" && schema.additionalProperties === false && record(schema.properties)
    && Object.keys(schema.properties).length <= 10 && (schema.required === undefined || (Array.isArray(schema.required)
      && schema.required.every((field) => Object.hasOwn(schema.properties, field))))
    && Object.entries(schema.properties).every(([name, field]) => grouped
      ? ["matrix", "templates", "jobs"].includes(name) && schemaValid(field)
      : fields.includes(name) && record(field) && (field.anyOf === undefined || (Array.isArray(field.anyOf) && field.anyOf.length <= 2 && field.anyOf.every(record)))
        && (field.type === "integer" || (field.anyOf || [field]).some((part) => ["string", "boolean"].includes(part.type)))
        && (field["x-csv-values"] === undefined || (Array.isArray(field["x-csv-values"]) && field["x-csv-values"].length <= 20
          && field["x-csv-values"].every((token) => matches(token, /^[a-z][a-z0-9_]{0,23}$/)))));
}
export function validateWorkflowDefinition(value) {
  const required = ["schema", "workflow_id", "workflow_version", "definition_hash", "title", "slots_schema", "scope_modes", "steps", "max_targets", "failure_policy", "valid_for_seconds", "request_approval", "runtime_checks_required", "release_gate"];
  protocol(value, exact(value, required) && value.schema === "capability.workflow/v1" && reference(workflowReference(value))
    && typeof value.title === "string" && value.title.length <= 200 && schemaValid(value.slots_schema, value.workflow_id === "inspection.context.read")
    && slotsValid(value.slots_schema, workflowDefaults(value.slots_schema), true)
    && Array.isArray(value.scope_modes) && value.scope_modes.length >= 1 && value.scope_modes.length <= 2
    && new Set(value.scope_modes).size === value.scope_modes.length && value.scope_modes.every((mode) => ["global", "explicit"].includes(mode))
    && (!["inspection.context.read", "inspection.collect"].includes(value.workflow_id) || !value.scope_modes.includes("global"))
    && value.max_targets === 100 && value.failure_policy === "stop" && integer(value.valid_for_seconds, 60, 86400)
    && typeof value.request_approval === "boolean" && value.runtime_checks_required === true
    && [null, "inspection_plan"].includes(value.release_gate) && Array.isArray(value.steps) && value.steps.length >= 1 && value.steps.length <= 3
    && JSON.stringify(value.steps.map((step) => step?.capability_id)) === JSON.stringify(recipeSteps[value.workflow_id])
    && value.steps.every((step, index) => exact(step, ["seq", "capability_id", "definition_version", "slot_group", "depends_on", "input_refs", "fanout", "retries"])
      && step.seq === index + 1 && ["inspection.run.query", "inspection.template.read", "operations.capability_matrix.read", "inspection.run.execute"].includes(step.capability_id)
      && step.definition_version === (step.capability_id === "inspection.run.execute" ? 2 : 1)
      && (step.slot_group === null || ["matrix", "templates", "jobs"].includes(step.slot_group))
      && JSON.stringify(step.depends_on) === JSON.stringify(index ? [index] : [])
      && exact(step.input_refs, []) && step.fanout === 1 && step.retries === 0));
  return value;
}
export function workflowDefaults(schema) {
  return Object.fromEntries(Object.entries(schema.properties).flatMap(([name, field]) => field.type === "object"
    ? [[name, workflowDefaults(field)]] : Object.hasOwn(field, "default") ? [[name, field.default]] : []));
}
function slotsValid(schema, slots, partial = false) {
  if (!exact(slots, [], Object.keys(schema.properties))) return false;
  if (!partial && (schema.required || []).some((name) => !Object.hasOwn(slots, name))) return false;
  return Object.entries(slots).every(([name, value]) => {
    const field = schema.properties[name], choices = field.anyOf || [field];
    if (value === null) return choices.some((part) => part.type === "null");
    if (field.type === "object") return slotsValid(field, value, partial);
    if (field.type === "integer") return integer(value, field.minimum ?? (field.exclusiveMinimum !== undefined ? field.exclusiveMinimum + 1 : 1), field.maximum ?? Number.MAX_SAFE_INTEGER);
    if (choices.some((part) => part.type === "boolean")) return typeof value === "boolean";
    const text = choices.find((part) => part.type === "string");
    if (!text || typeof value !== "string" || value.length > (text.maxLength ?? 300)) return false;
    if (name === "engine" || name === "db_type") return ["oracle", "mysql", "postgresql", "sqlserver", ...(name === "db_type" ? ["host"] : [])].includes(value);
    if (field["x-csv-values"]) {
      const tokens = value.split(",").map((part) => part.trim()).filter(Boolean);
      return tokens.every((token) => field["x-csv-values"].includes(token));
    }
    return !/[\u0000-\u001f\u007f]/.test(value);
  });
}
export function validateWorkflowInput(definition, payload) {
  validateWorkflowDefinition(definition);
  input(exact(payload, ["operation", "slots", "scope"], ["source_plan_id"]) && ["prepare", "rerun"].includes(payload.operation)
    && (payload.operation === "rerun" ? isWorkflowPlanId(payload.source_plan_id) : payload.source_plan_id === undefined || payload.source_plan_id === null)
    && slotsValid(definition.slots_schema, payload.slots, true) && record(payload.scope) && definition.scope_modes.includes(payload.scope.mode));
  if (payload.scope.mode === "global") {
    input(exact(payload.scope, ["mode"]) && slotsValid(definition.slots_schema, payload.slots));
  } else {
    const targets = payload.scope.targets;
    input(exact(payload.scope, ["mode", "targets"]) && Array.isArray(targets) && targets.length >= 1 && targets.length <= 100
      && new Set(targets.map((target) => target.instance_id)).size === targets.length);
    for (const target of targets) {
      input(exact(target, ["instance_id"], ["slots"]) && integer(target.instance_id)
        && slotsValid(definition.slots_schema, target.slots ?? {}, true));
      const local = target.slots ?? {};
      let merged = { ...payload.slots, ...local };
      if (definition.workflow_id === "inspection.context.read") {
        merged = {};
        for (const group of Object.keys(definition.slots_schema.properties)) {
          input(!Object.keys(local[group] || {}).some((name) => Object.hasOwn(payload.slots[group] || {}, name)));
          merged[group] = { ...(payload.slots[group] || {}), ...(local[group] || {}) };
        }
      } else input(!Object.keys(local).some((name) => Object.hasOwn(payload.slots, name)));
      input(slotsValid(definition.slots_schema, merged));
    }
  }
  input(new globalThis.TextEncoder().encode(JSON.stringify(payload)).length <= 65536);
  return payload;
}
function options({ authSession, idempotencyKey } = {}, needsKey = false) {
  input(record(authSession) && typeof authSession.token === "string" && authSession.token.length > 0
    && integer(authSession.generation, 0) && typeof authSession.loginEpoch === "string" && authSession.loginEpoch.length >= 16);
  if (needsKey) input(key(idempotencyKey));
  return { authSession, preserveErrorData: true, ...(needsKey ? { headers: { "Idempotency-Key": idempotencyKey } } : {}) };
}
export function listAssistantWorkflows(readOptions = {}) {
  return api.get("/assistant/workflows", readOptions).then((value) => {
    protocol(value, exact(value, ["schema", "items"]) && value.schema === "capability.workflow_catalog/v1"
      && Array.isArray(value.items) && value.items.length <= 5);
    value.items.forEach(validateWorkflowDefinition);
    return protocol(value, new Set(value.items.map((item) => `${item.workflow_id}:${item.workflow_version}`)).size === value.items.length);
  });
}
export function getAssistantWorkflow(id, version, readOptions = {}) {
  input(Object.hasOwn(WORKFLOW_NAMES, id) && integer(version, 1, 2147483647));
  return api.get(`/assistant/workflows/${id}/versions/${version}`, readOptions).then((value) => {
    validateWorkflowDefinition(value);
    return protocol(value, value.workflow_id === id && value.workflow_version === version);
  });
}
export function matchAssistantWorkflow(text, postOptions) {
  input(typeof text === "string" && text.trim().length > 0 && text.length <= 4000 && !/[\u0000-\u001f\u007f]/.test(text));
  return api.request("/assistant/workflows/match", { ...options(postOptions), method: "POST", body: JSON.stringify({ text, matcher_version: 1 }) }).then((value) => protocol(value,
    exact(value, ["schema", "matcher_version", "accepted", "normalized_text", "decision", "operation", "workflow", "missing_fields", "reason_code"])
    && value.schema === "capability.workflow_match/v1" && value.matcher_version === 1 && value.accepted === false
    && typeof value.normalized_text === "string" && value.normalized_text.length <= 4000
    && ["matched", "needs_input", "ambiguous", "not_matched", "rejected"].includes(value.decision)
    && [null, "navigate", "view_existing", "prepare", "rerun"].includes(value.operation)
    && (value.workflow === null || reference(value.workflow)) && Array.isArray(value.missing_fields) && value.missing_fields.length <= 32
    && value.missing_fields.every((field) => typeof field === "string" && field.length <= 100)
    && matches(value.reason_code, /^[a-z][a-z0-9_]{0,95}$/)));
}
export function validateWorkflowPreparation(value, expected) {
  protocol(value, exact(value, ["schema", "accepted", "status", "preparation_id", "workflow", "operation", "source_plan_id", "total", "items", "notice"])
    && value.schema === "capability.workflow_preparation/v1" && value.accepted === false && value.status === "prepared"
    && matches(value.preparation_id, /^CP[a-f0-9]{28}$/) && reference(value.workflow)
    && ["prepare", "rerun"].includes(value.operation)
    && (value.operation === "rerun" ? isWorkflowPlanId(value.source_plan_id) : value.source_plan_id === null)
    && integer(value.total, 1, 100) && Array.isArray(value.items) && value.items.length === value.total && value.items.every(summary)
    && new Set(value.items.map((item) => item.plan_id)).size === value.total
    && new Set(value.items.map((item) => item.instance_id)).size === value.total
    && (value.items.every((item) => item.instance_id !== null) || value.total === 1)
    && typeof value.notice === "string" && value.notice.length <= 1000);
  if (expected) protocol(value, sameWorkflow(value.workflow, expected.workflow) && value.operation === expected.operation
    && value.source_plan_id === expected.source_plan_id && value.total === expected.target_ids.length
    && value.items.every((item, index) => item.instance_id === expected.target_ids[index])
    && (!expected.preparation_id || value.preparation_id === expected.preparation_id)
    && (!expected.bindings || value.items.every((item, index) => ["plan_id", "instance_id", "revision", "binding_hash"].every((name) => item[name] === expected.bindings[index]?.[name]))));
  return value;
}
export function prepareAssistantWorkflow(definition, payload, postOptions) {
  validateWorkflowInput(definition, payload);
  const expected = { workflow: workflowReference(definition), operation: payload.operation, source_plan_id: payload.source_plan_id ?? null,
    target_ids: payload.scope.mode === "global" ? [null] : payload.scope.targets.map((target) => target.instance_id) };
  return api.request(`/assistant/workflows/${definition.workflow_id}/versions/${definition.workflow_version}/prepare`, {
    ...options(postOptions, true), method: "POST", body: JSON.stringify(payload),
  }).then((value) => validateWorkflowPreparation(value, expected));
}
export function getAssistantWorkflowPreparation({ idempotencyKey, expected, authSession } = {}) {
  input(key(idempotencyKey));
  return api.get("/assistant/workflows/preparation", { headers: { "Idempotency-Key": idempotencyKey }, authSession, preserveErrorData: true })
    .then((value) => validateWorkflowPreparation(value, expected));
}
export async function getAssistantWorkflowPlan(item, workflow, readOptions = {}) {
  input(summary(item) && reference(workflow));
  const value = await api.get(`/assistant/plans/${item.plan_id}`, readOptions);
  const projected = Object.fromEntries(WORKFLOW_SUMMARY_FIELDS.map((field) => [field, value?.[field]]));
  protocol(value, record(value) && value.schema === "capability.plan_read/v1" && summary(projected)
    && ["plan_id", "instance_id", "revision", "binding_hash"].every((name) => projected[name] === item[name])
    && sameWorkflow(value.workflow_binding, workflow));
  await validateWorkflowBoundPlan(value);
  return projected;
}
export function workflowCanonical(value) {
  const normalize = (item) => {
    if (Array.isArray(item)) return item.map(normalize);
    if (record(item)) return Object.fromEntries(Object.keys(item).sort().map((name) => [name, normalize(item[name])]));
    if (item === null || ["string", "boolean"].includes(typeof item) || Number.isSafeInteger(item)) return item;
    throw new TypeError("Invalid canonical workflow value");
  };
  return JSON.stringify(normalize(value)).replace(/[\u007f-\uffff]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
async function digest(value) {
  const bytes = await globalThis.crypto.subtle.digest("SHA-256", new globalThis.TextEncoder().encode(workflowCanonical(value)));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
function effectiveSlots(definition, slots, scope) {
  const local = scope.mode === "global" ? {} : scope.targets[0].slots || {};
  if (definition.workflow_id !== "inspection.context.read") return { ...workflowDefaults(definition.slots_schema), ...slots, ...local };
  return Object.fromEntries(Object.entries(definition.slots_schema.properties).map(([group, schema]) => [group, {
    ...workflowDefaults(schema), ...(slots[group] || {}), ...(local[group] || {}),
  }]));
}
export async function workflowCandidateHash(definition, slots, scope) {
  validateWorkflowInput(definition, { operation: "prepare", slots, scope });
  input(scope.mode === "global" || scope.targets.length === 1);
  const values = effectiveSlots(definition, slots, scope);
  return digest({ ...workflowReference(definition), scope_mode: scope.mode,
    effective_inputs: [{ instance_id: scope.mode === "global" ? null : scope.targets[0].instance_id,
      step_params: definition.steps.map((step) => step.slot_group ? values[step.slot_group] : values) }] });
}
export async function validateWorkflowBoundPlan(value) {
  const projected = Object.fromEntries(WORKFLOW_SUMMARY_FIELDS.map((field) => [field, value?.[field]]));
  protocol(value, record(value) && value.schema === "capability.plan_read/v1" && summary(projected)
    && exact(value.workflow_binding, ["workflow_id", "workflow_version", "definition_hash", "recipe_snapshot", "input", "candidate_hash"]));
  const binding = value.workflow_binding, definition = validateWorkflowDefinition(binding.recipe_snapshot);
  protocol(value, sameWorkflow(binding, definition) && exact(binding.input, ["slots", "scope"]) && hash(binding.candidate_hash));
  const { definition_hash: ignored, ...unsigned } = definition;
  protocol(value, await digest(unsigned) === binding.definition_hash);
  const { slots, scope } = binding.input;
  validateWorkflowInput(definition, { operation: "prepare", slots, scope });
  protocol(value, (scope.mode === "global" || scope.targets.length === 1)
    && (scope.mode === "global" ? value.instance_id === null : scope.targets[0].instance_id === value.instance_id)
    && await workflowCandidateHash(definition, slots, scope) === binding.candidate_hash
    && Array.isArray(value.steps) && value.steps.length === definition.steps.length);
  const effective = effectiveSlots(definition, slots, scope);
  for (let index = 0; index < definition.steps.length; index += 1) {
    const step = value.steps[index], skeleton = definition.steps[index];
    protocol(value, record(step) && record(step.params) && step.seq === skeleton.seq && step.capability_id === skeleton.capability_id
      && step.definition_version === skeleton.definition_version && workflowCanonical(step.depends_on) === workflowCanonical(skeleton.depends_on)
      && exact(step.input_refs, []));
    const params = { ...step.params };
    if (step.capability_id === "inspection.run.execute") for (const field of ["policy_revision", "runtime_digest", "target_fingerprint"]) {
      if (Object.hasOwn(params, field)) { protocol(value, hash(params[field])); delete params[field]; }
    }
    protocol(value, workflowCanonical(params) === workflowCanonical(skeleton.slot_group ? effective[skeleton.slot_group] : effective));
  }
  return { summary: projected, workflow: workflowReference(definition), definition, slots, scope, candidate_hash: binding.candidate_hash,
    rerunnable: value.status === "settled" && ["succeeded", "partial", "failed", "cancelled", "expired"].includes(value.outcome)
      && !value.steps.some((step) => step.outcome_kind === "unknown" || step.status === "unknown") };
}
export async function getAssistantWorkflowSource(initialPlan, readOptions = {}) {
  const id = typeof initialPlan === "string" ? initialPlan : initialPlan?.plan_id;
  input(isWorkflowPlanId(id));
  const value = await api.get(`/assistant/plans/${id}`, readOptions);
  protocol(value, value?.plan_id === id);
  const source = await validateWorkflowBoundPlan(value);
  if (record(initialPlan)) protocol(value, (initialPlan.revision === undefined || initialPlan.revision === source.summary.revision)
    && (initialPlan.binding_hash === undefined || initialPlan.binding_hash === source.summary.binding_hash));
  const current = await getAssistantWorkflow(source.workflow.workflow_id, source.workflow.workflow_version, readOptions);
  protocol(current, sameWorkflow(current, source.workflow));
  return source;
}
export async function reviseAssistantWorkflow(source, payload, postOptions) {
  input(record(source) && summary(source.summary) && exact(payload, ["expected_revision", "slots", "scope"])
    && payload.expected_revision === source.summary.revision);
  const expectedHash = await workflowCandidateHash(source.definition, payload.slots, payload.scope);
  const value = await api.request(`/assistant/workflows/plans/${source.summary.plan_id}/revisions`, {
    ...options(postOptions), method: "POST", body: JSON.stringify(payload),
  });
  const result = await validateWorkflowBoundPlan(value);
  protocol(value, result.summary.plan_id === source.summary.plan_id && result.summary.revision === payload.expected_revision + 1
    && result.summary.binding_hash !== source.summary.binding_hash && sameWorkflow(result.workflow, source.workflow)
    && result.candidate_hash === expectedHash && result.summary.approval_state !== "approved");
  return result;
}
export async function getAssistantWorkflowRevision(expected, readOptions = {}) {
  input(exact(expected, ["plan_id", "expected_revision", "candidate_hash"]) && isWorkflowPlanId(expected.plan_id)
    && integer(expected.expected_revision, 1, 2147483646) && hash(expected.candidate_hash));
  // A saved revision is recoverable from its frozen binding even after catalog withdrawal.
  const value = await api.get(`/assistant/plans/${expected.plan_id}`, readOptions);
  const result = await validateWorkflowBoundPlan(value);
  protocol(result, result.summary.plan_id === expected.plan_id && result.summary.revision === expected.expected_revision + 1
    && result.candidate_hash === expected.candidate_hash);
  return result;
}
export function workflowReady(item, now = Date.now()) {
  return summary(item) && item.outcome === null && new Date(item.expires_at).getTime() > now
    && ((item.status === "validated" && item.approval_state === "not_required")
      || (item.status === "approved" && item.approval_state === "approved" && item.approval_available === true));
}
export function newWorkflowKey(kind) {
  input(["prepare", "enqueue"].includes(kind));
  const bytes = new Uint8Array(16); globalThis.crypto.getRandomValues(bytes);
  return `${kind}:${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
export function workflowBindings(items) {
  return items.map(({ plan_id, instance_id, revision, binding_hash }) => ({ plan_id, instance_id, revision, binding_hash }));
}
export function validateWorkflowRecovery(value, now = Date.now()) {
  input(exact(value, ["schema", "owner", "login_epoch", "generation", "navigation_epoch", "created_at", "expires_at", "phase", "workflow", "operation", "source_plan_id", "target_ids", "keys", "preparation_id", "bindings", "batch_id"], ["revision"])
    && value.schema === "capability.workflow_recovery/v1" && exact(value.owner, ["user_id", "session_fingerprint"])
    && integer(value.owner.user_id) && hash(value.owner.session_fingerprint) && matches(value.login_epoch, /^[A-Za-z0-9._:-]{16,128}$/)
    && integer(value.generation, 0) && Number.isFinite(value.navigation_epoch) && value.navigation_epoch > 0
    && integer(value.created_at) && integer(value.expires_at, value.created_at + 1, value.created_at + 86400000)
    && value.created_at <= now + 60000 && value.expires_at > now && ["prepare", "enqueue", "revise"].includes(value.phase)
    && reference(value.workflow) && ["prepare", "rerun"].includes(value.operation)
    && (value.operation === "rerun" ? isWorkflowPlanId(value.source_plan_id) : value.source_plan_id === null)
    && Array.isArray(value.target_ids) && value.target_ids.length >= 1 && value.target_ids.length <= 100
    && new Set(value.target_ids).size === value.target_ids.length && value.target_ids.every((id) => integer(id) || (id === null && value.target_ids.length === 1))
    && exact(value.keys, ["prepare", "enqueue"]) && (key(value.keys.prepare) || (value.keys.prepare === null && record(value.revision)))
    && (value.keys.enqueue === null || (key(value.keys.enqueue) && value.keys.enqueue !== value.keys.prepare))
    && (value.preparation_id === null || matches(value.preparation_id, /^CP[a-f0-9]{28}$/))
    && (value.batch_id === null || matches(value.batch_id, /^CB[a-f0-9]{28}$/)));
  input(value.bindings === null || (Array.isArray(value.bindings) && value.bindings.length === value.target_ids.length
    && value.bindings.every((item, index) => exact(item, ["plan_id", "instance_id", "revision", "binding_hash"])
      && isWorkflowPlanId(item.plan_id) && item.instance_id === value.target_ids[index] && integer(item.revision) && hash(item.binding_hash))
    && new Set(value.bindings.map((item) => item.plan_id)).size === value.bindings.length));
  if (value.revision !== undefined) input(exact(value.revision, ["plan_id", "expected_revision", "candidate_hash"])
    && isWorkflowPlanId(value.revision.plan_id) && integer(value.revision.expected_revision, 1, 2147483646)
    && hash(value.revision.candidate_hash) && value.target_ids.length === 1 && value.keys.prepare === null);
  if (value.phase === "prepare") input(value.keys.enqueue === null && value.batch_id === null && value.revision === undefined);
  else if (value.phase === "revise") input(value.revision !== undefined && value.keys.enqueue === null && value.batch_id === null);
  else input(value.keys.enqueue !== null && (value.preparation_id !== null || value.revision !== undefined) && value.bindings !== null);
  input(JSON.stringify(value).length <= 32768);
  return value;
}
export function workflowRecoveryMatches(value, owner, session, navigationEpoch) {
  return value.owner.user_id === owner.user_id && value.owner.session_fingerprint === owner.session_fingerprint
    && value.login_epoch === session.loginEpoch && (value.navigation_epoch !== navigationEpoch || value.generation === session.generation);
}
