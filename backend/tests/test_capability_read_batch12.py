"""C13/C14: stored observability registers and linkage health (no live probing)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.db_instance import DBInstance
from app.models.observability import (ObservabilityAlertEvent, ObservabilityCollectPolicy,
                                      ObservabilityCurrentMetric, ObservabilityInstanceBinding,
                                      ObservabilitySource)
from app.models.user import User
from app.services import access_control, capability_registry as registry


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        roles = {row.role_key: row.id for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c13j-admin", password_hash="hash", role="admin", status="active"),
            User(id=2, username="c13j-reader", password_hash="hash", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"]),
            UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"),
            DBInstance(id=10, instance_name="obs-10", db_type="mysql", host="10.0.0.10", port=3306,
                       username="unused", password_cipher="env://C13J", enabled=1),
            ObservabilitySource(id=1, source_name="prom", source_type="prometheus",
                                api_url="http://prom.invalid", password_encrypted="env://PROM_SECRET", enabled=1),
            ObservabilityInstanceBinding(id=1, instance_id=10, host_identifier="obs-10", enabled=1,
                                         binding_status="active"),
            ObservabilityCollectPolicy(id=1, policy_name="minute", source_type="prometheus", enabled=1),
            ObservabilityAlertEvent(id=1, source_type="prometheus", alert_key="cpu", instance_id=10,
                                    severity="warning", title="CPU", status="firing"),
            ObservabilityCurrentMetric(id=1, source_type="prometheus", source_id=1, instance_id=10,
                                       standard_metric="cpu", metric_value=1, status="ok"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def read(db, capability, actor={"user_id": 1}, **kwargs):
    entry = registry._entry(capability)
    return registry.execute_read(db, capability, entry["version"], actor=actor, **kwargs)


def test_source_register_omits_secret_and_counts(db):
    value = read(db, "monitor.apim_source.read" if False else "monitor.apm_source.read")
    assert value["items"][0]["source_name"] == "prom"
    assert "PROM_SECRET" not in str(value) and "env://" not in str(value)
    assert value["alert_count"] == 1 and value["metric_count"] == 1 and value["task_count"] == 0


def test_linkage_health_reads_stored_bindings(db):
    value = read(db, "monitor.observability.read", instance_id=10)
    assert value["linkage"]["instance"]["id"] == 10
    assert isinstance(value["linkage"]["bindings"], list)


def test_scoped_operator_cannot_read_the_global_source_register(db):
    with pytest.raises(registry.CapabilityError) as error:
        read(db, "monitor.apm_source.read", actor={"user_id": 2})
    assert error.value.code in {"permission_denied", "scope_denied"}


def test_object_read_returns_stored_table_profiles(db):
    from app.models.table_profile import DBTableProfile

    db.add(DBTableProfile(instance_id=10, db_type="mysql", table_name="t1", capture_time=None, issues=[]))
    db.commit()
    value = read(db, "operations.object.read", instance_id=10)
    assert value["total"] == 1 and value["items"][0]["table_name"] == "t1"
