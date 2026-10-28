from __future__ import annotations

from sqlalchemy import inspect, text


REVISION = "20260824_01_scheduler_fencing"
DESCRIPTION = "Persist scheduler dispatch fencing epochs for HA audit and stale-worker rejection"


def _column_names(connection, table_name: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def _index_names(connection, table_name: str) -> set[str]:
    return {index["name"] for index in inspect(connection).get_indexes(table_name)}


def upgrade(connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("scheduler_dispatch_run"):
        return
    if "fencing_epoch" not in _column_names(connection, "scheduler_dispatch_run"):
        connection.execute(text(
            "ALTER TABLE scheduler_dispatch_run "
            "ADD COLUMN fencing_epoch INTEGER NOT NULL DEFAULT 0"
        ))
    index_name = "ix_scheduler_dispatch_run_fencing_epoch"
    if index_name not in _index_names(connection, "scheduler_dispatch_run"):
        connection.execute(text(
            f"CREATE INDEX {index_name} ON scheduler_dispatch_run (fencing_epoch)"
        ))


def downgrade(connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("scheduler_dispatch_run"):
        return
    index_name = "ix_scheduler_dispatch_run_fencing_epoch"
    if index_name in _index_names(connection, "scheduler_dispatch_run"):
        if connection.dialect.name in {"mysql", "mariadb"}:
            connection.execute(text(
                f"DROP INDEX {index_name} ON scheduler_dispatch_run"
            ))
        else:
            connection.execute(text(f"DROP INDEX {index_name}"))
    if "fencing_epoch" in _column_names(connection, "scheduler_dispatch_run"):
        connection.execute(text(
            "ALTER TABLE scheduler_dispatch_run DROP COLUMN fencing_epoch"
        ))
