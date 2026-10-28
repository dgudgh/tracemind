"""Oracle topology endpoint metadata tests; all database cursors are synthetic."""

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from app.collectors.oracle.oracle_connection import (
    _oracle_destination_metadata,
    _parse_oracle_connect_identifier,
    test_oracle_connection as run_oracle_connection_test,
)
from app.models.db_instance import DBInstance


def test_easy_connect_is_the_only_bare_value_promoted_to_network_endpoint():
    metadata = _parse_oracle_connect_identifier(
        "SERVICE=//dg02.example:1522/reporting ASYNC DB_UNIQUE_NAME=reportdb"
    )
    assert metadata == {
        "host": "dg02.example",
        "port": 1522,
        "oracle_connect_type": "service_name",
        "service_name": "reporting",
        "sid": "",
        "connect_identifier": "",
        "endpoint_unresolved": False,
        "endpoint_candidates": [],
    }
    alias = _parse_oracle_connect_identifier("SERVICE=REPORTDB_TNS ASYNC")
    assert alias["host"] == ""
    assert alias["connect_identifier"] == "REPORTDB_TNS"
    assert alias["endpoint_unresolved"] is True


@pytest.mark.parametrize(
    ("connect_data", "connect_type", "service_name", "sid"),
    [
        ("(SERVICE_NAME=reporting.example)", "service_name", "reporting.example", ""),
        ("(SID=REPORT2)", "sid", "", "REPORT2"),
    ],
)
def test_single_address_descriptor_freezes_host_port_and_connect_identity(
    connect_data, connect_type, service_name, sid
):
    descriptor = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.22)(PORT=1526))"
        f"(CONNECT_DATA={connect_data}))"
    )
    metadata = _parse_oracle_connect_identifier(descriptor)
    assert (metadata["host"], metadata["port"]) == ("192.0.2.22", 1526)
    assert metadata["oracle_connect_type"] == connect_type
    assert (metadata["service_name"], metadata["sid"]) == (service_name, sid)
    assert metadata["connect_identifier"] == ""
    assert metadata["endpoint_unresolved"] is False
    assert descriptor not in str(metadata)


def test_multi_address_descriptor_preserves_candidates_without_selecting_first():
    descriptor = (
        "(DESCRIPTION=(ADDRESS_LIST="
        "(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.31)(PORT=1521))"
        "(ADDRESS=(PROTOCOL=TCP)(HOST=192.0.2.32)(PORT=1522)))"
        "(CONNECT_DATA=(SERVICE_NAME=reporting)))"
    )
    metadata = _oracle_destination_metadata(descriptor)
    assert metadata["host"] == "" and metadata["port"] is None
    assert metadata["endpoint_unresolved"] is True
    assert metadata["endpoint_candidates"] == [
        {"host": "192.0.2.31", "port": 1521},
        {"host": "192.0.2.32", "port": 1522},
    ]
    assert metadata["oracle_connect_type"] == "service_name"
    assert metadata["service_name"] == "reporting"


def _oracle_instance():
    return DBInstance(
        instance_name="mesdb-seed", db_type="oracle", host="scan.example", port=1521,
        username="monitor", password_cipher="not-used", account_type="system",
        oracle_connect_type="service_name", service_name="mesdb.example",
    )


def _run_with_cursor(cursor):
    connection = MagicMock()
    connection.cursor.return_value = cursor
    capability = {
        "capability": "standard_inspection", "capability_label": "SYSTEM 标准巡检",
        "warnings": [],
    }
    with (
        patch("app.collectors.oracle.oracle_connection.oracle_connection",
              return_value=nullcontext(connection)),
        patch("app.collectors.oracle.oracle_connection.build_oracle_dsn",
              return_value="synthetic-dsn"),
        patch("app.collectors.oracle.oracle_connection.probe_oracle_inspection_capability",
              return_value=capability),
    ):
        return run_oracle_connection_test(_oracle_instance())


def test_dataguard_uses_catalog_descriptor_without_publishing_it():
    cursor = MagicMock()
    state = {"sql": ""}
    descriptor = (
        "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=198.51.100.22)(PORT=1528))"
        "(CONNECT_DATA=(SERVICE_NAME=reporting.example)))"
    )

    def execute(sql, *args):
        state["sql"] = sql
        if "FROM gv$instance" in sql:
            cursor.description = [(key,) for key in (
                "inst_id", "instance_number", "instance_name", "host_name",
                "host_ip", "status", "version", "thread#",
            )]
        elif "FROM v$dataguard_config" in sql:
            cursor.description = [("db_unique_name",), ("parent_dbun",), ("dest_role",)]
        elif "FROM v$archive_dest_status" in sql:
            cursor.description = [(key,) for key in (
                "dest_id", "status", "type", "database_mode", "recovery_mode",
                "gap_status", "db_unique_name", "destination",
            )]
        elif "FROM v$archive_dest " in sql:
            cursor.description = [("dest_id",), ("destination",)]
        elif "FROM v$dataguard_stats" in sql:
            cursor.description = [(key,) for key in (
                "name", "value", "unit", "time_computed", "datum_time",
            )]

    def fetchone():
        sql = state["sql"]
        if "FROM v$database" in sql:
            return ("MESDB", "mesdb", "PRIMARY", "READ WRITE", "MAXIMUM PERFORMANCE")
        if "FROM v$instance" in sql and "version_full" not in sql:
            return ("mesdb1", "db01", "19.0.0.0.0", "OPEN")
        if "version_full" in sql:
            return ("19.24.0.0.0",)
        return (1,)

    def fetchall():
        sql = state["sql"]
        if "FROM gv$instance" in sql:
            return [(1, 1, "mesdb1", "db01", "192.0.2.11", "OPEN", "19.0.0.0.0", 1)]
        if "FROM v$dataguard_config" in sql:
            return [("mesdb", None, "PRIMARY DATABASE"), ("reportdb", "mesdb", "PHYSICAL STANDBY")]
        if "FROM v$archive_dest_status" in sql:
            return [(2, "VALID", "PHYSICAL", "MOUNTED-STANDBY", "MANAGED",
                     "NO GAP", "reportdb", "REPORT_TNS")]
        if "FROM v$archive_dest " in sql:
            return [(2, descriptor)]
        return []

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall

    architecture = _run_with_cursor(cursor)["architecture"]
    member = architecture["members"][0]
    assert (member["host"], member["reported_host"], member["port"]) == (
        "198.51.100.22", "198.51.100.22", 1528,
    )
    assert member["db_unique_name"] == "reportdb"
    assert member["oracle_connect_type"] == "service_name"
    assert (member["service_name"], member["sid"]) == ("reporting.example", "")
    assert member["endpoint_unresolved"] is False
    assert member["connect_identifier"] == ""
    assert member["transport_source_db_unique_name"] == "mesdb"
    assert member["transport_destination_id"] == 2
    assert descriptor not in str(architecture)

    def execute_with_catalog_denied(sql, *args):
        execute(sql, *args)
        if "FROM v$archive_dest " in sql:
            raise PermissionError("catalog view denied")

    cursor.execute.side_effect = execute_with_catalog_denied
    degraded_member = _run_with_cursor(cursor)["architecture"]["members"][0]
    assert degraded_member["host"] == ""
    assert degraded_member["reported_host"] == ""
    assert degraded_member["db_unique_name"] == "reportdb"
    assert degraded_member["connect_identifier"] == "REPORT_TNS"
    assert degraded_member["endpoint_unresolved"] is True

    def standby_fetchone():
        if "FROM v$database" in state["sql"]:
            return ("MESDB", "reportdb", "PHYSICAL STANDBY", "READ ONLY WITH APPLY", "MAXIMUM PERFORMANCE")
        return fetchone()

    cursor.fetchone.side_effect = standby_fetchone
    standby_architecture = _run_with_cursor(cursor)["architecture"]
    assert standby_architecture["upstream_db_unique_name"] == "mesdb"
    assert next(m for m in standby_architecture["members"] if m["db_unique_name"] == "mesdb")["role_type"] == "primary"

    def relay_fetchone():
        if "FROM v$database" in state["sql"]:
            return ("MESDB", "relaydb", "PHYSICAL STANDBY", "READ ONLY WITH APPLY", "MAXIMUM PERFORMANCE")
        return fetchone()

    cursor.fetchone.side_effect = relay_fetchone
    relay_members = _run_with_cursor(cursor)["architecture"]["members"]
    assert next(m for m in relay_members if m["db_unique_name"] == "reportdb")["role_type"] == "standby"


def test_rac_member_uses_only_service_observed_active_on_that_instance():
    cursor = MagicMock()
    state = {"sql": ""}

    def execute(sql, *args):
        state["sql"] = sql
        if "FROM gv$instance" in sql:
            cursor.description = [(key,) for key in (
                "inst_id", "instance_number", "instance_name", "host_name",
                "host_ip", "status", "version", "thread#",
            )]
        elif "FROM gv$active_services" in sql:
            cursor.description = [("inst_id",), ("name",), ("network_name",)]
        elif "FROM v$dataguard_config" in sql:
            cursor.description = [("db_unique_name",)]
        elif "FROM v$archive_dest_status" in sql:
            cursor.description = [(key,) for key in (
                "dest_id", "status", "type", "database_mode", "recovery_mode",
                "gap_status", "db_unique_name", "destination",
            )]

    def fetchone():
        sql = state["sql"]
        if "FROM v$database" in sql:
            return ("MESDB", "mesdb", "PRIMARY", "READ WRITE", "MAXIMUM PERFORMANCE")
        if "FROM v$instance" in sql and "version_full" not in sql:
            return ("mesdb1", "db01", "19.0.0.0.0", "OPEN")
        if "version_full" in sql:
            return ("19.24.0.0.0",)
        return (1,)

    def fetchall():
        sql = state["sql"]
        if "FROM gv$instance" in sql:
            return [
                (1, 1, "mesdb1", "db01", "192.0.2.11", "OPEN", "19.0.0.0.0", 1),
                (2, 2, "mesdb2", "db02", "192.0.2.12", "OPEN", "19.0.0.0.0", 2),
            ]
        if "FROM gv$active_services" in sql:
            return [(1, "mesdb", "mesdb.example"), (2, "mesdb", "mesdb.example")]
        if "FROM v$dataguard_config" in sql:
            return [("mesdb",)]
        return []

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall

    member = _run_with_cursor(cursor)["architecture"]["members"][0]
    assert member["instance_name"] == "mesdb2"
    assert member["db_unique_name"] == "mesdb"
    assert member["oracle_connect_type"] == "service_name"
    assert (member["service_name"], member["sid"]) == ("mesdb.example", "")
    assert member["host"] == "192.0.2.12"
    assert member["endpoint_unresolved"] is False
