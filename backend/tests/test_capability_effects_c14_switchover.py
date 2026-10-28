"""C14: Data Guard switchover is a plan-only capability on a real Oracle primary."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans

PASSWORD = os.environ.get("MESDBTOOLS_ORACLE_PASSWORD")
HOST = os.environ.get("MESDBTOOLS_ORACLE_HOST", "192.168.102.200")
PORT = int(os.environ.get("MESDBTOOLS_ORACLE_PORT", "1521"))
SERVICE = os.environ.get("MESDBTOOLS_ORACLE_SERVICE", "mesdb")
REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}

pytestmark = pytest.mark.skipif(not PASSWORD, reason="set MESDBTOOLS_ORACLE_PASSWORD for a disposable Oracle")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MESDBTOOLS_ORACLE_PASSWORD", PASSWORD)
    engine = create_engine(f"sqlite:///{tmp_path / 'switchover.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="sw-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="sw-approver", password_hash="unused", role="admin", status="active"),
            AssetInstance(id=3, native_instance_id=3, stable_key="ora:3", name="mesdb", db_type="oracle",
                          ip=HOST, port=PORT, host_id=3, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
            DBInstance(id=3, instance_name="mesdb-19c", db_type="oracle", host=HOST, port=PORT,
                       service_name=SERVICE, oracle_connect_type="service", username="sys",
                       password_cipher="env://MESDBTOOLS_ORACLE_PASSWORD", account_type="sysdba", enabled=1),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def test_switchover_generates_plan_only_without_execution(db):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="operations.switchover.plan",
                                definition_version=3, instance_id=3)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "succeeded", finished
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["plan_only"] is True and inline["execution_blocked"] is True
    assert inline["status"] == "plan_only"
    assert "Switchover Pre-check" in inline["sql"]
    assert "v$database" in inline["sql"]
    assert inline["current"]["database_role"] == "PRIMARY"
    assert inline["warnings"]
    assert inline["current"]["guard_status"] == "NONE"
    assert isinstance(inline["destinations"], list)
    # The capability exposes a plan; it does not run SWITCHOVER.
    upper = inline["sql"].upper()
    assert "ALTER DATABASE" not in upper and "SWITCHOVER TO" not in upper
