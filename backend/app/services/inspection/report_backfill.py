"""Index inspection jobs that completed before automatic report publication existed.

The backfill is deliberately explicit and idempotent: only completed jobs with published
result rows and no artifact are candidates, each job commits on its own, and a failure is
reported instead of being turned into a partial index.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.services.inspection import report_artifacts, result_writer

TERMINAL = ("success", "partial", "failed")


def pending_jobs(db: Session, *, limit: int = 200) -> list[DBInspectionJob]:
    query = db.query(DBInspectionJob).filter(
        DBInspectionJob.stage == "completed",
        DBInspectionJob.status.in_(TERMINAL),
        DBInspectionJob.finished_at.is_not(None),
    ).order_by(DBInspectionJob.finished_at, DBInspectionJob.id).limit(max(1, min(limit, 2000)))
    pending = []
    for row in query.all():
        key = f"source:inspection:{row.id}"
        if db.query(ReportArtifact.id).filter_by(source_key=key).first() is not None:
            continue
        if not result_writer.count_by_task_ids(db, [row.id]).get(row.id):
            continue
        pending.append(row)
    return pending


def backfill(db: Session, *, limit: int = 200, dry_run: bool = False) -> dict:
    jobs = pending_jobs(db, limit=limit)
    status_by_id = {row.id: row.status for row in jobs}
    indexed: list[str] = []
    errors: list[dict] = []
    if dry_run:
        return {"dry_run": True, "candidates": len(jobs), "candidate_ids": sorted(status_by_id),
                "indexed": [], "errors": []}
    for row in jobs:
        try:
            artifact = report_artifacts.sync_job_artifact(db, row)
            if artifact is None:
                db.rollback()
                errors.append({"job_id": row.id, "error": "not_indexable"})
                continue
            db.commit()
            indexed.append(row.id)
        except Exception as exc:
            db.rollback()
            errors.append({"job_id": row.id, "error": type(exc).__name__})
    return {"dry_run": False, "candidates": len(jobs), "candidate_ids": sorted(status_by_id),
            "indexed": sorted(indexed), "errors": errors}
