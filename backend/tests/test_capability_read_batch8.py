"""C13 batch 8: identity, scheduler, unified alerts and problem trend."""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.alert_event import DBAlertEvent
from app.models.report_center import SchedulerDispatchRun, SchedulerLease
from app.models.user import User
from app.services import access_control, capability_registry as registry


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        roles = {row.role_key: row.id for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c13e-admin", password_hash="hash", role="admin", status="active"),
            User(id=2, username="c13e-reader", password_hash="hash", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"]),
            UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"),
            DBAlertEvent(id=1, event_uid="evt-1", source="zabbix", instance_id=10, severity="critical",
                         status="firing", title="CPU high"),
            SchedulerLease(id=1, lease_key="master_scheduler", owner_id="node-a", epoch=3,
                           lease_until=datetime.datetime.now() + datetime.timedelta(minutes=5)),
            SchedulerDispatchRun(id=1, dispatch_uid="d-1", lease_key="master_scheduler", owner_id="node-a",
                                 fencing_epoch=3, started_at=datetime.datetime.now(), status="success"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def read(db, capability, actor={"user_id": 1}, **kwargs):
    entry = registry._entry(capability)
    return registry.execute_read(db, capability, entry["version"], actor=actor, **kwargs)


def test_identity_reader_returns_provider_and_audit_sections(db):
    value = read(db, "platform.identity.read")
    assert value["items"] == [] and value["login_audit"] == []


def test_scheduler_reader_uses_persisted_lease_and_dispatches(db):
    value = read(db, "platform.scheduler.read")
    assert value["lease"]["active"] is True and value["lease"]["epoch"] == 3
    assert value["items"][0]["status"] == "success"


def test_alerts_are_scoped_and_read_does_not_advance_sla(db):
    value = read(db, "monitor.alert.read", actor={"user_id": 2})
    assert value["total"] == 0
    value = read(db, "monitor.alert.read", params={"status": "active"})
    assert value["items"][0]["title"] == "CPU high"
    with Session(db.get_bind(), autoflush=False) as fresh:
        assert fresh.get(DBAlertEvent, 1).sla_breached_at is None


def test_problem_trend_accepts_a_bounded_window(db):
    value = read(db, "performance.trend.read")
    assert value["days"] == 30 and isinstance(value["items"], list)


@pytest.mark.parametrize("capability", ["platform.identity.read", "platform.scheduler.read"])
def test_scoped_operator_cannot_read_global_administration(db, capability):
    with pytest.raises(registry.CapabilityError) as error:
        read(db, capability, actor={"user_id": 2})
    assert error.value.code in {"permission_denied", "scope_denied"}


def test_scoped_operator_cannot_read_foreign_alerts(db):
    with pytest.raises(registry.CapabilityError) as error:
        read(db, "monitor.alert.read", actor={"user_id": 2}, instance_id=10)
    assert error.value.code in {"permission_denied", "scope_denied"}
