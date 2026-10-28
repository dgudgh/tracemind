"""Idempotent additive backfill. Run only under the platform's migrator lock."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy.orm import Session
from app.core.database import engine
from app.engine_registry import resolve_legacy
from app.models.db_instance import DBInstance
from app.models.capability_plan import DBCapabilityPlan
from app.models.engine_execution import DBEngineInstanceBinding, DBEnginePlanBinding
from app.services.engine_execution import READ_OPERATIONS
from app.services.plan_validation import binding_hash


def backfill(db: Session):
    result = {"mapped": 0, "unresolved": 0, "conflict": 0, "reprepare_required": 0}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for instance in db.query(DBInstance).order_by(DBInstance.id).yield_per(100):
        known = resolve_legacy(instance.db_type)["engine_id"]
        row = db.get(DBEngineInstanceBinding, instance.id)
        if row is None:
            db.add(DBEngineInstanceBinding(instance_id=instance.id, engine_id=known,
                                           source_db_type=instance.db_type,
                                           state="mapped" if known else "unresolved",
                                           expected_identity_json=None, checked_at=now))
            result["mapped" if known else "unresolved"] += 1
        elif row.source_db_type != instance.db_type or (known and row.engine_id != known):
            row.state = "conflict"
            result["conflict"] += 1
    for plan in db.query(DBCapabilityPlan).filter(DBCapabilityPlan.status != "settled").order_by(DBCapabilityPlan.plan_id).yield_per(100):
        if plan.capability_id not in READ_OPERATIONS or "execution_identity" in plan.capability_snapshot_json:
            continue
        row = db.get(DBEnginePlanBinding, plan.plan_id)
        if row is None:
            db.add(DBEnginePlanBinding(plan_id=plan.plan_id, engine_id=None,
                                       execution_identity_json=None, provider_version=None,
                                       provider_generation=None,
                                       operation=None,
                                       request_hash=plan.capability_snapshot_json["request_hash"],
                                       binding_hash=binding_hash(plan.capability_snapshot_json),
                                       plan_revision=plan.capability_snapshot_json.get("revision", 1),
                                       state="reprepare_required", created_at=now))
            result["reprepare_required"] += 1
    db.flush()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write only after platform migrator authorization")
    args = parser.parse_args()
    if not args.apply:
        parser.error("explicit --apply required; do not run against an unapproved database")
    from app.migrations.locking import schema_lock
    with schema_lock(engine) as connection:
        with Session(bind=connection, autoflush=False) as db:
            print(backfill(db))
            db.commit()


if __name__ == "__main__":
    main()
