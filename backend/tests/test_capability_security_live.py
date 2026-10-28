"""C13 security reader against the live MySQL target; failure must not look like "no accounts"."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_registry as registry

PASSWORD = os.environ.get("MESDBTOOLS_MYSQL_TARGET_PASSWORD")
pytestmark = pytest.mark.skipif(not PASSWORD, reason="set MESDBTOOLS_MYSQL_TARGET_PASSWORD")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MESDBTOOLS_TARGET_MYSQL_PASSWORD", PASSWORD)
    engine = create_engine(f"sqlite:///{tmp_path / 'security.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id=1, username="c13h-admin", password_hash="hash", role="admin", status="active"))
        session.add(DBInstance(id=1, instance_name="live-mysql", db_type="mysql", host="127.0.0.1",
                               port=int(os.environ.get("MESDBTOOLS_MYSQL_TARGET_PORT", "33306")),
                               username=os.environ.get("MESDBTOOLS_MYSQL_TARGET_USER", "inspector"),
                               password_cipher="env://MESDBTOOLS_TARGET_MYSQL_PASSWORD", enabled=1))
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def test_live_mysql_accounts_are_read(db):
    entry = registry._entry("operations.security.read")
    value = registry.execute_read(db, "operations.security.read", entry["version"],
                                  actor={"user_id": 1}, instance_id=1)
    assert value["total"] >= 1
    assert any(row["user"] == "inspector" for row in value["items"])
    assert "password" not in {key for row in value["items"] for key in row if key == "password"}


def test_refused_connection_is_not_an_empty_success(db):
    instance = db.get(DBInstance, 1)
    instance.port = 1
    db.commit()
    entry = registry._entry("operations.security.read")
    with pytest.raises(registry.CapabilityError) as error:
        registry.execute_read(db, "operations.security.read", entry["version"],
                              actor={"user_id": 1}, instance_id=1)
    assert error.value.code == "upstream_unavailable"
