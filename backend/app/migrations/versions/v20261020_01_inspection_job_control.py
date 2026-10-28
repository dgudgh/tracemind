"""Record who requested a cancellation, and make the queue's ordering key indexed.

The control plane could already *request* a cancellation (``cancel_requested``), but nothing
recorded **who** asked or **when**. An operator asking "who stopped this run?" had no answer,
and the read model could not distinguish "cancelled before it started" from "cancelled while
still collecting" - the one distinction that decides whether the target may still be running.

There is deliberately no ``outcome`` column here: the outcome is a *derived* value
(``app/services/run_status.py``). Storing it would create a second source of truth next to
``status``/``failure_reason`` and the two would eventually disagree.
"""
from __future__ import annotations

from sqlalchemy import inspect

REVISION = "20261020_01_inspection_job_control"
DESCRIPTION = "Record cancellation requester/timestamp and index the inspection job queue order"
TABLE = "db_inspection_job"
ADDITIONS = (
    ("cancel_requested_at", "datetime"),
    ("cancel_requested_by", "integer"),
)
CREATED_AT_INDEX = "ix_db_inspection_job_created_at"
SUPPORTED = {"sqlite", "mysql", "mariadb", "postgresql"}


def _datetime_sql(dialect_name: str) -> str:
    # PostgreSQL has no DATETIME; the other three do, and the model maps to DateTime.
    return "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"


def upgrade(connection):
    if connection.dialect.name not in SUPPORTED:
        raise RuntimeError(f"{REVISION}: unsupported dialect {connection.dialect.name}")
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        return None
    existing = {item["name"] for item in inspector.get_columns(TABLE)}
    for name, kind in ADDITIONS:
        if name in existing:
            continue
        sql_type = _datetime_sql(connection.dialect.name) if kind == "datetime" else "INTEGER"
        connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {name} {sql_type} NULL")
    index_names = {index["name"] for index in inspect(connection).get_indexes(TABLE)}
    if CREATED_AT_INDEX not in index_names and "created_at" in existing:
        connection.exec_driver_sql(
            f"CREATE INDEX {CREATED_AT_INDEX} ON {TABLE} (created_at)"
        )
    return None


def downgrade(connection):
    # Older binaries ignore both columns, and dropping them would erase the audit fact of who
    # asked for a cancellation and when. The index is a pure performance addition.
    return None
