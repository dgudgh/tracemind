"""The one vocabulary for "what did this run actually do".

``DBInspectionJob.status`` records the *fact* (what the execution side wrote), and
``failure_reason`` records the explanation written that day. The **outcome** - the thing the
UI, the report centre and the AI context all need to agree on - is derived from those two here
and is deliberately **never stored**. A stored outcome would be a second source of truth, and
the two would drift the first time a code path forgot to update it.

Two rules make this module worth its own file:

1. **Anti-default-success.** Any status this module does not recognise becomes ``unknown``,
   never ``success``. The front end used to render ``outcome === "partial" ? ... : "success"``,
   which turned "no status field", "status is queued" and "status is a word I have never seen"
   into a green result.
2. **``unknown`` is information, not a failure.** ``timed_out`` and ``interrupted`` mean we do
   not know what the target side did: the result was never published, or the executing node
   vanished. Calling that ``failed`` asserts "definitely collected nothing", which is a claim
   we cannot support.

``plan_only``/``blocked``/``unsupported``/``needs_input`` are supported values, but only
:func:`declare` may produce them. :func:`normalize` can never return them, because a run that
was merely *planned* must not be summarised as an executed one.
"""
from __future__ import annotations

#: Statuses that end a run. Mirrors ``jobs.TERMINAL`` (kept here so this module stays free of
#: database imports and can be unit-tested as a pure function).
TERMINAL_STATUSES = frozenset(
    {"success", "partial", "failed", "cancelled", "timed_out", "interrupted", "expired"}
)
#: Statuses that mean "still running somewhere".
IN_PROGRESS_STATUSES = frozenset({"queued", "running"})
KNOWN_STATUSES = TERMINAL_STATUSES | IN_PROGRESS_STATUSES

#: Outcomes a run can have. ``plan_only``/``blocked``/``unsupported``/``needs_input`` are NOT
#: here on purpose - see DECLARED_OUTCOMES.
OUTCOMES = frozenset({"success", "partial", "failed", "cancelled", "expired", "unknown"})

#: Outcomes that only a producer can assert about itself. Never inferred from a job status.
DECLARED_OUTCOMES = frozenset({"plan_only", "blocked", "unsupported", "needs_input"})

#: Explicit classifications the execution side may attach to a failure. ``failure_code`` is
#: written into the terminal progress event, not into a new column.
FAILURE_CODES = frozenset(
    {
        "actor_inactive",
        "permission_revoked",
        "scope_revoked",
        "instance_disabled",
        "target_changed",
        "template_missing",
        "snapshot_invalid",
        "collector_error",
        "timeout",
        "lease_lost",
        "expired_in_queue",
        "cancelled_before_run",
        "cancelled_during_run",
        "cancelled_force_closed",
        "unrecognized_status",
        "unclassified_legacy",
    }
)

#: How far along a run is, by stage. Weights rather than counts: an inspection spends most of
#: its time collecting, so "validating" to "saving" is not 50% of the work.
STAGE_WEIGHTS = {"queued": 10, "validating": 30, "saving": 90}

# Canonical outcome per terminal status. Anything not listed is ``unknown``.
_STATUS_OUTCOME = {
    "success": "success",
    "partial": "partial",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",
    # Deliberately NOT "failed": we do not know what the target side did.
    "timed_out": "unknown",
    "interrupted": "unknown",
}
_DEFAULT_KIND = {
    "partial": "collector_error",
    "timed_out": "timeout",
    "interrupted": "lease_lost",
    "expired": "expired_in_queue",
}
_CANCEL_KINDS = frozenset(
    {"cancelled_before_run", "cancelled_during_run", "cancelled_force_closed"}
)
#: Cancellation kinds that still leave the target's state unproven, so a human must look.
_CANCEL_NEEDS_ATTENTION = frozenset({"cancelled_during_run", "cancelled_force_closed"})

#: Codes a ``failed`` run may carry. Everything else (including "no code at all") becomes
#: ``unclassified_legacy``: reporting a cause we were never told is worse than saying "unknown".
_FAILED_KINDS = frozenset(
    {
        "actor_inactive",
        "permission_revoked",
        "scope_revoked",
        "instance_disabled",
        "target_changed",
        "template_missing",
        "snapshot_invalid",
        "collector_error",
    }
)

SCHEMA_VERSION = "run_status/v1"


def results_published(stage: str | None, row_count: int) -> bool:
    """Whether this run published result rows.

    Not the same question as ``dispatch.PUBLISHED`` ("this run is worth a report"). A run can
    publish rows without being report-worthy, and vice versa.
    """
    return stage == "completed" and row_count > 0


def requires_attention(outcome: str | None, outcome_kind: str | None) -> bool:
    if outcome in {"unknown", "expired"}:
        return True
    return outcome_kind in _CANCEL_NEEDS_ATTENTION


def _percent(stage: str | None, status: str, terminal: bool, event_count: int | None):
    if terminal:
        return None, "terminal_no_percentage"
    if event_count is not None and event_count < 2:
        # Pre-control-plane jobs kept only the single "queued" event. Inventing a percentage
        # from it would be a fabrication, so the caller gets an explicit basis instead.
        return None, "missing_stage_events"
    weight = STAGE_WEIGHTS.get(str(stage or status))
    if weight is None:
        return None, "unknown_stage_weight"
    return weight, "stage_weights_v1"


def normalize(
    status: str | None,
    *,
    failure_code: str | None = None,
    stage: str | None = None,
    started: bool | None = None,
    event_count: int | None = None,
) -> dict:
    """Derive the externally visible outcome of one run.

    Args:
        status: ``DBInspectionJob.status`` verbatim - the authoritative stored fact.
        failure_code: explicit classification written by the producer into the terminal
            progress event. Never parsed out of a human-readable message.
        stage: ``DBInspectionJob.stage``; only used for in-progress percentage.
        started: whether the run ever got as far as ``running``. Used only to classify a
            cancellation with no recorded ``failure_code`` (jobs cancelled before this batch).
        event_count: number of progress events, used to refuse a fabricated percentage.

    Returns:
        A dict with ``status``, ``stage``, ``terminal``, ``outcome``, ``outcome_kind``,
        ``requires_attention``, ``percent`` and ``percent_basis``.
    """
    raw = status if isinstance(status, str) else None
    normalized = (raw or "").strip().lower() or None
    terminal = normalized in TERMINAL_STATUSES

    if normalized in IN_PROGRESS_STATUSES or normalized is None:
        # ``None`` means "no status recorded at all": that is not a success and it is not a
        # terminal state either - it is a run we know nothing about yet.
        if normalized is None:
            return {
                "schema": SCHEMA_VERSION,
                "status": raw,
                "stage": stage,
                "terminal": False,
                "outcome": "unknown",
                "outcome_kind": "unrecognized_status",
                "requires_attention": True,
                "percent": None,
                "percent_basis": "missing_status",
            }
        # Only a running job has moved past "queued"; a queued job that already carries a later
        # stage is an ordering artefact, not progress.
        stage_hint = stage if normalized == "running" and stage else normalized
        percent, basis = _percent(stage_hint, normalized, False, event_count)
        return {
            "schema": SCHEMA_VERSION,
            "status": raw,
            "stage": stage,
            "terminal": False,
            "outcome": None,
            "outcome_kind": None,
            "requires_attention": False,
            "percent": percent,
            "percent_basis": basis,
        }

    if normalized not in KNOWN_STATUSES:
        percent, basis = None, "terminal_no_percentage"
        return {
            "schema": SCHEMA_VERSION,
            "status": raw,
            "stage": stage,
            "terminal": False,
            "outcome": "unknown",
            "outcome_kind": "unrecognized_status",
            "requires_attention": True,
            "percent": percent,
            "percent_basis": basis,
        }

    outcome = _STATUS_OUTCOME[normalized]
    kind = _classify(normalized, failure_code, started)
    percent, basis = _percent(stage, normalized, True, event_count)
    return {
        "schema": SCHEMA_VERSION,
        "status": raw,
        "stage": stage,
        "terminal": True,
        "outcome": outcome,
        "outcome_kind": kind,
        "requires_attention": requires_attention(outcome, kind),
        "percent": percent,
        "percent_basis": basis,
    }


def _classify(status: str, failure_code: str | None, started: bool | None) -> str | None:
    code = failure_code if failure_code in FAILURE_CODES else None
    if status == "success":
        # A success carrying a failure code is a contradiction; the code wins for nobody.
        return None
    if status == "cancelled":
        if code in _CANCEL_KINDS:
            return code
        # Jobs cancelled before this batch recorded no code. Whether the run had started is
        # still provable from started_at, so do not guess "before_run" for both.
        return "cancelled_during_run" if started else "cancelled_before_run"
    if status == "failed":
        return code if code in _FAILED_KINDS else "unclassified_legacy"
    if code is not None:
        return code
    return _DEFAULT_KIND.get(status)


def outcome_of(status: str | None, **kwargs) -> str | None:
    """Convenience accessor used where only the outcome word is needed."""
    return normalize(status, **kwargs)["outcome"]


def is_declared(outcome: str | None) -> bool:
    return outcome in DECLARED_OUTCOMES


def declare(outcome: str, declared_by: str, *, kind: str | None = None, reason: str | None = None) -> dict:
    """Produce one of the four outcomes that cannot be inferred from a job status.

    ``plan_only``/``blocked``/``unsupported``/``needs_input`` all mean "no evidence that this
    ran". They are only legal when the producer says so explicitly and names itself: that is
    the difference between "the platform decided not to execute" and "the platform executed and
    quietly reported nothing".

    Raises:
        ValueError: if ``outcome`` is not declarable or ``declared_by`` is empty.
    """
    if outcome not in DECLARED_OUTCOMES:
        raise ValueError(
            f"{outcome!r} cannot be declared; declarable outcomes are "
            f"{sorted(DECLARED_OUTCOMES)}"
        )
    if not isinstance(declared_by, str) or not declared_by.strip():
        raise ValueError("declare() requires a non-empty declared_by producer")
    return {
        "schema": SCHEMA_VERSION,
        "status": None,
        "stage": None,
        "terminal": True,
        "outcome": outcome,
        "outcome_kind": kind,
        "requires_attention": outcome in {"blocked", "unsupported", "needs_input"},
        "percent": None,
        "percent_basis": "terminal_no_percentage",
        "declared_by": declared_by.strip(),
        "outcome_reason": reason,
    }
