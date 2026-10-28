from sqlalchemy import create_engine, inspect, text

from app.migrations.runner import downgrade_revision, run_pending_migrations
from app.migrations.versions.v20260824_01_scheduler_fencing import REVISION


def _legacy_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE scheduler_dispatch_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_uid VARCHAR(64) NOT NULL,
                lease_key VARCHAR(100) NOT NULL,
                owner_id VARCHAR(160) NOT NULL,
                started_at DATETIME NOT NULL,
                status VARCHAR(30) NOT NULL
            )
        """))
        connection.execute(text("""
            INSERT INTO scheduler_dispatch_run
                (dispatch_uid, lease_key, owner_id, started_at, status)
            VALUES ('legacy-run', 'master', 'node-a', CURRENT_TIMESTAMP, 'success')
        """))
    return engine


def test_scheduler_fencing_migration_preserves_dispatch_history_and_rolls_back():
    engine = _legacy_engine()
    try:
        assert REVISION in run_pending_migrations(engine, allow_upgrade=True)
        columns = {
            column["name"] for column in inspect(engine).get_columns("scheduler_dispatch_run")
        }
        assert "fencing_epoch" in columns
        indexes = {
            index["name"] for index in inspect(engine).get_indexes("scheduler_dispatch_run")
        }
        assert "ix_scheduler_dispatch_run_fencing_epoch" in indexes
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT fencing_epoch FROM scheduler_dispatch_run WHERE dispatch_uid='legacy-run'"
            )).scalar_one() == 0

        assert downgrade_revision(engine, REVISION) is True
        columns = {
            column["name"] for column in inspect(engine).get_columns("scheduler_dispatch_run")
        }
        assert "fencing_epoch" not in columns
    finally:
        engine.dispose()
