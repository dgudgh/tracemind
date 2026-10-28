"""Give data quality policies a schedule so the periodic consumer has something to be due against.

Before this, `evaluate_data_quality` and `run_retention` only ran when somebody opened the
observability page: no cadence column existed, so no scheduler could honestly claim to be
"due". The columns are additive and nullable-with-default; existing policies keep evaluating
unchanged and retention stays a plan until a scope opts into execution.
"""
from __future__ import annotations

from sqlalchemy import DateTime, Integer, inspect

REVISION = "20261019_01_data_quality_schedule"
DESCRIPTION = "Add data quality evaluation/retention cadence and retention execution opt-in"
TABLE = "observability_data_quality_policy"
ADDITIONS = (
    ("evaluate_interval_seconds", "INTEGER", Integer),
    ("retention_interval_seconds", "INTEGER", Integer),
    ("retention_auto_execute", "INTEGER", Integer),
    ("last_retention_at", "DATETIME", DateTime),
)
SUPPORTED = {"sqlite", "mysql", "mariadb", "postgresql"}


def upgrade(connection):
    if connection.dialect.name not in SUPPORTED:
        raise RuntimeError(f"{REVISION}: unsupported dialect {connection.dialect.name}")
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        return None
    existing = {item["name"] for item in inspector.get_columns(TABLE)}
    for name, ddl_type, _expected in ADDITIONS:
        if name in existing:
            continue
        connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl_type} NULL")
    return None


def downgrade(connection):
    # Older code reads these columns through defaults; dropping them would take the schedule
    # away from anyone who rolls back the binary but not the schema.
    return None
