"""Incremental C07 DDL preserves existing data and refuses destructive downgrade."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.migrations import runner
from app.migrations.versions import v20261020_05_capability_control as migration
from app.models.capability_control import DBCapabilityTargetFence


@pytest.fixture
def engine(tmp_path):
    value = create_engine(f"sqlite:///{tmp_path / 'control-migration.db'}")
    with value.begin() as connection:
        connection.execute(text("CREATE TABLE preserved_business (id INTEGER PRIMARY KEY, value TEXT)"))
        connection.execute(text("INSERT INTO preserved_business VALUES (1, 'keep')"))
    yield value
    value.dispose()


def test_registered_incremental_upgrade_and_second_boot_preserve_existing_data(engine):
    assert migration in runner.MIGRATIONS
    assert {table.name for table in migration._tables()}.isdisjoint(inspect(engine).get_table_names())
    with engine.begin() as connection:
        assert runner.apply_migrations(connection, [migration]) == [migration.REVISION]
    with engine.begin() as connection:
        assert runner.apply_migrations(connection, [migration]) == []
        assert connection.execute(text("SELECT value FROM preserved_business WHERE id=1")).scalar_one() == "keep"
    assert {table.name for table in migration._tables()} <= set(inspect(engine).get_table_names())


def test_empty_downgrade_is_idempotent_and_never_drops_unrelated_data(engine):
    with engine.begin() as connection:
        migration.upgrade(connection)
        migration.upgrade(connection)
        migration.downgrade(connection)
        migration.downgrade(connection)
        assert connection.execute(text("SELECT count(*) FROM preserved_business")).scalar_one() == 1
    assert {table.name for table in migration._tables()}.isdisjoint(inspect(engine).get_table_names())


def test_nonempty_epoch_history_refuses_all_downgrade_ddl_before_any_table_is_dropped(engine):
    with engine.begin() as connection:
        migration.upgrade(connection)
        connection.execute(DBCapabilityTargetFence.__table__.insert().values(
            target_key="instance:fixture", epoch=1, row_version=0, hold_state="unclaimed",
            updated_at=datetime(2026, 10, 20), hold_reason="epoch must never be reset",
        ))
    with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="non-empty downgrade refused"):
            migration.downgrade(connection)
    assert {table.name for table in migration._tables()} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(select(DBCapabilityTargetFence.__table__.c.epoch)).scalar_one() == 1
        assert connection.execute(text("SELECT value FROM preserved_business WHERE id=1")).scalar_one() == "keep"
