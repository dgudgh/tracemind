"""Add nullable schema identity without rewriting or inventing old samples."""
from sqlalchemy import JSON, String, Text, inspect

REVISION = "20260911_01_sql_history_identity"
DESCRIPTION = "Preserve SQL history schema and collection target identity"
TABLE = "db_sql_plan_history"
ADDITIONS = (
    (TABLE, "schema_name", "VARCHAR(128)", 128),
    (TABLE, "target_fingerprint", "VARCHAR(64)", 64),
    (TABLE, "target_snapshot", "JSON", None),
    ("db_sql_profile", "target_fingerprint", "VARCHAR(64)", 64),
)


def upgrade(connection):
    inspector = inspect(connection)
    if connection.dialect.name not in {"sqlite", "mysql", "mariadb", "postgresql"}:
        raise RuntimeError("Unsupported SQL history migration dialect")
    pending = []
    for table, name, ddl_type, length in ADDITIONS:
        if not inspector.has_table(table):
            continue
        columns = {item["name"]: item for item in inspector.get_columns(table)}
        if name not in columns:
            pending.append((table, name, ddl_type))
            continue
        column = columns[name]
        column_type = column["type"]
        valid_type = isinstance(column_type, (JSON, Text)) if length is None else (
            isinstance(column_type, String)
            and (column_type.length is None or column_type.length >= length)
        )
        if not valid_type or not column["nullable"]:
            raise RuntimeError(f"Unexpected SQL history {table}.{name} definition")
    for table, name, ddl_type in pending:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type} NULL")


def downgrade(connection):
    # Older application code ignores the additive column; retain evidence on rollback.
    return None
