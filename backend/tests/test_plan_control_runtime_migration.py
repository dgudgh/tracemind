"""Real pre-C07 ledger DDL/data migration, never a platform database."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.schema import CreateColumn
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.migrations.versions import v20261020_07_capability_control_runtime as migration
from app.models.capability_execution import DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan


OLD_DDL = """
CREATE TABLE db_capability_execution (
 id INTEGER PRIMARY KEY, batch_id VARCHAR(30) NOT NULL, seq INTEGER NOT NULL,
 plan_id VARCHAR(30) NOT NULL, plan_revision INTEGER NOT NULL, binding_hash VARCHAR(64) NOT NULL,
 target_key VARCHAR(100) NOT NULL, state VARCHAR(32) NOT NULL, worker_id VARCHAR(128),
 owner_token VARCHAR(64), attempt INTEGER NOT NULL, claimed_at DATETIME, heartbeat_at DATETIME,
 lease_expires_at DATETIME, reconciliation_checked_at DATETIME, finished_at DATETIME, reason VARCHAR(96),
 CONSTRAINT uq_capability_execution_sequence UNIQUE(batch_id,seq),
 CONSTRAINT ck_capability_execution_sequence CHECK(seq >= 1 AND seq <= 100),
 CONSTRAINT ck_capability_execution_state CHECK(state IN
 ('queued','running','archived','reconciliation_required','cancelled','expired','blocked')),
 CONSTRAINT ck_capability_execution_owner CHECK(state != 'running' OR
 (owner_token IS NOT NULL AND worker_id IS NOT NULL AND heartbeat_at IS NOT NULL AND lease_expires_at IS NOT NULL))
)
"""


@pytest.fixture
def engine(tmp_path):
    value = create_engine(f"sqlite:///{tmp_path / 'old-control.db'}")
    with value.begin() as connection:
        connection.execute(text(OLD_DDL))
        for index in DBCapabilityExecution.__table__.indexes:
            index.create(connection)
        connection.execute(text("CREATE INDEX custom_execution_reason ON db_capability_execution(reason)"))
        for identity, state in enumerate(("queued", "running", "reconciliation_required"), 1):
            connection.execute(text(
                "INSERT INTO db_capability_execution "
                "(id,batch_id,seq,plan_id,plan_revision,binding_hash,target_key,state,worker_id,owner_token,"
                "attempt,heartbeat_at,lease_expires_at,reason) VALUES "
                "(:id,'historical',:id,:plan,1,:hash,:target,:state,'old-worker','old-token',1,"
                "'2026-10-20 01:00:00','2026-10-20 01:00:45',:reason)"),
                {"id": identity, "plan": f"historical-{identity}", "hash": "a" * 64,
                 "target": f"instance:{identity}", "state": state,
                 "reason": "plan_outcome_uncertain" if identity == 3 else "preserved"})
        DBCapabilityPlan.__table__.create(connection)
        connection.execute(DBCapabilityPlan.__table__.insert().values(
            plan_id="historical-3", capability_id="inspection.run.execute", definition_version=2,
            capability_snapshot_json={}, requested_by=1, trigger_type="manual", status="settled",
            outcome="unknown", idempotency_key="historical-unknown", created_at=datetime(2026, 10, 20)))
    yield value
    value.dispose()


def test_old_queued_running_unknown_history_survives_upgrade_and_second_upgrade(engine):
    assert set(migration.ADDITIONS).isdisjoint({row["name"] for row in inspect(engine).get_columns(migration.TABLE)})
    with engine.connect() as connection:
        before = connection.execute(text("SELECT * FROM db_capability_execution ORDER BY id")).mappings().all()
    with engine.begin() as connection:
        migration.upgrade(connection)
        migration.upgrade(connection)
    with engine.connect() as connection:
        rows = connection.execute(select(DBCapabilityExecution.__table__).order_by(DBCapabilityExecution.id)).mappings().all()
        for old, new in zip(before, rows):
            for name, value in old.items():
                actual = new[name]
                # Typed DateTime readers return datetime rather than the raw legacy SQL string.
                assert str(actual) == str(value)
            assert not new["control_enabled"] and new["control_version"] == new["fencing_epoch"] == 0
            assert new["desired_state"] == "run" and new["checkpoint_json"] is None
        assert connection.execute(select(DBCapabilityPlan.outcome)).scalar_one() == "unknown"
    assert "custom_execution_reason" in {row["name"] for row in inspect(engine).get_indexes(migration.TABLE)}
    checks = {row["name"]: row["sqltext"] for row in inspect(engine).get_check_constraints(migration.TABLE)}
    assert all(name in checks for name in migration.CHECKS)
    assert "paused" in checks[migration.CHECKS[0]] and "stopped" in checks[migration.CHECKS[0]]


def test_custom_trigger_refusal_precedes_any_column_or_data_change(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE custom_audit (execution_id INTEGER)"))
        connection.execute(text("CREATE TRIGGER preserve_execution_trigger AFTER UPDATE ON db_capability_execution "
                                "BEGIN INSERT INTO custom_audit VALUES (new.id); END"))
        with pytest.raises(RuntimeError, match="custom execution triggers"):
            migration.upgrade(connection)
        assert set(migration.ADDITIONS).isdisjoint(
            {row["name"] for row in inspect(connection).get_columns(migration.TABLE)})
        assert connection.execute(text("SELECT count(*) FROM db_capability_execution")).scalar_one() == 3
        assert connection.execute(text("SELECT count(*) FROM sqlite_master WHERE type='trigger' "
                                       "AND name='preserve_execution_trigger'")).scalar_one() == 1
        connection.execute(text("UPDATE db_capability_execution SET reason='still works' WHERE id=1"))
        assert connection.execute(text("SELECT execution_id FROM custom_audit")).scalar_one() == 1


def test_downgrade_retains_checkpoints_and_all_history(engine):
    with engine.begin() as connection:
        migration.upgrade(connection)
        with pytest.raises(RuntimeError, match="must be retained"):
            migration.downgrade(connection)
        assert connection.execute(text("SELECT count(*) FROM db_capability_execution")).scalar_one() == 3


def test_new_columns_compile_for_supported_dialects_without_contacting_them():
    for dialect in (sqlite.dialect(), mysql.dialect(), postgresql.dialect()):
        for name in migration.ADDITIONS:
            assert name in str(CreateColumn(DBCapabilityExecution.__table__.c[name]).compile(dialect=dialect))


def test_expression_index_is_refused_before_rebuild_can_drop_it(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE INDEX preserve_expression_index ON db_capability_execution(lower(reason))"))
        with pytest.raises(RuntimeError, match="custom expression indexes"):
            migration.upgrade(connection)
        assert set(migration.ADDITIONS).isdisjoint(
            {row["name"] for row in inspect(connection).get_columns(migration.TABLE)})
        assert connection.execute(text("SELECT count(*) FROM sqlite_master WHERE type='index' "
                                       "AND name='preserve_expression_index'")).scalar_one() == 1
