"""Versioned, additive replacement for the former startup schema patcher.

Keep this revision immutable once released; subsequent changes need a new revision.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy.schema import CreateColumn

REVISION = "20260908_01_runtime_schema"
DESCRIPTION = (
    "Journal legacy runtime columns, indexes, backfills and retained inspection source"
)


def integer(default=None):
    return Column(
        Integer, server_default=text(str(default)) if default is not None else None
    )


def string(length, default=None):
    return Column(
        String(length),
        server_default=text(repr(default)) if default is not None else None,
    )


COLUMNS = {
    "db_instance": {
        "system_name": string(100),
        "db_version": string(100),
        "patch_info": Column(Text),
        "schema_name": string(100),
        "sslmode": string(30),
        "encrypt": string(20),
        "trust_server_certificate": string(20),
        "driver_name": string(100),
        "certificate_profile_id": integer(),
        "tls_server_name": string(255),
        "tls_verify": integer(1),
        "account_type": string(30, "system"),
        "architecture_type": string(50),
        "role_type": string(50),
        "ssh_host": string(100),
        "ssh_port": integer(22),
        "ssh_username": string(100),
        "ssh_password_cipher": Column(Text),
        "password_rotated_at": Column(DateTime),
        "ssh_password_rotated_at": Column(DateTime),
        "tenant_id": integer(),
        "organization_id": integer(),
    },
    **{
        name: {"tenant_id": integer(), "organization_id": integer()}
        for name in ("asset_host", "asset_cluster", "asset_instance")
    },
    "notify_event": {"event_key": string(160)},
    # NULL marks rows needing backfill after a MySQL implicit DDL commit or crash.
    "notify_log": {"attempt_count": integer()},
    "itsm_ticket": {"idempotency_key": string(160)},
    "archive_lifecycle_request": {"approval_request_id": integer()},
    "backup_operation": {
        "duration_seconds": integer(),
        "policy_id": integer(),
        "drill_policy_id": integer(),
    },
    "automation_plan": {
        "idempotency_key": string(160),
        "plan_kind": string(30, "change"),
        "source_plan_id": integer(),
        "risk_level": string(20, "medium"),
        "maintenance_window_start": Column(DateTime),
        "maintenance_window_end": Column(DateTime),
        "timeout_seconds": integer(300),
        "max_retries": integer(0),
        "retry_interval_seconds": integer(10),
        "expected_target_identity": string(255),
        "validation_script": Column(Text),
        "validation_sha256": string(64),
        "created_by": string(100),
    },
    "automation_approval_request": {
        "required_approvals": integer(1),
        "approval_count": integer(0),
        "cancelled_at": Column(DateTime),
        "cancelled_by": string(100),
        "cancellation_reason": Column(Text),
    },
    "automation_execution_run": {
        "idempotency_key": string(160),
        "attempt_count": integer(0),
        "target_identity": string(255),
        "validation_status": string(30),
        "validation_message": Column(Text),
        "error_message": Column(Text),
        "cancel_requested": integer(0),
    },
    "automation_execution_output": {"output_sha256": string(64)},
    "automation_rollback_plan": {
        "created_by": string(100),
        "prepared_plan_id": integer(),
    },
    "db_alert_event": {
        "fingerprint": string(64),
        "correlation_key": string(200),
        "occurrence_count": integer(1),
        "source_count": integer(1),
        "acknowledged_at": Column(DateTime),
        "acknowledged_by": string(100),
        "assigned_at": Column(DateTime),
        "assigned_to": string(100),
        "suppressed_until": Column(DateTime),
        "suppress_reason": string(500),
        "maintenance_window_id": integer(),
        "sla_due_at": Column(DateTime),
        "sla_breached_at": Column(DateTime),
        "impact_json": Column(Text),
        "tenant_id": integer(),
        "organization_id": integer(),
    },
    "db_alert_action_log": {
        "idempotency_key": string(160),
        "attempt_count": integer(0),
        "max_retries": integer(3),
        "next_retry_time": Column(DateTime),
        "dead_letter_at": Column(DateTime),
        "update_time": Column(DateTime),
    },
    "incident_agent_command": {
        "signature_algorithm": string(30, "hmac-sha256"),
        "signing_key_id": string(64),
    },
    "db_capacity_alert": {
        "isolate_until": Column(DateTime),
        "isolate_reason": string(500),
        "occurrence_count": integer(1),
        "first_seen": Column(DateTime),
        "last_seen": Column(DateTime),
        "resolved_at": Column(DateTime),
    },
    "db_capacity_policy": {
        "last_status": string(20),
        "last_message": Column(Text),
        "last_success_time": Column(DateTime),
        "consecutive_failures": integer(0),
        "last_sample_count": integer(0),
        "data_freshness_status": string(20, "unknown"),
    },
    "db_awr_collect_policy": {"last_status": string(20), "last_message": Column(Text)},
    "db_perf_metric_policy": {"last_status": string(20), "last_message": Column(Text)},
    "db_object_size_daily": {"capacity_limit_known": integer()},
    "db_mysql_recovery_plan": {
        "restore_location": string(50, "local"),
        "restore_target": string(50, "bak_table"),
        "source_database": string(128),
        "source_table": string(128),
        "target_instance_id": Column(BigInteger),
        "target_database": string(128),
        "target_table": string(128),
        "temp_database": string(128),
        "precheck_result": Column(JSON),
        "verify_sql": Column(JSON),
        "compare_sql": Column(JSON),
        "writeback_sql": Column(JSON),
    },
    "db_awr_report": {
        "host_name": string(100, ""),
        "performance_score": Column(Float),
        "cpu_score": Column(Float),
        "memory_score": Column(Float),
        "io_score": Column(Float),
        "analysis_results": Column(JSON),
        "recommendations": Column(JSON),
    },
}
for _columns in COLUMNS.values():
    for _name, _column in _columns.items():
        _column.name = _name


INDEXES = (
    ("notify_event", "uq_notify_event_event_key", ("event_key",), True),
    (
        "itsm_ticket",
        "ix_itsm_ticket_config_id_idempotency_key",
        ("config_id", "idempotency_key"),
        False,
    ),
    (
        "incident_agent",
        "ix_incident_agent_auth_token_hash",
        ("auth_token_hash",),
        False,
    ),
)
BACKFILL_DEFAULTS = {
    ("notify_log", "attempt_count"),
    ("db_object_size_daily", "capacity_limit_known"),
}


def add_column_sql(dialect, table_name, column):
    quote = dialect.identifier_preparer.quote
    if dialect.name == "sqlite" and (table_name, column.name) in BACKFILL_DEFAULTS:
        column = Column(column.name, column.type, server_default=text("1"))
    return f"ALTER TABLE {quote(table_name)} ADD COLUMN {CreateColumn(column).compile(dialect=dialect)}"


def task_id_type_sql(dialect, *, nullable: bool) -> str | None:
    if dialect.name == "sqlite":
        return None
    if dialect.name == "postgresql":
        return (
            "ALTER TABLE db_check_result ALTER COLUMN task_id "
            "TYPE VARCHAR(30) USING CAST(task_id AS VARCHAR(30))"
        )
    if dialect.name in {"mysql", "mariadb"}:
        return "ALTER TABLE db_check_result MODIFY COLUMN task_id VARCHAR(30)" + (
            " NULL" if nullable else " NOT NULL"
        )
    raise ValueError(f"unsupported platform database dialect: {dialect.name}")


def _copy_legacy_results(connection):
    # Integrity repair only: retain this revision's schema and original source.
    # Resumed copies must compare content, not infer safety from equal row counts.
    from app.migrations.inspection_result_integrity import copy_initial_legacy_results

    copy_initial_legacy_results(connection)


def upgrade(connection):
    added = set()
    inspector = inspect(connection)
    if inspector.has_table("db_check_result_new") and not inspector.has_table(
        "db_check_result"
    ):
        from app.models.check_result import DBCheckResult

        DBCheckResult.__table__.create(bind=connection, checkfirst=True)
    for table_name, expected in COLUMNS.items():
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name, column in expected.items():
            if name not in existing:
                connection.execute(
                    text(add_column_sql(connection.dialect, table_name, column))
                )
                added.add((table_name, name))

    for table_name, index_name, columns, unique in INDEXES:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        if not set(columns).issubset(existing):
            continue
        if index_name not in {
            index["name"] for index in inspector.get_indexes(table_name)
        }:
            # Do not discard duplicate business keys to manufacture a unique index.
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(
                text(
                    f"CREATE {'UNIQUE ' if unique else ''}INDEX {quote(index_name)} "
                    f"ON {quote(table_name)} ({', '.join(quote(name) for name in columns)})"
                )
            )

    inspector = inspect(connection)
    if inspector.has_table("db_check_result"):
        columns = {
            column["name"]: column
            for column in inspector.get_columns("db_check_result")
        }
        column = columns.get("task_id")
        if column and isinstance(column["type"], Integer):
            statement = task_id_type_sql(
                connection.dialect, nullable=column["nullable"]
            )
            if statement:
                if column.get("default") is not None:
                    raise RuntimeError(
                        "task_id has a legacy default; migrate its type and default explicitly"
                    )
                connection.execute(text(statement))

    # NULL-only backfills can resume after MySQL's implicit DDL commits.
    if inspector.has_table("notify_log"):
        columns = {column["name"] for column in inspector.get_columns("notify_log")}
        value = "COALESCE(retry_count, 0) + 1" if "retry_count" in columns else "1"
        condition = (
            "1 = 1"
            if connection.dialect.name == "sqlite"
            and ("notify_log", "attempt_count") in added
            else "attempt_count IS NULL"
        )
        connection.execute(
            text(f"UPDATE notify_log SET attempt_count = {value} WHERE {condition}")
        )
    if inspector.has_table("db_object_size_daily"):
        condition = (
            "1 = 1"
            if connection.dialect.name == "sqlite"
            and ("db_object_size_daily", "capacity_limit_known") in added
            else "capacity_limit_known IS NULL"
        )
        connection.execute(
            text(
                "UPDATE db_object_size_daily SET capacity_limit_known = CASE "
                "WHEN LOWER(db_type) IN ('mysql', 'mariadb', 'postgresql', 'postgres', 'pg') THEN 0 ELSE 1 END "
                f"WHERE {condition}"
            )
        )
    if connection.dialect.name != "sqlite":
        for table_name, name in sorted(BACKFILL_DEFAULTS):
            inspector = inspect(connection)
            if inspector.has_table(table_name):
                column = next(
                    item
                    for item in inspector.get_columns(table_name)
                    if item["name"] == name
                )
                if column.get("default") is None:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table_name} ALTER COLUMN {name} SET DEFAULT 1"
                        )
                    )
    # No DDL after this point: copied rows and their runtime revision commit together.
    _copy_legacy_results(connection)
