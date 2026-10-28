import logging
from datetime import datetime

from app.core.database import SessionLocal
from app.models.capacity_policy import DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.services.capacity_monitor import evaluate_data_freshness, run_collect_by_policy

logger = logging.getLogger("app.tasks.capacity_scheduler")


def run_due_capacity_jobs():
    db = SessionLocal()
    try:
        now = datetime.now()
        policies = (
            db.query(DBCapacityPolicy)
            .filter(DBCapacityPolicy.enabled == 1)
            .filter((DBCapacityPolicy.next_run_time == None) | (DBCapacityPolicy.next_run_time <= now))  # noqa: E711
            .all()
        )
        for policy in policies:
            try:
                result = run_collect_by_policy(db, policy)
                if result.get("status") in {"error", "skipped", "no_data"}:
                    logger.info(
                        "capacity policy %s | instance_id=%s | message=%s",
                        result.get("status"),
                        policy.instance_id,
                        result.get("message"),
                    )
            except Exception as exc:
                logger.error("容量定时巡检失败 | instance_id=%s | error=%s", policy.instance_id, exc)
                db.rollback()
        evaluate_data_freshness(db, now)
    finally:
        db.close()


def get_all_policies_status(db):
    policies = db.query(DBCapacityPolicy).all()
    # One query for the whole estate instead of one per policy. This backs the scheduler
    # listing page, and the policy count grows with the number of instances, so the
    # per-row lookup made the endpoint O(policies) round trips. Same shape as
    # ``capacity_monitor.evaluate_data_freshness``, which already batches this way.
    instance_ids = {policy.instance_id for policy in policies if policy.instance_id}
    instances = (
        {
            row.id: row
            for row in db.query(DBInstance).filter(DBInstance.id.in_(instance_ids)).all()
        }
        if instance_ids
        else {}
    )
    result = []
    for p in policies:
        instance = instances.get(p.instance_id)
        result.append({
            "id": p.id,
            "instance_id": p.instance_id,
            "instance_name": instance.instance_name if instance else "未知",
            "host": f"{instance.host}:{instance.port}" if instance else "",
            "enabled": p.enabled,
            "frequency_minutes": p.frequency_minutes,
            "forecast_days_threshold": p.forecast_days_threshold,
            "abnormal_growth_mb": float(p.abnormal_growth_mb) if p.abnormal_growth_mb else 0,
            "abnormal_growth_pct": float(p.abnormal_growth_pct) if p.abnormal_growth_pct else 0,
            "retention_days": p.retention_days,
            "last_run_time": p.last_run_time.isoformat(sep=" ") if p.last_run_time else None,
            "last_success_time": p.last_success_time.isoformat(sep=" ") if p.last_success_time else None,
            "next_run_time": p.next_run_time.isoformat(sep=" ") if p.next_run_time else None,
            "last_status": p.last_status,
            "last_message": p.last_message,
            "consecutive_failures": p.consecutive_failures or 0,
            "last_sample_count": p.last_sample_count or 0,
            "data_freshness_status": p.data_freshness_status or "unknown",
        })
    return result
