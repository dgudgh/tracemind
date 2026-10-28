"""Validate model tables, columns, indexes and uniqueness without destructive repair."""

import hashlib

from sqlalchemy import Index, MetaData, Table, UniqueConstraint, inspect
from sqlalchemy.schema import AddConstraint

from app.migrations.policy import SchemaUpgradeRequired


def _index_name(connection, index):
    return connection.dialect.identifier_preparer.truncate_and_render_index_name(
        index.name, _alembic_quote=False
    )


def _full_index(index):
    options = index.get("dialect_options", {})
    return all(
        options.get(name) is None for name in ("postgresql_where", "sqlite_where")
    )


def _inventory(connection, table):
    inspector = inspect(connection)
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes(table.name, schema=table.schema)
    }
    unique = {
        frozenset(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            table.name, schema=table.schema
        )
    }
    unique.update(
        frozenset(index["column_names"])
        for index in indexes.values()
        if index["unique"] and _full_index(index)
    )
    return indexes, unique


def _index_matches(index, actual):
    return (
        tuple(column.name for column in index.columns) == tuple(actual["column_names"])
        and bool(index.unique) == bool(actual["unique"])
        and _full_index(actual)
    )


def reconcile_bootstrap_indexes(connection, metadata):
    """Resume a partial MySQL CREATE TABLE / CREATE INDEX without dropping anything."""
    for table in metadata.sorted_tables:
        inspector = inspect(connection)
        if not inspector.has_table(table.name, schema=table.schema):
            continue
        columns = {
            item["name"]
            for item in inspector.get_columns(table.name, schema=table.schema)
        }
        indexes, unique = _inventory(connection, table)
        for index in sorted(table.indexes, key=lambda item: item.name):
            if not {column.name for column in index.columns}.issubset(columns):
                continue
            name = _index_name(connection, index)
            if name in indexes:
                if not _index_matches(index, indexes[name]):
                    raise SchemaUpgradeRequired(
                        f"Index {table.fullname}.{name} has an incompatible definition"
                    )
                continue
            index.create(connection)
        _, unique = _inventory(connection, table)
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            required = frozenset(column.name for column in constraint.columns)
            if required in unique or not required.issubset(columns):
                continue
            reflected = Table(
                table.name, MetaData(), schema=table.schema, autoload_with=connection
            )
            if connection.dialect.name == "sqlite":
                # SQLite cannot ALTER ADD CONSTRAINT. An equivalent unique index is additive.
                name = (
                    constraint.name
                    or "uq_bootstrap_"
                    + hashlib.sha256(
                        (table.fullname + ":" + ",".join(sorted(required))).encode()
                    ).hexdigest()[:24]
                )
                Index(
                    name,
                    *(reflected.c[column.name] for column in constraint.columns),
                    unique=True,
                ).create(connection)
            else:
                # AddConstraint changes its constraint's create rule in older SQLAlchemy.
                # Use a reflected copy so retries do not mutate application model metadata.
                copied = UniqueConstraint(
                    *(reflected.c[column.name] for column in constraint.columns),
                    name=constraint.name,
                    deferrable=constraint.deferrable,
                    initially=constraint.initially,
                )
                connection.execute(AddConstraint(copied))
            unique.add(required)


def validate_schema(connection, metadata):
    inspector = inspect(connection)
    missing = []
    for table in metadata.tables.values():
        if not inspector.has_table(table.name, schema=table.schema):
            missing.append(table.fullname)
            continue
        columns = {
            column["name"]
            for column in inspector.get_columns(table.name, schema=table.schema)
        }
        missing.extend(
            f"{table.fullname}.{column.name}"
            for column in table.columns
            if column.name not in columns
        )
        indexes, unique = _inventory(connection, table)
        for index in table.indexes:
            name = _index_name(connection, index)
            if name not in indexes or not _index_matches(index, indexes[name]):
                missing.append(f"{table.fullname}.{name} (index)")
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                required = frozenset(column.name for column in constraint.columns)
                if required not in unique:
                    missing.append(
                        f"{table.fullname}.{constraint.name or ','.join(sorted(required))} (unique)"
                    )
    if missing:
        raise SchemaUpgradeRequired(
            "Registered schema objects are missing or incompatible after the recorded migrations: "
            + ", ".join(missing)
            + ". Add an explicit versioned migration; startup will not repair silently."
        )
