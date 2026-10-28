"""C08: an explicit, idempotent index backfill for jobs that predate automatic publication."""
from __future__ import annotations

from app.models.report_center import ReportArtifact
from app.services.inspection import jobs, report_backfill
from test_inspection_report_atomic_publication import isolated_report_signing_key  # noqa: F401
from test_inspection_staged_publication import env, claimed, prepared  # noqa: F401


def _legacy(env):
    row = claimed(env)
    findings = prepared(env, row)
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=False)
    assert env.db.query(ReportArtifact).count() == 0
    return row


def test_backfill_indexes_only_completed_jobs_with_results(env, isolated_report_signing_key):
    row = _legacy(env)
    dry = report_backfill.backfill(env.db, limit=10, dry_run=True)
    assert dry["candidate_ids"] == [row.id] and dry["indexed"] == []
    assert env.db.query(ReportArtifact).count() == 0

    result = report_backfill.backfill(env.db, limit=10)
    assert result["indexed"] == [row.id] and result["errors"] == []
    assert env.db.query(ReportArtifact).filter_by(source_key=f"source:inspection:{row.id}").count() == 1

    again = report_backfill.backfill(env.db, limit=10)
    assert again["candidates"] == 0 and again["indexed"] == []


def test_backfill_failure_is_reported_and_leaves_no_artifact(env, isolated_report_signing_key, monkeypatch):
    row = _legacy(env)

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture index failure")
    monkeypatch.setattr(report_backfill.report_artifacts, "sync_job_artifact", fail)
    result = report_backfill.backfill(env.db, limit=10)
    assert result["indexed"] == [] and result["errors"][0]["job_id"] == row.id
    assert env.db.query(ReportArtifact).count() == 0
