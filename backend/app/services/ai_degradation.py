"""One closed vocabulary for AI degradation. Pure functions: no IO, no provider imports."""
from __future__ import annotations

AI_MODEL_OK = "ok"
AI_NOT_CONFIGURED = "not_configured"
AI_MODEL_FAILED = "model_failed"
AI_OUTPUT_REJECTED = "output_rejected"
AI_NOT_SENT = "not_sent"

STATUSES = frozenset({AI_MODEL_OK, AI_NOT_CONFIGURED, AI_MODEL_FAILED, AI_OUTPUT_REJECTED, AI_NOT_SENT})

# Closed set: a response may never carry a free-text reason such as a raw exception message.
REASONS = frozenset({
    "provider_unavailable", "timeout", "generation_incomplete", "invalid_output",
    "no_evidence", "consent_required", "not_applicable",
})

_ERROR_CODES = {
    "no_supporting_evidence": (AI_MODEL_FAILED, "no_evidence"),
    "generation_incomplete": (AI_MODEL_FAILED, "generation_incomplete"),
    "provider_unavailable": (AI_MODEL_FAILED, "provider_unavailable"),
    "timeout": (AI_MODEL_FAILED, "timeout"),
    "context_unavailable": (AI_NOT_SENT, "not_applicable"),
    "context_exceeded": (AI_NOT_SENT, "not_applicable"),
    "invalid_output": (AI_OUTPUT_REJECTED, "invalid_output"),
    "invalid_response": (AI_OUTPUT_REJECTED, "invalid_output"),
    "empty_response": (AI_OUTPUT_REJECTED, "invalid_output"),
    "model_refusal": (AI_OUTPUT_REJECTED, "invalid_output"),
    "ValueError": (AI_OUTPUT_REJECTED, "invalid_output"),
}


def degradation(status: str, *, local_result_available: bool, reason_code: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown AI degradation status: {status!r}")
    if reason_code is not None and reason_code not in REASONS:
        raise ValueError(f"unknown AI degradation reason_code: {reason_code!r}")
    return {"status": status, "local_result_available": bool(local_result_available), "reason_code": reason_code}


def from_error_code(error_code: str | None, *, local_result_available: bool) -> dict:
    """Map an existing internal error code onto the vocabulary; never passes the code through."""
    if not error_code:
        return degradation(AI_MODEL_OK, local_result_available=local_result_available)
    status, reason = _ERROR_CODES.get(error_code, (AI_MODEL_FAILED, "provider_unavailable"))
    return degradation(status, local_result_available=local_result_available, reason_code=reason)


# A degraded AI answer must hand the user to a model-free path, not leave them waiting.
# Each step names the capability that actually serves the manual equivalent (None where the
# manual path is a page-level review of evidence already returned). A test resolves every
# non-null value against the registry, so deleting a manual capability breaks the build
# instead of leaving a dead hint in the UI.
MANUAL_STEPS = {
    "diagnosis.evidence_review": None,
    "awr.report_read": "performance.awr.query",
    "slow_sql.raw_list": "performance.slow_sql.read",
    "operations.capability_matrix": "operations.capability_matrix.read",
    "workflow.manual_draft": None,
}


def manual_handoff(step: str) -> dict:
    if step not in MANUAL_STEPS:
        raise ValueError(f"unknown manual step: {step!r}")
    return {"required": True, "step": step, "capability_id": MANUAL_STEPS[step], "model_free": True}


def no_manual() -> dict:
    return {"required": False, "step": None, "capability_id": None, "model_free": True}


def manual_for(state: dict, step: str) -> dict:
    """The manual next step for a degradation state: required unless the model answered."""
    if state.get("status") not in STATUSES:
        raise ValueError("manual_for needs a degradation state")
    return no_manual() if state["status"] == AI_MODEL_OK else manual_handoff(step)
