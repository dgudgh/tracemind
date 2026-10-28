"""Widen counter storage without changing the service's JSON numeric contract."""

from __future__ import annotations

import re

from sqlalchemy import BigInteger, Integer, SmallInteger, String, inspect, literal, text


REVISION = "20260910_01_slow_sql_counters"
DESCRIPTION = "Widen slow-SQL counters to BIGINT while retaining existing data and attributes"
TABLE = "db_slow_sql"
COUNTERS = ("executions", "logical_reads", "physical_reads", "rows_processed")


def _unexpected(name: str, detail: str) -> None:
    raise RuntimeError(f"{REVISION}: unexpected {TABLE}.{name}: {detail}")


def _mysql_clauses(connection) -> list[str]:
    # Reflection alone omits some MySQL EXTRA attributes. Read them before any DDL,
    # since MODIFY replaces the definition and MySQL DDL implicitly commits.
    rows = connection.execute(text(
        "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
    ), {"table_name": TABLE}).mappings()
    columns = {row["COLUMN_NAME"]: row for row in rows}
    clauses = []
    for name in COUNTERS:
        if name not in columns:
            _unexpected(name, "missing column")
        column = columns[name]
        data_type = column["DATA_TYPE"].lower()
        column_type = column["COLUMN_TYPE"].lower()
        if data_type not in {"int", "bigint"} or not re.fullmatch(
            rf"{data_type}(?:\([0-9]+\))?", column_type
        ):
            _unexpected(name, f"expected signed INT or BIGINT, found {column_type}")
        if column["EXTRA"]:
            _unexpected(name, f"unsupported EXTRA attribute {column['EXTRA']!r}")
        nullable = column["IS_NULLABLE"]
        if nullable not in {"YES", "NO"}:
            _unexpected(name, "unknown nullability")
        definition = "BIGINT " + ("NULL" if nullable == "YES" else "NOT NULL")
        default = column["COLUMN_DEFAULT"]
        # MariaDB may return SQL NULL as 'NULL', or quote a literal default.
        if default is None or str(default).upper() == "NULL":
            if nullable == "YES":
                definition += " DEFAULT NULL"
        else:
            value = str(default)
            if len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1]
            if not re.fullmatch(r"[+-]?[0-9]+", value) or not (
                -(2**63) <= int(value) <= 2**63 - 1
            ):
                _unexpected(name, "unsupported non-integer or out-of-range default")
            definition += f" DEFAULT {int(value)}"
        comment = column["COLUMN_COMMENT"]
        if comment:
            rendered = literal(comment, type_=String()).compile(
                dialect=connection.dialect, compile_kwargs={"literal_binds": True}
            )
            definition += f" COMMENT {rendered}"
        if data_type == "int":
            clauses.append(f"MODIFY COLUMN {name} {definition}")
    return clauses


def upgrade(connection) -> None:
    dialect = connection.dialect.name
    if dialect not in {"mysql", "mariadb", "postgresql", "sqlite"}:
        raise RuntimeError(f"{REVISION}: unsupported database dialect {dialect}")
    inspector = inspect(connection)
    if not inspector.has_table(TABLE):
        # Initialization runs revisions before creating tables from model metadata.
        return
    if TABLE not in inspector.get_table_names():
        _unexpected("*", "expected a table, not a view or temporary object")

    if dialect in {"mysql", "mariadb"}:
        clauses = _mysql_clauses(connection)
    else:
        columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
        clauses = []
        for name in COUNTERS:
            if name not in columns:
                _unexpected(name, "missing column")
            column = columns[name]
            kind = column["type"]
            if not isinstance(kind, Integer) or isinstance(kind, SmallInteger):
                _unexpected(name, f"expected INTEGER or BIGINT, found {kind}")
            if "computed" in column or "identity" in column:
                _unexpected(name, "generated counters are unsupported")
            if dialect == "postgresql" and not isinstance(kind, BigInteger):
                clauses.append(f"ALTER COLUMN {name} TYPE BIGINT")

    # SQLite INTEGER already stores signed 64-bit values: never rebuild its table.
    # PostgreSQL TYPE retains defaults/nullability/comments/indexes. A mixed schema
    # is safe to resume, including after MySQL DDL succeeded but journaling failed.
    if clauses:
        connection.exec_driver_sql(f"ALTER TABLE {TABLE} " + ", ".join(clauses))


def downgrade(connection) -> None:
    if inspect(connection).has_table(TABLE):
        raise RuntimeError(
            f"{REVISION}: destructive counter downgrade is unsupported while {TABLE} "
            "exists; retain BIGINT columns for application rollback to avoid data loss"
        )
