from __future__ import annotations

from sqlalchemy import inspect, select

REVISION = "20260908_02_inspection_jobs"
DESCRIPTION = "Persist inspection jobs, frozen templates, ownership and execution outcomes"


def upgrade(connection) -> None:
    from app.models.inspection_job import DBInspectionJob

    DBInspectionJob.__table__.create(connection, checkfirst=True)


def downgrade(connection) -> None:
    from app.models.inspection_job import DBInspectionJob

    table = DBInspectionJob.__table__
    if not inspect(connection).has_table(table.name):
        return
    if connection.execute(select(table.c.id).limit(1)).first():
        raise RuntimeError("Inspection job history must be retained; non-empty table cannot be downgraded")
    table.drop(connection)
