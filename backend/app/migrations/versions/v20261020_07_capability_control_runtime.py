"""Opt-in local checkpoint control; preserve legacy data and all control history."""
from __future__ import annotations

from sqlalchemy import CheckConstraint, MetaData, Table, inspect, select, text
from sqlalchemy.schema import AddConstraint, CreateColumn, DropConstraint

REVISION = "20261020_07_capability_control_runtime"
DESCRIPTION = "Add opt-in execution control versions, checkpoints and local fencing epochs"
TABLE = "db_capability_execution"
ADDITIONS = ("control_enabled", "control_version", "desired_state", "observed_state",
             "control_command_id", "fencing_epoch", "checkpoint_json", "checkpoint_hash", "checkpoint_at")
CHECKS = ("ck_capability_execution_state", "ck_capability_execution_control", "ck_capability_execution_checkpoint")


def upgrade(connection):
    from app.models.capability_execution import DBCapabilityExecution

    dialect = connection.dialect.name
    if dialect not in {"sqlite", "mysql", "mariadb", "postgresql"}:
        raise RuntimeError(f"{REVISION}: unsupported dialect {dialect}")
    current = DBCapabilityExecution.__table__
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        current.create(connection)
        return
    if dialect == "sqlite":
        if inspector.has_table("c07_execution_upgrade"):
            raise RuntimeError("Control migration temporary table requires manual review")
        if inspector.get_foreign_keys(TABLE) or any(
                foreign_key.get("referred_table") == TABLE for table in inspector.get_table_names()
                if table != TABLE for foreign_key in inspector.get_foreign_keys(table)):
            raise RuntimeError("Control migration requires explicit handling of custom execution foreign keys")
        indexes = connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:table"), {"table": TABLE}).scalars().all()
        for name in indexes:
            quoted = connection.dialect.identifier_preparer.quote(name)
            if any(row[1] == -2 for row in connection.exec_driver_sql(f"PRAGMA index_xinfo({quoted})")):
                raise RuntimeError("Control migration requires explicit handling of custom expression indexes")
    # Refuse a SQLite rebuild that would silently discard custom triggers.
    if dialect == "sqlite" and connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:table"), {"table": TABLE}).first():
        raise RuntimeError("Control migration requires explicit handling of custom execution triggers")
    existing = {row["name"] for row in inspector.get_columns(TABLE)}
    for name in ADDITIONS:
        if name not in existing:
            column_sql = str(CreateColumn(current.c[name]).compile(dialect=connection.dialect))
            connection.exec_driver_sql(f"ALTER TABLE {TABLE} ADD COLUMN {column_sql}")
    constraints = {row["name"]: row["sqltext"] for row in inspect(connection).get_check_constraints(TABLE)}
    if all(name in constraints for name in CHECKS) and "stopped" in constraints[CHECKS[0]]:
        return
    wanted = {constraint.name: constraint for constraint in current.constraints if isinstance(constraint, CheckConstraint)}
    if dialect == "sqlite":
        old = Table(TABLE, MetaData(), autoload_with=connection)
        replacement = old.to_metadata(MetaData(), name="c07_execution_upgrade")
        for constraint in tuple(replacement.constraints):
            if constraint.name in CHECKS:
                replacement.constraints.remove(constraint)
        for name in CHECKS:
            replacement.append_constraint(CheckConstraint(str(wanted[name].sqltext), name=name))
        indexes = list(replacement.indexes)
        replacement.indexes.clear()
        replacement.create(connection)
        names = list(old.c.keys())
        connection.execute(replacement.insert().from_select(names, select(*(old.c[name] for name in names))))
        old.drop(connection)
        connection.exec_driver_sql(f"ALTER TABLE c07_execution_upgrade RENAME TO {TABLE}")
        replacement.name = TABLE
        for index in indexes:
            replacement.indexes.add(index)
            index.create(connection)
    else:
        for name in CHECKS:
            if name in constraints:
                if dialect in {"mysql", "mariadb"}:
                    keyword = "CONSTRAINT" if dialect == "mariadb" else "CHECK"
                    connection.exec_driver_sql(f"ALTER TABLE {TABLE} DROP {keyword} {name}")
                else:
                    connection.execute(DropConstraint(wanted[name]))
            connection.execute(AddConstraint(wanted[name]))


def downgrade(connection):
    raise RuntimeError("Control checkpoints/epochs must be retained; runtime downgrade requires manual review")
