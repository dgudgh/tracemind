"""Observation-time boundary shared by current AWR diagnosis projections."""
from datetime import datetime, timedelta

from app.models.oracle_awr import AwrReport


MAX_CURRENT_AWR_AGE = timedelta(hours=1)


def _local_naive(value: datetime) -> datetime:
    return value.astimezone().replace(tzinfo=None) if value.tzinfo else value


def awr_window_quality(report, as_of: datetime | None = None) -> str:
    as_of = _local_naive(as_of or datetime.now())
    begin, end = report.begin_time, report.end_time
    if begin is None or end is None:
        return "missing"
    begin, end = _local_naive(begin), _local_naive(end)
    if begin > end or end > as_of:
        return "invalid"
    return "good" if end >= as_of - MAX_CURRENT_AWR_AGE else "stale"


def latest_current_awr(db, instance_id: int, as_of: datetime | None = None):
    as_of = _local_naive(as_of or datetime.now())
    return db.query(AwrReport).filter(
        AwrReport.instance_id == instance_id,
        AwrReport.analysis_results.isnot(None),
        AwrReport.begin_time.isnot(None),
        AwrReport.end_time >= as_of - MAX_CURRENT_AWR_AGE,
        AwrReport.end_time <= as_of,
        AwrReport.begin_time <= AwrReport.end_time,
        AwrReport.status.notin_(["failed", "error"]),
    ).order_by(AwrReport.end_time.desc(), AwrReport.id.desc()).first()


def observation_window(report) -> dict:
    return {"begin_time": report.begin_time.isoformat() if report.begin_time else None,
            "end_time": report.end_time.isoformat() if report.end_time else None,
            "max_age_seconds": int(MAX_CURRENT_AWR_AGE.total_seconds())}
