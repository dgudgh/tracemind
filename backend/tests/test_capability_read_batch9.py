"""C13 batch 9: snapshot agent/rule registers and the static Grafana catalogue."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.user import User
from app.services import access_control, capability_registry as registry


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        roles = {row.role_key: row.id for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="c13f-admin", password_hash="hash", role="admin", status="active"),
            User(id=2, username="c13f-reader", password_hash="hash", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"]),
            UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def read(db, capability, actor={"user_id": 1}):
    entry = registry._entry(capability)
    return registry.execute_read(db, capability, entry["version"], actor=actor)


def test_snapshot_registers_return_bounded_lists(db):
    agents = read(db, "diagnosis.snapshot_agent.read")
    rules = read(db, "diagnosis.snapshot_rule.read")
    assert agents["items"] == [] and agents["total"] == 0
    assert rules["items"] == [] and rules["total"] == 0


def test_grafana_catalogue_is_static_and_nonempty(db):
    value = read(db, "monitor.grafana_template.read")
    assert value["total"] > 0 and value["items"][0]["key"]


@pytest.mark.parametrize("capability", ["diagnosis.snapshot_agent.read", "diagnosis.snapshot_rule.read"])
def test_scoped_operator_cannot_read_snapshot_administration(db, capability):
    with pytest.raises(registry.CapabilityError) as error:
        read(db, capability, actor={"user_id": 2})
    assert error.value.code in {"permission_denied", "scope_denied"}
