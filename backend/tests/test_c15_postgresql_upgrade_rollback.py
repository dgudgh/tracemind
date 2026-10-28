"""C15: a real PostgreSQL upgrade/rollback rehearsal on a copied, disposable schema.

This is not a customer-database rehearsal, but it is a real PostgreSQL 8 transaction/DDL run:
old business rows survive the two pending revisions, a successful downgrade removes only the
new receipt table, and a non-empty receipt table refuses to downgrade instead of dropping
evidence.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.migrations.runner import JOURNAL, MIGRATIONS, downgrade_revision, run_pending_migrations
from app.models.capability_proposal import DBCapabilityProposal
from app.models.db_instance import DBInstance
from app.models.user import User

URL = os.environ.get("MESDBTOOLS_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set MESDBTOOLS_PG_TEST_URL to a disposable PostgreSQL database")

PENDING = {"20261020_06_capability_preparation", "20261020_07_capability_control_runtime",
           "20261020_08_capability_proposal"}


@pytest.fixture()
def engine():
    eng = create_engine(URL, pool_pre_ping=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        session.add(User(id=1, username="c15-old-user", password_hash="unused", role="admin", status="active"))
        session.add(DBInstance(id=1, instance_name="c15-old-instance", db_type="mysql", host="127.0.0.1",
                               port=3306, username="unused", enabled=1, password_cipher="env://C15"))
        session.commit()
    # Simulate the pre-upgrade schema: the two newest tables are absent and not journalled.
    with eng.begin() as connection:
        for table in ("db_capability_proposal", "db_capability_preparation"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        JOURNAL.create(connection, checkfirst=True)
        connection.execute(text(f"DELETE FROM {JOURNAL.name}"))
        for migration in MIGRATIONS:
            if migration.REVISION in PENDING:
                continue
            connection.execute(JOURNAL.insert().values(revision=migration.REVISION,
                                                      description=migration.DESCRIPTION))
    yield eng
    eng.dispose()


def test_pending_revisions_upgrade_preserve_old_rows_and_reapply_after_rollback(engine):
    with engine.connect() as connection:
        assert not inspect(connection).has_table("db_capability_proposal")
    applied = run_pending_migrations(engine, allow_upgrade=True)
    assert PENDING <= set(applied)
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert {"db_capability_proposal", "db_capability_preparation"} <= tables
        assert connection.execute(text("SELECT COUNT(*) FROM sys_user WHERE username='c15-old-user'")).scalar() == 1
        assert connection.execute(text("SELECT instance_name FROM db_instance WHERE id=1")).scalar() == "c15-old-instance"
    # An empty receipt table is removable; business rows are untouched.
    assert downgrade_revision(engine, "20261020_08_capability_proposal") is True
    with engine.connect() as connection:
        assert not inspect(connection).has_table("db_capability_proposal")
        assert connection.execute(text("SELECT COUNT(*) FROM sys_user")).scalar() == 1
    assert "20261020_08_capability_proposal" in set(run_pending_migrations(engine, allow_upgrade=True))


def test_a_non_empty_receipt_table_refuses_downgrade(engine):
    run_pending_migrations(engine, allow_upgrade=True)
    with Session(engine) as session:
        session.add(DBCapabilityProposal(
            proposal_id="WP" + "0" * 28, requested_by=1, idempotency_key="c15-keep",
            request_key_hash="r" * 64, input_hash="i" * 64, proposal_hash="p" * 64,
            catalog_hash="c" * 64, provider_config_id=1, provider_revision="v" * 64,
            provider_model="model", proposal_json={"schema": "capability.workflow_proposal/v1",
                                                   "accepted": False}, created_at=__import__("datetime").datetime.now(),
        ))
        session.commit()
    with pytest.raises(RuntimeError, match="retained"):
        downgrade_revision(engine, "20261020_08_capability_proposal")
    with engine.connect() as connection:
        assert inspect(connection).has_table("db_capability_proposal")
        assert connection.execute(text("SELECT COUNT(*) FROM db_capability_proposal")).scalar() == 1
