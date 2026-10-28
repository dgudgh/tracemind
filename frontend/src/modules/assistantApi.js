import { api, buildQuery } from "../api";

const base = "/assistant";
const capabilityPattern = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2}$/;
const planPattern = /^pl[a-f0-9]{26}$/;
const executionBatchPattern = /^CB[a-f0-9]{28}$/;
const executionQueueStates = new Set(["queued", "running", "archived", "reconciliation_required", "cancelled", "expired", "blocked", "paused", "stopped"]);
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const validPlanId = (value) => typeof value === "string" && value.length === 28 && planPattern.test(value);
const boundedState = (value) => typeof value === "string" && value.length > 0 && value.length <= 24;
const planState = (value) => record(value) && validPlanId(value.plan_id)
  && boundedState(value.status) && (value.outcome === null || boundedState(value.outcome));
const planEnvelope = (value) => planState(value) && value.schema === "capability.plan_read/v1"
  && (!Object.hasOwn(value, "revision") || (Number.isSafeInteger(value.revision) && value.revision >= 1))
  && (!Object.hasOwn(value, "binding_hash") || validBinding(value.binding_hash))
  && (!Object.hasOwn(value, "expires_at") || value.expires_at === null || utcTimestamp(value.expires_at))
  && (!Object.hasOwn(value, "approval_available") || typeof value.approval_available === "boolean")
  && (!Object.hasOwn(value, "approved_by") || value.approved_by === null || positiveInteger(value.approved_by))
  && (!Object.hasOwn(value, "approval_expires_at") || value.approval_expires_at === null || utcTimestamp(value.approval_expires_at))
  && (Object.hasOwn(value, "execution_batch_id") === Object.hasOwn(value, "execution_queue_state"))
  && (!Object.hasOwn(value, "execution_batch_id")
    || (typeof value.execution_batch_id === "string" && executionBatchPattern.test(value.execution_batch_id)
      && executionQueueStates.has(value.execution_queue_state)))
  && Array.isArray(value.steps) && value.steps.length <= 20
  && value.steps.every((step) => record(step) && boundedState(step.status));

function protocol(value, valid) {
  if (!valid) throw Object.assign(new Error("Invalid assistant API response"), { code: "assistant_response_invalid" });
  return value;
}

function readPlan(value, expectedId) {
  return protocol(value, planEnvelope(value) && (expectedId === undefined || value.plan_id === expectedId));
}

function capabilityPath(value) {
  if (typeof value !== "string" || value.length > 64 || capabilityPattern.exec(value)?.[0] !== value) {
    throw new TypeError("Invalid assistant capability ID");
  }
  return value;
}

function planPath(value) {
  if (!validPlanId(value)) {
    throw new TypeError("Invalid assistant plan ID");
  }
  return value;
}

function utcTimestamp(value) {
  if (typeof value !== "string" || value.length > 32
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return false;
  const time = new Date(value);
  return Number.isFinite(time.getTime()) && time.toISOString().slice(0, 19) === value.slice(0, 19);
}

function validationEnvelope(value, planId) {
  const fields = ["schema", "plan_id", "revision", "checked_at", "plan_status", "can_execute", "issues", "approval_available", "requires_recheck"];
  return record(value) && Object.keys(value).length === fields.length && fields.every((key) => Object.hasOwn(value, key))
    && value.schema === "capability.plan_validation/v1" && value.plan_id === planId && validPlanId(value.plan_id)
    && Number.isSafeInteger(value.revision) && value.revision >= 1 && utcTimestamp(value.checked_at)
    && ["draft", "validated", "awaiting_approval", "approved", "running", "settled"].includes(value.plan_status)
    && typeof value.can_execute === "boolean" && typeof value.approval_available === "boolean" && value.requires_recheck === true
    && Array.isArray(value.issues) && value.issues.length <= 100
    && value.issues.every((issue) => record(issue) && Object.keys(issue).length === 1 && Object.hasOwn(issue, "code")
      && typeof issue.code === "string" && issue.code.length <= 64 && /^[a-z][a-z0-9_]*$/.exec(issue.code)?.[0] === issue.code)
    && (!value.can_execute || (["validated", "approved"].includes(value.plan_status) && value.issues.length === 0));
}

const positiveInteger = (value) => Number.isSafeInteger(value) && value > 0;
const validBinding = (value) => typeof value === "string" && value.length === 64 && /^[a-f0-9]{64}$/.test(value);
const validCapabilityId = (value) => typeof value === "string" && capabilityPattern.exec(value)?.[0] === value;
const validDefinitionVersion = (value) => Number.isInteger(value) && value >= 1 && value <= 100;
const paramName = /^[a-z][a-z0-9_]{0,63}$/;
const dangerousParam = new Set(["sql", "command", "script", "shell", "url", "expr", "path", "query"]);
const safeParam = (key, value) => !dangerousParam.has(key) && (value === null || typeof value === "boolean"
  || (typeof value === "string" && value.length <= 200)
  || (typeof value === "number" && Number.isFinite(value)))
  && (!["page", "page_size"].includes(key) || (Number.isInteger(value) && value >= 1));
const fieldsOnly = (value, fields) => record(value) && Object.keys(value).every((key) => fields.includes(key));

function decisionBody(payload, approve = false) {
  if (!fieldsOnly(payload, approve ? ["expected_revision", "expected_binding_hash", "valid_for_seconds"] : ["expected_revision"])
    || !positiveInteger(payload.expected_revision) || payload.expected_revision > 2147483646
    || (approve && (!validBinding(payload.expected_binding_hash)
      || (payload.valid_for_seconds !== undefined && (!Number.isInteger(payload.valid_for_seconds)
        || payload.valid_for_seconds < 60 || payload.valid_for_seconds > 3600))))) throw new TypeError("Invalid assistant approval input");
  return payload;
}

function planInput(payload, revision = false) {
  const fields = ["capability_id", "definition_version", "instance_id", "params", "steps", "valid_for_seconds", "request_approval",
    ...(revision ? ["expected_revision"] : ["trigger_type", "parent_plan_id"])];
  const paramsValid = (params) => record(params) && Object.keys(params).length <= 20
    && Object.entries(params).every(([key, value]) => paramName.test(key) && safeParam(key, value));
  if (!fieldsOnly(payload, fields) || !validCapabilityId(payload.capability_id) || payload.capability_id === "inspection.run.execute"
    || !validDefinitionVersion(payload.definition_version)
    || (payload.instance_id !== undefined && payload.instance_id !== null && !positiveInteger(payload.instance_id))
    || (payload.valid_for_seconds !== undefined && (!Number.isInteger(payload.valid_for_seconds) || payload.valid_for_seconds < 60 || payload.valid_for_seconds > 86400))
    || (payload.request_approval !== undefined && typeof payload.request_approval !== "boolean")
    || (payload.params !== undefined && !paramsValid(payload.params))
    || (payload.trigger_type !== undefined && !["manual", "assistant"].includes(payload.trigger_type))
    || (payload.parent_plan_id !== undefined && !validPlanId(payload.parent_plan_id))
    || (revision && (!positiveInteger(payload.expected_revision) || payload.expected_revision > 2147483646))) throw new TypeError("Invalid assistant plan input");
  if (payload.steps !== undefined) {
    if (!Array.isArray(payload.steps) || payload.steps.length < 1 || payload.steps.length > 20
      || (payload.params && Object.keys(payload.params).length)
      || payload.steps[0]?.capability_id !== payload.capability_id || payload.steps[0]?.definition_version !== payload.definition_version
      || !payload.steps.every((step, index) => fieldsOnly(step, ["seq", "title", "capability_id", "definition_version", "params", "depends_on", "input_refs", "fanout", "retries"])
        && step.seq === index + 1 && validCapabilityId(step.capability_id)
        && step.capability_id !== "inspection.run.execute" && validDefinitionVersion(step.definition_version)
        && typeof step.title === "string" && step.title.length > 0 && step.title.length <= 200
        && paramsValid(step.params)
        && Array.isArray(step.depends_on) && new Set(step.depends_on).size === step.depends_on.length
        && step.depends_on.every((seq) => Number.isInteger(seq) && seq >= 1 && seq < step.seq)
        && (step.input_refs === undefined || (record(step.input_refs) && Object.keys(step.input_refs).length === 0))
        && (step.fanout === undefined || step.fanout === 1) && (step.retries === undefined || step.retries === 0))) throw new TypeError("Invalid assistant plan steps");
  }
  return payload;
}

function governedPlan(value, expectedId) {
  readPlan(value, expectedId);
  return protocol(value, positiveInteger(value.revision) && validBinding(value.binding_hash)
    && utcTimestamp(value.expires_at) && typeof value.approval_available === "boolean"
    && positiveInteger(value.requested_by) && (value.instance_id === null || positiveInteger(value.instance_id))
    && ["draft", "validated", "awaiting_approval", "approved", "running", "settled"].includes(value.status)
    && ["not_required", "pending", "approved", "revoked"].includes(value.approval_state)
    && value.steps.length >= 1 && value.steps.every((step, index) => step.seq === index + 1
      && typeof step.capability_id === "string" && step.capability_id.length <= 64 && capabilityPattern.exec(step.capability_id)?.[0] === step.capability_id
      && positiveInteger(step.definition_version) && typeof step.title === "string" && step.title.length <= 200
      && record(step.params) && record(step.input_refs) && Array.isArray(step.depends_on)
      && new Set(step.depends_on).size === step.depends_on.length
      && step.depends_on.every((seq) => Number.isInteger(seq) && seq >= 1 && seq < step.seq))
    && value.steps[0].capability_id === value.capability_id && value.steps[0].definition_version === value.definition_version
    && (value.approved_by === null || positiveInteger(value.approved_by))
    && (value.approval_expires_at === null || utcTimestamp(value.approval_expires_at)));
}

export function getAssistantPlanApproval(planId) {
  return api.get(`${base}/plans/${planPath(planId)}/approval`).then((value) => {
    protocol(value, fieldsOnly(value, ["schema", "plan", "required_approvals", "decisions"])
      && value.schema === "capability.plan_approval/v1" && Number.isInteger(value.required_approvals)
      && value.required_approvals >= 0 && value.required_approvals <= 20 && Array.isArray(value.decisions)
      && value.decisions.length <= 20 && value.decisions.every((item) => fieldsOnly(item, ["actor_id", "approved_at", "expires_at"])
        && positiveInteger(item.actor_id) && utcTimestamp(item.approved_at) && utcTimestamp(item.expires_at))
      && new Set(value.decisions.map((item) => item.actor_id)).size === value.decisions.length);
    governedPlan(value.plan, planId);
    return value;
  });
}

function governancePost(planId, suffix, payload, { authSession } = {}) {
  return api.request(`${base}/plans/${planPath(planId)}/${suffix}`, { method: "POST", ...(authSession === undefined ? {} : { authSession }), body: JSON.stringify(payload) })
    .then((value) => {
      governedPlan(value, planId);
      return protocol(value, value.revision === payload.expected_revision + (suffix === "revisions" ? 1 : 0)
        && (payload.expected_binding_hash === undefined || value.binding_hash === payload.expected_binding_hash));
    });
}

export function reviseAssistantPlan(planId, payload, options) {
  planPath(planId);
  planInput(payload, true);
  return governancePost(planId, "revisions", payload, options).then((value) => protocol(value, value.revision === payload.expected_revision + 1));
}

export function requestAssistantPlanApproval(planId, payload, options) {
  planPath(planId);
  return governancePost(planId, "approval/request", decisionBody(payload), options);
}

export function approveAssistantPlan(planId, payload, options) {
  planPath(planId);
  return governancePost(planId, "approval/approve", decisionBody(payload, true), options);
}

export function revokeAssistantPlanApproval(planId, payload, options) {
  planPath(planId);
  return governancePost(planId, "approval/revoke", decisionBody(payload), options);
}

export function listAssistantCapabilities(params = {}) {
  return api.get(`${base}/capabilities${buildQuery(params)}`).then((value) => protocol(value,
    record(value) && value.schema === "capability.registry/v1" && Array.isArray(value.items)
    && value.items.every(record)));
}

export function getAssistantCapability(capability) {
  return api.get(`${base}/capabilities/${capabilityPath(capability)}`).then((value) => protocol(value,
    record(value) && value.capability_id === capability
    && Number.isInteger(value.definition_version) && value.definition_version > 0));
}

export async function createAssistantPlan(payload, { idempotencyKey, authSession } = {}) {
  planInput(payload);
  const headers = {};
  if (idempotencyKey !== undefined) {
    if (typeof idempotencyKey !== "string" || idempotencyKey.length < 1 || idempotencyKey.length > 128
      || /[^A-Za-z0-9._:-]/.test(idempotencyKey)) {
      throw new TypeError("Invalid assistant idempotency key");
    }
    headers["Idempotency-Key"] = idempotencyKey;
  }
  try {
    return readPlan(await api.request(`${base}/plans`, {
      method: "POST", body: JSON.stringify(payload), headers, ...(authSession === undefined ? {} : { authSession }), preserveErrorData: true,
    }));
  } catch (error) {
    const plan = error?.responseData;
    // A saved missing-input plan is a typed response, not generic validation success.
    if (error?.status === 422 && planEnvelope(plan)
      && plan.status === "settled" && plan.outcome === "needs_input"
      && Array.isArray(plan.missing_params) && plan.missing_params.length > 0
      && plan.missing_params.every((field) => typeof field === "string")) {
      return plan;
    }
    throw error;
  }
}

export function listAssistantPlans(params = {}) {
  return api.get(`${base}/plans${buildQuery(params)}`).then((value) => protocol(value,
    record(value) && value.schema === "capability.plan_list/v1" && Array.isArray(value.items)
    && Number.isSafeInteger(value.total) && value.total >= 0
    && Number.isInteger(value.page) && value.page >= 1 && value.page <= 1000000
    && Number.isInteger(value.page_size) && value.page_size >= 1 && value.page_size <= 100
    && value.items.length <= value.page_size && value.items.every(planState)));
}

export function getAssistantPlan(planId) {
  return api.get(`${base}/plans/${planPath(planId)}`).then((value) => readPlan(value, planId));
}

export function getAssistantPlanValidation(planId) {
  return api.get(`${base}/plans/${planPath(planId)}/validation`).then((value) => protocol(value, validationEnvelope(value, planId)));
}

export function executeAssistantPlan(planId, { authSession } = {}) {
  return api.request(`${base}/plans/${planPath(planId)}/execute`, {
    method: "POST", body: JSON.stringify({}), ...(authSession === undefined ? {} : { authSession }),
  }).then((value) => readPlan(value, planId));
}
