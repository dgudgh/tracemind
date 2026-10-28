"""Retain preparation receipts without rewriting existing plans or business data."""
REVISION = "20261020_06_capability_preparation"
DESCRIPTION = "Recover atomically prepared plans by authenticated request identity"


def upgrade(connection):
    from app.models.capability_preparation import DBCapabilityPreparation

    DBCapabilityPreparation.__table__.create(connection, checkfirst=True)


def downgrade(connection):
    from sqlalchemy import inspect, select
    from app.models.capability_preparation import DBCapabilityPreparation

    table = DBCapabilityPreparation.__table__
    if not inspect(connection).has_table(table.name):
        return
    if connection.execute(select(table.c.preparation_id).limit(1)).first():
        raise RuntimeError("Preparation receipts must be retained; non-empty downgrade refused")
    table.drop(connection)
