"""Bounded notification retries, driven by the fenced master scheduler."""

from app.core.database import SessionLocal
from app.notify.service import consume_due_notifications


def run_due_notification_jobs():
    db = SessionLocal()
    try:
        return consume_due_notifications(db)
    finally:
        db.close()
