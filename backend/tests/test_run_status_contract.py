"""The status vocabulary is the contract every other surface reads.

These assertions are deliberately about *words*, not about plumbing: if ``timed_out`` ever
becomes ``failed`` again, or an unrecognised status silently becomes ``success``, an operator
is told something definite about a run nobody has evidence for.
"""

from __future__ import annotations

import pytest

from app.services import run_status


@pytest.mark.parametrize("status", ["success", "partial", "failed"])
def test_completed_runs_keep_their_own_outcome(status):
    result = run_status.normalize(status)
    assert result["outcome"] == status
    assert result["terminal"] is True
    assert result["status"] == status


@pytest.mark.parametrize(
    "status,kind,outcome",
    [
        ("timed_out", "timeout", "unknown"),
        ("interrupted", "lease_lost", "unknown"),
    ],
)
def test_unverifiable_endings_are_unknown_not_failed(status, kind, outcome):
    """We do not know what the target side did, so we must not claim "collected nothing"."""
    result = run_status.normalize(status)
    assert result["outcome"] == outcome
    assert result["outcome"] != "failed"
    assert result["outcome_kind"] == kind
    assert result["requires_attention"] is True


def test_expired_queue_entry_is_its_own_outcome():
    result = run_status.normalize("expired")
    assert result["outcome"] == "expired"
    assert result["outcome_kind"] == "expired_in_queue"
    assert result["requires_attention"] is True
    assert result["terminal"] is True


@pytest.mark.parametrize("status", ["queued", "running"])
def test_in_flight_runs_have_no_outcome(status):
    result = run_status.normalize(status, stage=status)
    assert result["outcome"] is None
    assert result["terminal"] is False
    assert result["requires_attention"] is False


@pytest.mark.parametrize("status", ["some_new_status", "SUCCESS!", "", "done", "planning"])
def test_unrecognised_status_is_unknown_and_never_success(status):
    result = run_status.normalize(status)
    assert result["outcome"] == "unknown"
    assert result["outcome"] != "success"
    assert result["outcome_kind"] == "unrecognized_status"
    assert result["requires_attention"] is True


def test_missing_status_is_unknown_rather_than_success():
    for value in (None, 0, {}, []):
        result = run_status.normalize(value)  # type: ignore[arg-type]
        assert result["outcome"] == "unknown"
        assert result["outcome_kind"] == "unrecognized_status"


@pytest.mark.parametrize(
    "declared", ["plan_only", "blocked", "unsupported", "needs_input"]
)
def test_declared_outcomes_cannot_be_inferred_from_a_job_status(declared):
    """A declared outcome means "no evidence this ran"; a job status may never imply it."""
    result = run_status.normalize(declared)
    assert result["outcome"] != declared
    assert result["outcome"] == "unknown"
    assert run_status.normalize("success")["outcome"] != declared
    assert run_status.normalize("failed")["outcome"] != declared
    assert run_status.normalize(declared)["outcome"] not in run_status.DECLARED_OUTCOMES


@pytest.mark.parametrize(
    "status", sorted(run_status.KNOWN_STATUSES) + ["anything", "", None]
)
def test_normalize_never_returns_a_declared_outcome(status):
    outcome = run_status.normalize(status)["outcome"]
    assert outcome not in run_status.DECLARED_OUTCOMES


def test_declare_requires_an_explicit_outcome_outside_the_normal_vocabulary():
    with pytest.raises(ValueError):
        run_status.declare("success", "some-producer")
    with pytest.raises(ValueError):
        run_status.declare("unknown", "some-producer")
    with pytest.raises(ValueError):
        run_status.declare("plan_only", "   ")


def test_declare_records_who_asserted_the_outcome():
    result = run_status.declare("plan_only", "services.config_governance", kind="read_only_plan")
    assert result["outcome"] == "plan_only"
    assert result["declared_by"] == "services.config_governance"
    assert result["outcome_kind"] == "read_only_plan"
    assert result["requires_attention"] is False
    assert run_status.is_declared(result["outcome"]) is True


def test_terminal_runs_never_report_a_percentage():
    for status in sorted(run_status.TERMINAL_STATUSES):
        result = run_status.normalize(status, stage="saving")
        assert result["percent"] is None, status
        assert result["percent_basis"] == "terminal_no_percentage", status


def test_running_runs_report_a_weighted_percentage_with_its_basis():
    for stage, expected in (("queued", 10), ("validating", 30), ("saving", 90)):
        result = run_status.normalize("running", stage=stage)
        assert result["percent"] == expected
        assert result["percent_basis"] == "stage_weights_v1"
        assert 0 < result["percent"] < 100


def test_running_job_with_a_single_event_does_not_invent_progress():
    result = run_status.normalize("running", stage="validating", event_count=1)
    assert result["percent"] is None
    assert result["percent_basis"] == "missing_stage_events"


def test_queued_job_is_not_advanced_by_a_stale_stage_column():
    result = run_status.normalize("queued", stage="saving")
    assert result["percent"] == run_status.STAGE_WEIGHTS["queued"]


def test_failure_codes_classify_failures_and_absence_of_one_is_admitted():
    assert run_status.normalize("failed", failure_code="template_missing")["outcome_kind"] == "template_missing"
    assert run_status.normalize("failed", failure_code="permission_revoked")["outcome_kind"] == "permission_revoked"
    # No code (jobs that failed before this vocabulary existed) is stated, not guessed.
    assert run_status.normalize("failed")["outcome_kind"] == "unclassified_legacy"
    # An invented code is not echoed back as if it were a real classification.
    assert run_status.normalize("failed", failure_code="made_up")["outcome_kind"] == "unclassified_legacy"


def test_success_never_carries_a_failure_classification():
    result = run_status.normalize("success", failure_code="collector_error")
    assert result["outcome_kind"] is None


def test_cancellation_distinguishes_before_and_during_collection():
    before = run_status.normalize("cancelled", failure_code="cancelled_before_run")
    during = run_status.normalize("cancelled", failure_code="cancelled_during_run")
    assert (before["outcome"], before["outcome_kind"]) == ("cancelled", "cancelled_before_run")
    assert (during["outcome"], during["outcome_kind"]) == ("cancelled", "cancelled_during_run")
    # Only "cancelled while collecting" needs a human to check the target afterwards.
    assert before["requires_attention"] is False
    assert during["requires_attention"] is True
    # Jobs cancelled before this batch have no code; the started flag still decides honestly.
    assert run_status.normalize("cancelled")["outcome_kind"] == "cancelled_before_run"
    assert run_status.normalize("cancelled", started=True)["outcome_kind"] == "cancelled_during_run"


def test_results_published_is_not_the_same_question_as_report_worthiness():
    assert run_status.results_published("completed", 3) is True
    assert run_status.results_published("completed", 0) is False
    assert run_status.results_published("saving", 3) is False
    assert run_status.results_published("failed", 1) is False


def test_outcome_words_are_a_closed_set():
    produced = {run_status.normalize(status)["outcome"] for status in run_status.KNOWN_STATUSES}
    assert produced <= run_status.OUTCOMES | {None}
    assert "success" in produced
