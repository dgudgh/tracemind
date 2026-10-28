"""Persist control evidence without activating any control dispatcher."""
from __future__ import annotations

REVISION = "20261020_05_capability_control"
DESCRIPTION = "Retain capability control commands, target epochs and reconciliation evidence"


def _tables():
    from app.models.capability_control import (
        DBCapabilityControlCommand, DBCapabilityReconciliation, DBCapabilityTargetFence,
    )
    return (DBCapabilityControlCommand.__table__, DBCapabilityTargetFence.__table__,
            DBCapabilityReconciliation.__table__)


def upgrade(connection) -> None:
    for table in _tables():
        table.create(connection, checkfirst=True)


def downgrade(connection) -> None:
    from sqlalchemy import inspect, select

    existing = [table for table in _tables() if inspect(connection).has_table(table.name)]
    # Check the entire set before any DDL, including on engines without transactional DDL.
    for table in existing:
        if connection.execute(select(table).limit(1)).first():
            raise RuntimeError("Control history and target epochs must be retained; non-empty downgrade refused")
    for table in reversed(existing):
        table.drop(connection)
