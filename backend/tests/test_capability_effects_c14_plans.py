"""C14 plan-only capabilities: a reviewed script is the deliverable, never an execution."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.backup_operation import BackupOperation
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id=1, username="c14pl-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14pl-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="oracle-10", db_type="oracle", host="127.0.0.1", port=1521,
                       username="system", service_name="ORCL", enabled=1,
                       password_cipher="env://SYNTHETIC_PLAN_PASSWORD"),
            DBInstance(id=20, instance_name="mysql-20", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_PLAN_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="oracle",
                          ip="127.0.0.1", port=1521, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
            AssetInstance(id=20, native_instance_id=20, stable_key="asset:20", name="asset-20", db_type="mysql",
                          ip="127.0.0.1", port=3306, host_id=20, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _run(db, capability, instance_id, params=None):
    created = plans.create_plan(db, actor=REQUESTER, capability_id=capability, definition_version=3,
                                instance_id=instance_id, params=params or {})
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_startup_script_is_generated_and_not_executed(db):
    finished = _run(db, "operations.startup.plan", 10)
    assert finished["outcome"] == "succeeded"
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["plan_only"] is True and "STARTUP" in inline["script"]
    assert inline["status"] == "plan_only"


def test_shutdown_script_is_generated_and_not_executed(db):
    finished = _run(db, "operations.shutdown.plan", 10, params={"mode": "immediate"})
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert "SHUTDOWN IMMEDIATE" in inline["script"] and inline["execution_blocked"] is True


def test_backup_plan_creates_no_operation_row(db):
    finished = _run(db, "protection.backup.plan", 20)
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["plan_only"] is True and inline["script"].strip()
    assert db.query(BackupOperation).count() == 0


def test_restore_plan_surfaces_the_blocked_reason_without_executing(db):
    finished = _run(db, "protection.restore.plan", 20)
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["script"].strip() and inline["execution_blocked"] is True
    assert inline["status"] == "blocked"


def test_a_plan_step_still_requires_independent_approval(db):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="operations.shutdown.plan",
                                definition_version=3, instance_id=10)
    with pytest.raises(plans.PlanError, match="approval_required"):
        plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
