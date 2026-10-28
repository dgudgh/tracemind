"""C14: observability auto-bind is judged by the binding rows it persists."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.observability import ObservabilityInstanceBinding
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans
from app.services.observability import binding_service

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id=1, username="c14b2-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14b2-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="10.0.0.10", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_BIND_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="10.0.0.10", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _run(db):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="monitor.observability.bind",
                                definition_version=3)
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_binding_rows_are_the_evidence(db, monkeypatch):
    def auto_bind(_db):
        row = ObservabilityInstanceBinding(instance_id=10, host_identifier="mysql-10", binding_status="active",
                                           match_rule="test", enabled=1)
        _db.add(row)
        _db.commit()
        _db.refresh(row)
        return {"status": "success", "matched_count": 1, "binding_ids": [row.id]}
    monkeypatch.setattr(binding_service, "auto_bind", auto_bind)
    finished = _run(db)
    assert finished["outcome"] == "succeeded"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["count"] == 1


def test_no_match_is_partial_not_success(db, monkeypatch):
    monkeypatch.setattr(binding_service, "auto_bind",
                        lambda _db: {"status": "success", "matched_count": 0, "binding_ids": []})
    finished = _run(db)
    assert finished["outcome"] == "partial"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["reason_code"] == "no_matching_binding"
