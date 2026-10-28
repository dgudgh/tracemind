"""Add capacity anti-flap controls without changing today's behaviour.

Defaults preserve the current semantics exactly: ``sustained_minutes=0`` publishes immediately
and ``recovery_margin_pct=10`` only widens the close condition for deployments that never had
one. ``notified_at`` is backfilled from ``first_seen`` so alerts that are already published stay
published after the upgrade instead of going quiet for a cycle.
"""
from __future__ import annotations

from sqlalchemy import DateTime, Integer, Numeric, inspect, text

REVISION = "20261019_02_capacity_anti_flap"
DESCRIPTION = "Add capacity sustained-breach window, recovery margin and publication stamp"
POLICY_TABLE = "db_capacity_policy"
ALERT_TABLE = "db_capacity_alert"
POLICY_ADDITIONS = (
    ("sustained_minutes", "INTEGER", Integer),
    ("recovery_margin_pct", "NUMERIC(8, 2)", Numeric),
)
SUPPORTED = {"sqlite", "mysql", "mariadb", "postgresql"}


def upgrade(connection):
    if connection.dialect.name not in SUPPORTED:
        raise RuntimeError(f"{REVISION}: unsupported dialect {connection.dialect.name}")
    inspector = inspect(connection)

    if inspector.has_table(POLICY_TABLE):
        existing = {item["name"] for item in inspector.get_columns(POLICY_TABLE)}
        for name, ddl_type, _expected in POLICY_ADDITIONS:
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {POLICY_TABLE} ADD COLUMN {name} {ddl_type} NULL"
                )

    if not inspector.has_table(ALERT_TABLE):
        return None
    existing = {item["name"] for item in inspector.get_columns(ALERT_TABLE)}
    if "notified_at" not in existing:
        connection.exec_driver_sql(f"ALTER TABLE {ALERT_TABLE} ADD COLUMN notified_at DATETIME NULL")
        # Everything open was already published under the previous rules.
        connection.execute(text(
            f"UPDATE {ALERT_TABLE} SET notified_at = COALESCE(first_seen, create_time) "
            f"WHERE notified_at IS NULL"
        ))
    return None


def downgrade(connection):
    # Older binaries ignore all three columns; dropping them would silently re-arm flapping.
    return None
