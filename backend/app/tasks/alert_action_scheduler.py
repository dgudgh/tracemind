"""Bounded alert-action retries, driven by the fenced master scheduler."""

from app.core.database import SessionLocal
from app.services.alert_lifecycle import retry_due_actions


def run_due_alert_action_jobs():
    db = SessionLocal()
    try:
        return retry_due_actions(db)
    finally:
        db.close()
