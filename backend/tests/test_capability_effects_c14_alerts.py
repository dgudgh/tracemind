"""C14: external alert import is judged by the unified events it touches."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.alert_event import DBAlertEvent
from app.models.db_instance import DBInstance
from app.models.external_monitoring import PrometheusAlertCache
from app.models.observability import ObservabilityAlertEvent
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c14a-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14a-approver", password_hash="unused", role="admin", status="active"),
            User(id=2, username="c14a-viewer", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"].id),
            UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_ALERT_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="127.0.0.1", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
            PrometheusAlertCache(id=1, alert_name="HighCPU", alert_status="firing", severity="warning",
                                 db_instance_id=10, instance_name="mysql-10", labels_json={}, annotations_json={}),
            ObservabilityAlertEvent(id=1, source_type="prometheus", alert_key="cpu", instance_id=10,
                                    severity="warning", title="CPU high", status="firing"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _approved(db, actor=REQUESTER):
    created = plans.create_plan(db, actor=actor, capability_id="monitor.alert.sync", definition_version=3)
    governance.request_approval(db, created["plan_id"], actor=actor, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return created


def test_imported_events_are_the_evidence(db):
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "succeeded", finished
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["count"] == 2
    assert {row.source for row in db.query(DBAlertEvent).all()} == {"prometheus"}


def test_empty_cache_is_partial(db):
    db.query(PrometheusAlertCache).delete()
    db.query(ObservabilityAlertEvent).delete()
    db.commit()
    created = _approved(db)
    finished = plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)
    assert finished["outcome"] == "partial"
    assert finished["steps"][0]["result_ref"]["inline"]["data"]["reason_code"] == "empty_alert_cache"
