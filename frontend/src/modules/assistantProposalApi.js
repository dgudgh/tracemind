/*
 * C11 proposal client, 2026-10-21.
 * A proposal is one consented model call whose output is structure only: `accepted` is
 * always false and `requires_confirmation` is always true. Nothing here prepares, freezes,
 * approves or enqueues; adopting a candidate only fills the manual draft, which then goes
 * through the ordinary prepare → review/approval → explicit batch path.
 * The provider list exists because the consent binding needs `revision`, and `GET /ai-config`
 * neither exposes it nor is reachable without `platform.settings`. Only id/name/type/model/
 * revision reach the browser: never an endpoint, key material or timeout.
 * Lost replies lock the draft and are recovered by the original-idempotency-key GET, never
 * by repeating the POST. Every POST carries the captured session, and stale sessions cannot
 * update the view.
 */
import { api } from "../api";

export const PROVIDER_SCHEMA = "capability.workflow_providers/v1";
export const PROPOSAL_SCHEMA = "capability.workflow_proposal/v1";
export const CANDIDATE_SCHEMA = "capability.workflow_candidate/v1";
export const PROPOSAL_KEY = "mesguard.assistant.proposal-request.v1";
const PROVIDER_FIELDS = ["config_id", "name", "provider_type", "model", "revision", "is_default"];
const PROPOSAL_FIELDS = ["schema", "validator_version", "accepted", "model_decision", "decision", "workflow",
  "missing_fields", "candidate", "catalog_hash", "proposal_hash", "validation_scope", "requires_confirmation",
  "proposal_id", "provider"];
const CANDIDATE_FIELDS = ["schema", "accepted", "workflow_binding", "missing_fields", "scope", "slots",
  "plan_inputs", "effective_inputs", "candidate_hash"];
const DECISIONS = ["matched", "needs_input", "not_matched"];
const record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const exact = (value, fields) => record(value) && fields.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => fields.includes(key));
const integer = (value, min = 1, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
const matches = (value, regex) => typeof value === "string" && regex.exec(value)?.[0] === value;
const digest = (value) => matches(value, /^[a-f0-9]{64}$/);

function protocol(value, valid) {
  if (!valid) throw Object.assign(new Error("Invalid proposal response"), { code: "assistant_proposal_response_invalid" });
  return value;
}
function input(valid) {
  if (!valid) throw new TypeError("Invalid proposal input");
}
const cleanText = (value) => typeof value === "string" && value.length <= 4000 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);

export function newProposalKey() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `proposal:${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}
export const isProposalKey = (value) => matches(value, /^proposal:[a-f0-9]{32}$/);

export function validateProviders(value) {
  protocol(value, exact(value, ["schema", "items", "notice"]) && value.schema === PROVIDER_SCHEMA
    && Array.isArray(value.items) && value.items.length <= 20 && typeof value.notice === "string" && value.notice.length <= 500
    && value.items.every((item) => exact(item, PROVIDER_FIELDS) && integer(item.config_id) && typeof item.name === "string"
      && item.name.length <= 100 && typeof item.provider_type === "string" && typeof item.model === "string"
      && item.model.length <= 100 && digest(item.revision) && typeof item.is_default === "boolean")
    && new Set(value.items.map((item) => item.config_id)).size === value.items.length);
  return value;
}
export function listProposalProviders(readOptions = {}) {
  return api.get("/assistant/workflows/providers", readOptions).then(validateProviders);
}

export function validateScope(scope, lower = 0, upper = 100) {
  if (!record(scope)) return false;
  if (scope.mode === "global") return Object.keys(scope).length === 1;
  if (scope.mode !== "explicit") return false;
  const targets = scope.targets;
  return Object.keys(scope).every((key) => ["mode", "targets"].includes(key)) && Array.isArray(targets)
    && targets.length >= Math.max(lower, 0) && targets.length <= upper
    && new Set(targets.map((item) => item?.instance_id)).size === targets.length
    && targets.every((item) => record(item) && Object.hasOwn(item, "instance_id") && integer(item.instance_id)
      && (item.slots === undefined || record(item.slots)));
}

function validateCandidate(value) {
  return exact(value, CANDIDATE_FIELDS) && value.schema === CANDIDATE_SCHEMA && value.accepted === false
    && exact(value.workflow_binding, ["workflow_id", "workflow_version", "definition_hash", "recipe_snapshot"])
    && typeof value.workflow_binding.workflow_id === "string" && integer(value.workflow_binding.workflow_version)
    && digest(value.workflow_binding.definition_hash) && record(value.workflow_binding.recipe_snapshot)
    && Array.isArray(value.missing_fields) && value.missing_fields.every((field) => typeof field === "string")
    && validateScope(value.scope) && record(value.slots)
    && Array.isArray(value.plan_inputs) && value.plan_inputs.length <= 100
    && Array.isArray(value.effective_inputs) && value.effective_inputs.length === value.plan_inputs.length
    && digest(value.candidate_hash);
}

export function validateProposal(value) {
  protocol(value, exact(value, PROPOSAL_FIELDS) && value.schema === PROPOSAL_SCHEMA && value.validator_version === 1
    && value.accepted === false && value.requires_confirmation === true && value.validation_scope === "structure_only"
    && matches(value.proposal_id, /^WP[a-f0-9]{28}$/) && DECISIONS.includes(value.decision) && DECISIONS.includes(value.model_decision)
    && (value.workflow === null || (exact(value.workflow, ["workflow_id", "workflow_version", "definition_hash"])
      && typeof value.workflow.workflow_id === "string" && integer(value.workflow.workflow_version) && digest(value.workflow.definition_hash)))
    && Array.isArray(value.missing_fields) && value.missing_fields.length <= 64
    && value.missing_fields.every((field) => typeof field === "string" && field.length <= 100)
    && (value.candidate === null || validateCandidate(value.candidate))
    && (value.decision === "matched") === (value.candidate !== null)
    && digest(value.catalog_hash) && digest(value.proposal_hash)
    && exact(value.provider, ["config_id", "revision", "model"]) && integer(value.provider.config_id)
    && digest(value.provider.revision));
  return value;
}

export function validateProposalInput(payload) {
  input(exact(payload, ["text", "scope", "provider_config_id", "provider_revision", "allow_model_context",
    "confirmed_policy_id"]) && cleanText(payload.text) && payload.text.trim().length > 0
    && validateScope(payload.scope, 1, 100) && integer(payload.provider_config_id, 1, 2 ** 53 - 1)
    && digest(payload.provider_revision) && payload.allow_model_context === true
    && (payload.confirmed_policy_id === null || integer(payload.confirmed_policy_id, 1, 2 ** 53 - 1)));
  return payload;
}

function postOptions({ authSession, idempotencyKey } = {}) {
  input(record(authSession) && typeof authSession.token === "string" && authSession.token.length > 0
    && Number.isSafeInteger(authSession.generation) && typeof authSession.loginEpoch === "string")
  input(isProposalKey(idempotencyKey));
  return { authSession, preserveErrorData: true, headers: { "Idempotency-Key": idempotencyKey } };
}

export function requestProposal(payload, { authSession, idempotencyKey } = {}) {
  validateProposalInput(payload);
  return api.request("/assistant/workflows/proposals", {
    ...postOptions({ authSession, idempotencyKey }), method: "POST", body: JSON.stringify(payload),
  }).then(validateProposal);
}
export function getProposal({ authSession, idempotencyKey } = {}) {
  input(isProposalKey(idempotencyKey));
  return api.get("/assistant/workflows/proposals", {
    authSession, preserveErrorData: true, headers: { "Idempotency-Key": idempotencyKey },
  }).then(validateProposal);
}

/**
 * Map a validated candidate onto the manual draft. Pure: no request, no execution, no
 * defaulting of a target the user did not confirm. A withdrawn recipe makes adoption fail
 * closed instead of trusting the frozen snapshot's own hash.
 */
export function proposalAdoption(value, catalog) {
  validateProposal(value);
  const candidate = value.candidate;
  if (value.decision !== "matched" || candidate === null) throw new TypeError("Proposal has no adoptable candidate");
  const reference = value.workflow;
  const entry = (catalog || []).find((item) => item?.workflow_id === reference.workflow_id
    && item?.workflow_version === reference.workflow_version && item?.definition_hash === reference.definition_hash);
  if (!entry) throw Object.assign(new Error("Recipe is no longer in the authorized catalog"), { code: "assistant_proposal_catalog_changed" });
  return { reference, definition: entry, slots: candidate.slots, scope: candidate.scope, candidate_hash: candidate.candidate_hash };
}

export function scopeFromTargets(instanceIds) {
  const ids = instanceIds.filter((id) => Number.isSafeInteger(id) && id > 0);
  return ids.length ? { mode: "explicit", targets: ids.map((id) => ({ instance_id: id })) } : null;
}

export function readStoredProposalKey(storage = globalThis.sessionStorage) {
  try {
    const raw = storage?.getItem(PROPOSAL_KEY);
    if (raw === null || raw === undefined || raw.length > 400) return null;
    const value = JSON.parse(raw);
    return isProposalKey(value?.key) && typeof value?.login_epoch === "string" ? value : null;
  } catch { return null; }
}
export function storeProposalKey(key, loginEpoch, storage = globalThis.sessionStorage) {
  input(isProposalKey(key) && typeof loginEpoch === "string");
  storage.setItem(PROPOSAL_KEY, JSON.stringify({ key, login_epoch: loginEpoch }));
  if (readStoredProposalKey(storage)?.key !== key) throw new Error("Proposal key not persisted");
}
export function clearProposalKey(storage = globalThis.sessionStorage) {
  storage.removeItem(PROPOSAL_KEY);
}
