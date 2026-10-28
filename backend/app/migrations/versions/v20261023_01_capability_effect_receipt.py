"""Add immutable evidence bindings for durable common capability effects."""

REVISION = "20261023_01_capability_effect_receipt"
DESCRIPTION = "Bind durable capability effects to execution, target, params and domain result"


def upgrade(connection):
    from app.models.capability_effect_receipt import DBCapabilityEffectReceipt

    DBCapabilityEffectReceipt.__table__.create(connection, checkfirst=True)


def downgrade(connection):
    from sqlalchemy import inspect, select
    from app.models.capability_effect_receipt import DBCapabilityEffectReceipt

    table = DBCapabilityEffectReceipt.__table__
    if not inspect(connection).has_table(table.name):
        return
    if connection.execute(select(table.c.id).limit(1)).first():
        raise RuntimeError("Capability effect receipts must be retained; non-empty downgrade refused")
    table.drop(connection)
