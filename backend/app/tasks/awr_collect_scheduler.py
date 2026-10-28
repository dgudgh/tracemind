import logging
from datetime import datetime

from app.core.database import SessionLocal
from app.models.awr_collect_policy import DBAwrCollectPolicy
from app.services.awr_collect import run_collect_by_policy

logger = logging.getLogger("app.tasks.awr_collect_scheduler")


def run_due_awr_jobs():
    db = SessionLocal()
    try:
        now = datetime.now()
        policies = (
            db.query(DBAwrCollectPolicy)
            .filter(DBAwrCollectPolicy.enabled == 1)
            .filter((DBAwrCollectPolicy.next_run_time == None) | (DBAwrCollectPolicy.next_run_time <= now))  # noqa: E711
            .all()
        )
        for policy in policies:
            try:
                run_collect_by_policy(db, policy)
            except Exception as exc:
                logger.error("AWR 自动采集失败 | instance_id=%s | error=%s", policy.instance_id, exc)
                db.rollback()
    finally:
        db.close()

