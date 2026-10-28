"""Expand-only engine sidecars. Existing P-1 tables and writers remain intact."""
REVISION = "20261027_01_engine_execution"
DESCRIPTION = "Add engine identity, plan binding and standard result sidecars"


def upgrade(connection):
    from app.models.engine_execution import DBEngineInstanceBinding, DBEnginePlanBinding, DBEngineResultReceipt
    for model in (DBEngineInstanceBinding, DBEnginePlanBinding, DBEngineResultReceipt):
        model.__table__.create(connection, checkfirst=True)


def downgrade(connection):
    raise RuntimeError("expand_only_migration")
