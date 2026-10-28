"""PostgreSQL discovery metadata checks using synthetic cursors only."""

from unittest.mock import MagicMock, patch

from app.collectors.postgresql.postgresql_connection import (
    test_postgresql_connection as _run_postgresql_connection_test,
)
from app.models.db_instance import DBInstance


def _instance(*, host="198.51.100.10", port=5432):
    return DBInstance(
        instance_name="pg-node",
        db_type="postgresql",
        host=host,
        port=port,
        username="monitor",
        password_cipher="synthetic-secret-reference",
        database_name="postgres",
    )


def _synthetic_context(*, rows_by_sql, execute_error=None):
    state = {"sql": ""}
    cursor = MagicMock()

    def execute(sql, *args):
        state["sql"] = sql
        if execute_error is not None:
            execute_error(sql)

    def rows():
        for marker, value in rows_by_sql.items():
            if marker in state["sql"]:
                return value
        return []

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = lambda: (rows()[0] if rows() else None)
    cursor.fetchall.side_effect = lambda: list(rows())
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    context = MagicMock()
    context.__enter__.return_value = connection
    return context, connection, cursor


def _run(instance, context):
    with patch(
        "app.collectors.postgresql.postgresql_connection.postgresql_connection",
        return_value=context,
    ):
        return _run_postgresql_connection_test(instance)


def test_primary_keeps_application_name_out_of_endpoint_and_marks_port_candidate():
    context, connection, cursor = _synthetic_context(rows_by_sql={
        "SELECT version()": [{
            "version": "PostgreSQL 16.4",
            "database_name": "postgres",
            "current_user": "monitor",
        }],
        "pg_is_in_recovery()": [{
            "in_recovery": False,
            "host_ip": "198.51.100.10",
            "port": 6432,
        }],
        "FROM pg_stat_replication": [
            {
                "application_name": "standby-one",
                "client_addr": "198.51.100.11",
                "client_hostname": "standby-one.internal",
                "client_port": 49152,
                "state": "streaming",
                "sync_state": "async",
                "replay_lag_seconds": None,
            },
            {
                "application_name": "203.0.113.99",
                "client_addr": None,
                "client_hostname": None,
                "client_port": 49153,
                "state": "streaming",
                "sync_state": "async",
                "replay_lag_seconds": None,
            },
        ],
        "pg_stat_activity": [{"cnt": 3}],
        "pg_stat_database": [{"cnt": 1}],
        "pg_control_system()": [{"system_identifier": "7555000111222333444"}],
    })

    result = _run(_instance(port=5432), context)
    architecture = result["architecture"]
    resolved, application_only = architecture["members"]

    assert architecture["host_ip"] == "198.51.100.10"
    assert architecture["port"] == 6432
    assert architecture["system_identifier"] == "7555000111222333444"
    assert architecture["member_key"] == "198.51.100.10:6432"
    assert architecture["topology_group"] == "postgresql:streaming:7555000111222333444"

    assert resolved["host"] == "198.51.100.11"
    assert resolved["port"] == 6432
    assert resolved["port_inferred"] is True
    assert resolved["replication_delay_seconds"] is None
    assert resolved["health_status"] == "unknown"

    assert application_only["application_name"] == "203.0.113.99"
    assert application_only["host"] == ""
    assert application_only["port_inferred"] is True
    assert application_only["member_key"] == "postgresql-application:203.0.113.99"

    replication_sql = next(
        call.args[0]
        for call in cursor.execute.call_args_list
        if "FROM pg_stat_replication" in call.args[0]
    )
    assert "client_port" not in replication_sql
    assert "COALESCE" not in replication_sql.upper()
    assert connection.rollback.call_count == 0
    assert "synthetic-secret-reference" not in str(result)


def test_replica_exposes_single_wal_upstream_without_conninfo_or_fake_health():
    context, connection, cursor = _synthetic_context(rows_by_sql={
        "SELECT version()": [{
            "version": "PostgreSQL 15.8",
            "database_name": "postgres",
            "current_user": "monitor",
        }],
        "pg_is_in_recovery()": [{
            "in_recovery": True,
            "host_ip": "198.51.100.11",
            "port": 6432,
        }],
        "FROM pg_stat_wal_receiver": [{
            "status": "streaming",
            "sender_host": "198.51.100.10",
            "sender_port": 6432,
            "slot_name": "standby_one",
            "conninfo": "password=must-not-be-retained",
        }],
        "pg_stat_activity": [{"cnt": 3}],
        "pg_stat_database": [{"cnt": 1}],
        "pg_control_system()": [{"system_identifier": "7555000111222333444"}],
    })

    result = _run(_instance(host="198.51.100.11", port=5432), context)
    architecture = result["architecture"]

    assert architecture["role_type"] == "replica"
    assert architecture["upstream_host"] == "198.51.100.10"
    assert architecture["upstream_port"] == 6432
    assert architecture["system_identifier"] == "7555000111222333444"
    assert architecture["members"] == [{
        "member_key": "198.51.100.10:6432",
        "name": "198.51.100.10",
        "host": "198.51.100.10",
        "reported_host": "198.51.100.10",
        "port": 6432,
        "role_type": "primary",
        "health_status": "unknown",
        "member_state": "streaming",
        "slot_name": "standby_one",
    }]
    wal_sql = next(
        call.args[0]
        for call in cursor.execute.call_args_list
        if "FROM pg_stat_wal_receiver" in call.args[0]
    )
    assert "conninfo" not in wal_sql.lower()
    assert "password" not in str(result).lower()
    assert connection.rollback.call_count == 0


def test_system_identifier_permission_failure_rolls_back_and_degrades():
    def deny_control_system(sql):
        if "pg_control_system()" in sql:
            raise PermissionError("permission denied for function pg_control_system")

    context, connection, _ = _synthetic_context(
        rows_by_sql={
            "SELECT version()": [{
                "version": "PostgreSQL 14.12",
                "database_name": "postgres",
                "current_user": "monitor",
            }],
            "pg_is_in_recovery()": [{
                "in_recovery": False,
                "host_ip": "198.51.100.10",
                "port": 5432,
            }],
            "FROM pg_stat_replication": [],
            "pg_stat_activity": [{"cnt": 3}],
            "pg_stat_database": [{"cnt": 1}],
        },
        execute_error=deny_control_system,
    )

    result = _run(_instance(), context)

    assert result["architecture"]["architecture_type"] == "standalone"
    assert result["architecture"]["host_ip"] == "198.51.100.10"
    assert "system_identifier" not in result["architecture"]
    assert result["capability"] == "limited"
    assert any("pg_control_system" in warning for warning in result["warnings"])
    connection.rollback.assert_called_once_with()
