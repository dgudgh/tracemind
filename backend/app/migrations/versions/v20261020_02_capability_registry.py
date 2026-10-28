"""Create the capability definition ledger.

The table is created through ``DBCapabilityDefinition.__table__.create`` on purpose - not with
hand-written DDL. ``schema_contract.validate_schema`` compares tables, columns, **index names**
and UniqueConstraints against the model metadata one by one, and any drift (for example an
index named anything but SQLAlchemy's generated ``ix_<table>_<column>``) makes an existing
installation refuse to start after the upgrade. Building from the model guarantees the
migration cannot diverge from the declaration.

A pre-existing installation cannot get this table from ``create_all``:
``initialization.initialize_schema`` only runs ``metadata.create_all`` while the bootstrap
revision has not been applied yet, and every real installation already applied it. That is why
this revision exists.
"""
from __future__ import annotations

REVISION = "20261020_02_capability_registry"
DESCRIPTION = "Create the capability definition ledger"


def upgrade(connection) -> None:
    from app.models.capability_definition import DBCapabilityDefinition

    DBCapabilityDefinition.__table__.create(connection, checkfirst=True)


def downgrade(connection) -> None:
    from sqlalchemy import inspect, select

    from app.models.capability_definition import DBCapabilityDefinition

    table = DBCapabilityDefinition.__table__
    if not inspect(connection).has_table(table.name):
        return
    if connection.execute(select(table.c.capability_id).limit(1)).first():
        raise RuntimeError(
            "Capability registry history must be retained; non-empty table cannot be downgraded"
        )
    table.drop(connection)
