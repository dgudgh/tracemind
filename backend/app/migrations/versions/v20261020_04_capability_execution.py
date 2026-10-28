"""Create the durable capability batch and execution ledger, preserving model metadata."""
from __future__ import annotations

REVISION = "20261020_04_capability_execution"
DESCRIPTION = "Persist capability batches, bounded delivery, ownership and reconciliation"


def upgrade(connection) -> None:
    from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution

    for model in (DBCapabilityBatch, DBCapabilityExecution):
        model.__table__.create(connection, checkfirst=True)


def downgrade(connection) -> None:
    from sqlalchemy import inspect, select

    from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution

    tables = (DBCapabilityExecution.__table__, DBCapabilityBatch.__table__)
    # Preflight every table before dropping any of them, including on non-transactional DDL.
    existing = [table for table in tables if inspect(connection).has_table(table.name)]
    for table in existing:
        if connection.execute(select(table).limit(1)).first():
            raise RuntimeError("Capability execution history must be retained; non-empty table cannot be downgraded")
    for table in existing:
        table.drop(connection)
