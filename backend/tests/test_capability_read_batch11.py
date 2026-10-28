"""C13 batch 11: stored Oracle AWR report history (no Oracle connection needed to list rows)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.oracle_awr import AwrReport
from app.models.user import User
from app.services import capability_registry as registry


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="c13i-admin", password_hash="hash", role="admin", status="active"),
            DBInstance(id=10, instance_name="c13i-oracle", db_type="oracle", host="127.0.0.1", port=1521,
                       username="unused", password_cipher="env://C13I", enabled=1),
            AwrReport(id=1, instance_id=10, db_name="ORCL", dbid="123", instance_name="orcl1",
                      report_type="INSTANCE", report_format="HTML", begin_snap=1, end_snap=2,
                      file_name="awr.html", file_size=10, status="success"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def test_awr_history_lists_stored_rows(db):
    entry = registry._entry("performance.awr.query")
    value = registry.execute_read(db, "performance.awr.query", entry["version"],
                                  actor={"user_id": 1}, instance_id=10)
    assert value["total"] == 1 and value["items"][0]["file_name"] == "awr.html"


def test_awr_history_refuses_a_non_oracle_target(db):
    entry = registry._entry("performance.awr.query")
    with pytest.raises(registry.CapabilityError) as error:
        registry.execute_read(db, "performance.awr.query", entry["version"],
                              actor={"user_id": 1}, instance_id=99)
    assert error.value.code in {"engine_not_supported", "instance_not_found", "target_unavailable"}
