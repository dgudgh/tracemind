from decimal import Decimal
from typing import Any

from app.models.sql_plan import DBSqlPlanHistory


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_sql_history(db, profile: Any) -> list[dict[str, Any]]:
    def field(key):
        return profile.get(key) if isinstance(profile, dict) else getattr(profile, key, None)

    instance_id, sql_id = field("instance_id"), field("sql_id")
    rows = []
    if instance_id and sql_id:
        rows = (
            db.query(DBSqlPlanHistory)
            .filter(DBSqlPlanHistory.instance_id == instance_id, DBSqlPlanHistory.sql_id == sql_id,
                    DBSqlPlanHistory.schema_name == field("schema_name"), DBSqlPlanHistory.source == field("source"),
                    DBSqlPlanHistory.target_fingerprint == field("target_fingerprint"))
            .order_by(DBSqlPlanHistory.sample_time.desc(), DBSqlPlanHistory.id.desc())
            .limit(90)
            .all()
        )
    if rows:
        return [{
            "sample_time": str(row.sample_time) if row.sample_time else None,
            "history_status": "recorded" if row.sample_time else "sample_time_missing",
            "target_status": "recorded" if row.target_snapshot else "historical_context_missing",
            "target_snapshot": row.target_snapshot,
            "plan_hash": row.plan_hash,
            "executions": row.executions,
            "elapsed_ms": _float(row.elapsed_ms),
            "cpu_ms": _float(row.cpu_ms),
            "buffer_gets": row.buffer_gets,
            "disk_reads": row.disk_reads,
            "rows_processed": row.rows_processed,
            "source": row.source,
        } for row in rows]
    capture_time = profile.get("capture_time") if isinstance(profile, dict) else getattr(profile, "capture_time", None)
    return [{
        "sample_time": str(capture_time) if capture_time else None,
        "history_status": "current_sample_only" if capture_time else "not_collected",
        "target_status": "historical_context_missing",
        "plan_hash": profile.get("plan_hash") if isinstance(profile, dict) else getattr(profile, "plan_hash", None),
        "executions": profile.get("executions") if isinstance(profile, dict) else getattr(profile, "executions", None),
        "elapsed_ms": _float(profile.get("elapsed_ms") if isinstance(profile, dict) else getattr(profile, "elapsed_ms", None)),
        "cpu_ms": _float(profile.get("cpu_ms") if isinstance(profile, dict) else getattr(profile, "cpu_ms", None)),
        "buffer_gets": profile.get("buffer_gets") if isinstance(profile, dict) else getattr(profile, "buffer_gets", None),
        "disk_reads": profile.get("disk_reads") if isinstance(profile, dict) else getattr(profile, "disk_reads", None),
        "rows_processed": profile.get("rows_processed") if isinstance(profile, dict) else getattr(profile, "rows_processed", None),
        "source": profile.get("source") if isinstance(profile, dict) else getattr(profile, "source", None),
    }]
