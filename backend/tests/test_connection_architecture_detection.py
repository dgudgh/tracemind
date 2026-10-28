import unittest
from unittest.mock import MagicMock, patch

from app.collectors.mysql.mysql_connection import (
    _replica_peer_endpoint_candidates,
    test_mysql_connection as run_mysql_connection_test,
)
from app.collectors.oracle.oracle_connection import (
    _oracle_full_version,
    _oracle_interval_seconds,
    test_oracle_connection as run_oracle_connection_test,
)
from app.collectors.postgresql.postgresql_connection import (
    test_postgresql_connection as run_postgresql_connection_test,
)
from app.collectors.sqlserver.sqlserver_connection import (
    test_sqlserver_connection as run_sqlserver_connection_test,
)
from app.cmdb.replication import upstream_evidence
from app.models.db_instance import DBInstance


def connection_context(fetch_rows):
    cursor = MagicMock()
    cursor.fetchone.side_effect = fetch_rows
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    context = MagicMock()
    context.__enter__.return_value = connection
    return context, cursor


class ConnectionArchitectureDetectionTest(unittest.TestCase):
    def test_oracle_full_version_prefers_patch_release(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("19.3.0.0.0",)

        version = _oracle_full_version(cursor, "19.0.0.0.0")

        self.assertEqual(version, "19.3.0.0.0")
        cursor.execute.assert_called_once_with("SELECT version_full FROM v$instance")

    def test_oracle_interval_is_normalized_to_seconds(self):
        self.assertEqual(_oracle_interval_seconds("+00 00:00:00"), 0)
        self.assertEqual(_oracle_interval_seconds("+02 03:04:05.500"), 183845.5)
        self.assertEqual(_oracle_interval_seconds("00:00:07"), 7)
        self.assertEqual(_oracle_interval_seconds("-00 00:00:02"), -2)
        self.assertIsNone(_oracle_interval_seconds(""))
        self.assertIsNone(_oracle_interval_seconds("UNKNOWN"))

    def test_mysql_primary_reports_discovered_replicas(self):
        instance = DBInstance(
            instance_name="mysql-primary",
            db_type="mysql",
            host="198.51.100.152",
            port=3306,
            username="monitor",
            password_cipher="secret",
            account_type="system",
        )
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, args=None):
            state["sql"] = sql

        def fetchone():
            sql = state["sql"]
            if sql.startswith("SELECT @@hostname"):
                return {
                    "hostname": "orch-mysql01",
                    "port": 3306,
                    "version": "8.0.44",
                    "server_id": 3232261784,
                    "server_uuid": "primary-uuid",
                    "read_only": 0,
                    "super_read_only": 0,
                }
            return {"ok": 1}

        def fetchall():
            sql = state["sql"]
            if sql.startswith("SHOW GLOBAL VARIABLES WHERE"):
                return []
            if sql == "SHOW REPLICAS":
                return [
                    {"server_id": 3232261785, "host": "orch-mysql02", "port": 3306, "replica_uuid": "replica-2"},
                    {"server_id": 3232261786, "host": "orch-mysql03", "port": 3306, "replica_uuid": "replica-3"},
                ]
            return []

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.mysql.mysql_connection.mysql_connection", return_value=context):
            result = run_mysql_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["architecture_type"], "replication")
        self.assertEqual(architecture["role_type"], "primary")
        self.assertEqual(result["version"], "8.0.44")
        self.assertEqual([member["host"] for member in architecture["members"]], ["orch-mysql02", "orch-mysql03"])
        self.assertTrue(all(member["role_type"] == "replica" for member in architecture["members"]))
        self.assertNotIn("peer_endpoint_candidates", architecture)
        self.assertFalse(any(call.args[0] == "SHOW PROCESSLIST" for call in cursor.execute.call_args_list))

    def test_mysql_missing_replica_hosts_report_unpaired_processlist_candidates(self):
        instance = DBInstance(
            instance_name="mysql-primary",
            db_type="mysql",
            host="192.168.102.152",
            port=3306,
            username="monitor",
            password_cipher="secret",
            account_type="system",
        )
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, args=None):
            state["sql"] = sql

        def fetchone():
            if state["sql"].startswith("SELECT @@hostname"):
                return {
                    "hostname": "mysql-primary",
                    "port": 3306,
                    "version": "8.0.44",
                    "server_id": 1523306,
                    "server_uuid": "primary-uuid",
                    "read_only": 0,
                    "super_read_only": 0,
                }
            return {"ok": 1}

        def fetchall():
            sql = state["sql"]
            if sql.startswith("SHOW GLOBAL VARIABLES WHERE"):
                return []
            if sql == "SHOW REPLICAS":
                return [
                    {"server_id": 1533306, "host": "", "port": 3306, "replica_uuid": "replica-153"},
                    {"server_id": 1543306, "host": None, "port": 3306, "replica_uuid": "replica-154"},
                ]
            if sql == "SHOW PROCESSLIST":
                return [
                    {"Id": 12, "Host": "192.168.102.153:44518", "Command": "Binlog Dump GTID"},
                    {"Id": 13, "Host": "[2001:db8::154]:47060", "Command": "binlog dump"},
                    {"Id": 14, "Host": "192.168.102.153:44518", "Command": "BINLOG DUMP GTID"},
                    {"Id": 141, "Host": "2001:db8::154:47060", "Command": "Binlog Dump GTID"},
                    {"Id": 15, "Host": "replica.example:47061", "Command": "Binlog Dump GTID"},
                    {"Id": 16, "Host": "192.168.102.155:47062", "Command": "Sleep"},
                ]
            return []

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.mysql.mysql_connection.mysql_connection", return_value=context):
            result = run_mysql_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual([member["host"] for member in architecture["members"]], ["", ""])
        self.assertTrue(all(member["health_status"] == "unknown" for member in architecture["members"]))
        self.assertEqual(
            architecture["peer_endpoint_candidates"],
            [
                {"host": "192.168.102.153", "port": 3306},
                {"host": "2001:db8::154", "port": 3306},
            ],
        )

    def test_mysql_processlist_candidates_are_bounded(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {
                "host": f"192.0.2.{index}:45000",
                "command": "Binlog Dump GTID",
            }
            for index in range(1, 21)
        ]

        candidates = _replica_peer_endpoint_candidates(cursor, [3306, 3307])

        cursor.execute.assert_called_once_with("SHOW PROCESSLIST")
        self.assertEqual(len(candidates), 32)
        self.assertEqual(candidates[0], {"host": "192.0.2.1", "port": 3306})
        self.assertEqual(candidates[1], {"host": "192.0.2.1", "port": 3307})
        self.assertEqual(candidates[-1], {"host": "192.0.2.16", "port": 3307})

    def test_mysql_processlist_failure_keeps_unknown_replica_architecture(self):
        instance = DBInstance(
            instance_name="mysql-primary",
            db_type="mysql",
            host="192.168.102.152",
            port=3306,
            username="monitor",
            password_cipher="secret",
            account_type="system",
        )
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, args=None):
            state["sql"] = sql
            if sql == "SHOW PROCESSLIST":
                raise PermissionError("PROCESS privilege required")

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = lambda: (
            {
                "hostname": "mysql-primary",
                "port": 3306,
                "version": "8.0.44",
                "server_id": 1523306,
                "server_uuid": "primary-uuid",
                "read_only": 0,
                "super_read_only": 0,
            }
            if state["sql"].startswith("SELECT @@hostname")
            else {"ok": 1}
        )
        cursor.fetchall.side_effect = lambda: (
            [{"server_id": 1533306, "host": "", "port": 3306, "replica_uuid": "replica-153"}]
            if state["sql"] == "SHOW REPLICAS"
            else []
        )
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.mysql.mysql_connection.mysql_connection", return_value=context):
            result = run_mysql_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["architecture_type"], "replication")
        self.assertEqual(architecture["members"][0]["host"], "")
        self.assertEqual(architecture["members"][0]["health_status"], "unknown")
        self.assertNotIn("peer_endpoint_candidates", architecture)
        self.assertTrue(any("副本端点候选探测失败" in warning for warning in result["warnings"]))

    def test_mysql_replica_exposes_single_channel_upstream_evidence(self):
        instance = DBInstance(
            instance_name="mysql-replica",
            db_type="mysql",
            host="192.168.102.153",
            port=3306,
            username="monitor",
            password_cipher="secret",
            account_type="system",
        )
        state = {"sql": ""}
        cursor = MagicMock()
        replica_status_rows = [{
            "Channel_Name": "",
            "Source_Host": "192.168.102.152",
            "Source_Port": 3307,
            "Source_UUID": "primary-uuid",
            "Source_Server_Id": 1523307,
            "Source_User": "replication_user",
            "Replica_IO_Running": "Yes",
            "Replica_SQL_Running": "Yes",
            "Seconds_Behind_Source": 0,
        }]

        cursor.execute.side_effect = lambda sql, args=None: state.update(sql=sql)
        cursor.fetchone.side_effect = lambda: (
            {
                "hostname": "mysql-replica",
                "port": 3306,
                "version": "8.0.44",
                "server_id": 1533306,
                "server_uuid": "replica-uuid",
                "read_only": 1,
                "super_read_only": 1,
            }
            if state["sql"].startswith("SELECT @@hostname")
            else {"ok": 1}
        )

        def fetchall():
            sql = state["sql"]
            if sql == "SHOW REPLICA STATUS":
                return replica_status_rows
            return []

        cursor.fetchall.side_effect = fetchall
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.mysql.mysql_connection.mysql_connection", return_value=context):
            result = run_mysql_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["upstream_host"], "192.168.102.152")
        self.assertEqual(architecture["upstream_port"], 3307)
        self.assertEqual(architecture["upstream_uuid"], "primary-uuid")
        self.assertEqual(architecture["upstream_server_id"], 1523307)
        self.assertNotIn("source_user", architecture)
        self.assertEqual(
            upstream_evidence(architecture),
            {
                "state": "verified",
                "upstream": {
                    "host": "192.168.102.152",
                    "port": 3307,
                    "server_uuid": "primary-uuid",
                    "server_id": 1523307,
                },
                "evidence_source": "upstream_metadata",
            },
        )

        replica_status_rows.append({
            "Channel_Name": "analytics",
            "Source_Host": "192.168.102.151",
            "Source_Port": 3308,
            "Source_UUID": "analytics-primary-uuid",
            "Source_Server_Id": 1513308,
            "Replica_IO_Running": "Yes",
            "Replica_SQL_Running": "Yes",
        })
        with patch("app.collectors.mysql.mysql_connection.mysql_connection", return_value=context):
            multi_channel = run_mysql_connection_test(instance)["architecture"]

        for field in ("upstream_host", "upstream_port", "upstream_uuid", "upstream_server_id"):
            self.assertNotIn(field, multi_channel)
        self.assertEqual(upstream_evidence(multi_channel)["state"], "derived")

    def test_postgresql_reports_streaming_replica_role(self):
        instance = DBInstance(
            instance_name="pg-replica",
            db_type="postgresql",
            host="pg.example",
            port=5432,
            username="monitor",
            password_cipher="secret",
            database_name="postgres",
        )
        context, cursor = connection_context([
            {"version": "PostgreSQL 16", "database_name": "postgres", "current_user": "monitor"},
            {"in_recovery": True},
            {"cnt": 4},
            {"cnt": 2},
        ])
        with patch("app.collectors.postgresql.postgresql_connection.postgresql_connection", return_value=context):
            result = run_postgresql_connection_test(instance)

        self.assertEqual(result["architecture"]["architecture_type"], "streaming_replication")
        self.assertEqual(result["architecture"]["role_type"], "replica")
        self.assertTrue(result["architecture"]["in_recovery"])
        self.assertFalse(any("pg_stat_replication" in call.args[0] for call in cursor.execute.call_args_list))

    def test_postgresql_primary_reports_streaming_members(self):
        instance = DBInstance(
            instance_name="pg-primary",
            db_type="postgresql",
            host="10.0.0.10",
            port=5432,
            username="monitor",
            password_cipher="secret",
            database_name="postgres",
        )
        state = {"sql": ""}
        cursor = MagicMock()
        cursor.execute.side_effect = lambda sql, *args: state.update(sql=sql)
        cursor.fetchone.side_effect = lambda: (
            {"version": "PostgreSQL 16", "database_name": "postgres", "current_user": "monitor"}
            if state["sql"].startswith("SELECT version()")
            else {"in_recovery": False}
            if "pg_is_in_recovery" in state["sql"]
            else {"cnt": 1}
        )
        cursor.fetchall.side_effect = lambda: (
            [
                {
                    "application_name": "pg-standby-1",
                    "client_addr": "10.0.0.11",
                    "client_hostname": None,
                    "state": "streaming",
                    "sync_state": "sync",
                    "replay_lag_seconds": 0,
                }
            ]
            if "FROM pg_stat_replication" in state["sql"]
            else []
        )
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.postgresql.postgresql_connection.postgresql_connection", return_value=context):
            result = run_postgresql_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["architecture_type"], "streaming_replication")
        self.assertEqual(architecture["role_type"], "primary")
        self.assertEqual(architecture["members"][0]["host"], "10.0.0.11")
        self.assertEqual(architecture["members"][0]["role_type"], "replica")

    def test_sqlserver_reports_always_on_secondary_role(self):
        instance = DBInstance(
            instance_name="sqlserver-secondary",
            db_type="sqlserver",
            host="sqlserver.example",
            port=1433,
            username="monitor",
            password_cipher="secret",
            database_name="master",
        )
        context, _ = connection_context([
            {"version": "SQL Server 2022", "database_name": "master", "current_login": "monitor"},
            {"is_hadr_enabled": 1},
            {"role_desc": "SECONDARY"},
            {"cnt": 8},
            {"cnt": 6},
        ])
        with patch("app.collectors.sqlserver.sqlserver_connection.sqlserver_connection", return_value=context):
            result = run_sqlserver_connection_test(instance)

        self.assertEqual(result["architecture"]["architecture_type"], "always_on")
        self.assertEqual(result["architecture"]["role_type"], "replica")
        self.assertEqual(result["architecture"]["availability_group_role"], "SECONDARY")

    def test_sqlserver_reports_availability_group_members(self):
        instance = DBInstance(
            instance_name="sql-primary",
            db_type="sqlserver",
            host="sql01.example",
            port=1433,
            username="monitor",
            password_cipher="secret",
            database_name="master",
        )
        state = {"sql": ""}
        cursor = MagicMock()
        cursor.execute.side_effect = lambda sql, *args: state.update(sql=sql)
        cursor.fetchone.side_effect = lambda: (
            {"version": "SQL Server 2022", "database_name": "master", "current_login": "monitor"}
            if state["sql"].startswith("SELECT @@VERSION")
            else {"is_hadr_enabled": 1}
            if "IsHadrEnabled" in state["sql"]
            else {"role_desc": "PRIMARY", "group_id": "group-1", "group_name": "MES-AG"}
            if "ars.role_desc" in state["sql"]
            else {"cnt": 1}
        )
        cursor.fetchall.side_effect = lambda: (
            [
                {
                    "replica_id": "replica-2",
                    "replica_server_name": "SQL02\\MES",
                    "endpoint_url": "TCP://sql02.example:5022",
                    "is_local": 0,
                    "role_desc": "SECONDARY",
                    "connected_state_desc": "CONNECTED",
                    "synchronization_health_desc": "HEALTHY",
                }
            ]
            if "FROM sys.availability_replicas" in state["sql"]
            else []
        )
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        context = MagicMock()
        context.__enter__.return_value = connection

        with patch("app.collectors.sqlserver.sqlserver_connection.sqlserver_connection", return_value=context):
            result = run_sqlserver_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["topology_group"], "sqlserver:always_on:group-1")
        self.assertEqual(architecture["members"][0]["name"], "SQL02\\MES")
        self.assertEqual(architecture["members"][0]["role_type"], "replica")
        self.assertEqual(architecture["members"][0]["health_status"], "healthy")

    def test_oracle_reports_data_guard_member(self):
        instance = DBInstance(
            instance_name="mesdb",
            db_type="oracle",
            host="10.0.0.20",
            port=1521,
            username="sys",
            password_cipher="secret",
            service_name="mesdb",
            account_type="sysdba",
        )
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, *args):
            state["sql"] = sql
            if "FROM gv$instance" in sql:
                cursor.description = [
                    (name,)
                    for name in (
                        "inst_id",
                        "instance_number",
                        "instance_name",
                        "host_name",
                        "host_ip",
                        "status",
                        "version",
                        "thread#",
                    )
                ]
            elif "FROM v$dataguard_config" in sql:
                cursor.description = [("db_unique_name",)]
            elif "FROM v$archive_dest_status" in sql:
                cursor.description = [(name,) for name in ("dest_id", "status", "type", "database_mode", "recovery_mode", "gap_status", "db_unique_name", "destination")]
            elif "FROM v$dataguard_stats" in sql:
                cursor.description = [
                    (name,)
                    for name in ("name", "value", "unit", "time_computed", "datum_time")
                ]

        def fetchone():
            sql = state["sql"]
            if "name, db_unique_name, database_role" in sql:
                return ("MESDB", "mesdb", "PRIMARY", "READ WRITE", "MAXIMUM PERFORMANCE")
            if "instance_name, host_name, version, status FROM v$instance" in sql:
                return ("mesdb1", "db01", "19.0.0.0.0", "OPEN")
            return (1,)

        def fetchall():
            sql = state["sql"]
            if "FROM gv$instance" in sql:
                return [
                    (1, 1, "mesdb1", "db01", "10.0.0.11", "OPEN", "19.0.0.0.0", 1)
                ]
            if "FROM v$dataguard_config" in sql:
                return [("mesdb",), ("reportdb",)]
            if "FROM v$archive_dest_status" in sql:
                return [(2, "VALID", "PHYSICAL", "MOUNTED-STANDBY", "MANAGED REAL TIME APPLY", "NO GAP", "reportdb", "reportdb")]
            if "FROM v$dataguard_stats" in sql:
                return [
                    ("apply finish time", "+00 00:00:03.500", "interval", "07/24/2026 10:36:52", None),
                    ("apply lag", "+00 00:00:02", "interval", "07/24/2026 10:36:52", "07/24/2026 10:36:50"),
                    ("transport lag", "+00 00:00:01", "interval", "07/24/2026 10:36:52", "07/24/2026 10:36:51"),
                ]
            return []

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall
        connection = MagicMock()
        connection.cursor.return_value = cursor
        context = MagicMock()
        context.__enter__.return_value = connection

        with (
            patch("app.collectors.oracle.oracle_connection.oracle_connection", return_value=context),
            patch("app.collectors.oracle.oracle_connection.build_oracle_dsn", return_value="oracle-dsn"),
        ):
            result = run_oracle_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["architecture_type"], "data_guard")
        self.assertEqual(architecture["topology_group"], "oracle:data_guard:mesdb:reportdb")
        self.assertEqual(architecture["members"][0]["name"], "reportdb")
        self.assertEqual(architecture["members"][0]["role_type"], "standby")
        self.assertEqual(architecture["members"][0]["health_status"], "healthy")
        self.assertEqual(architecture["transport_lag_seconds"], 1)
        self.assertEqual(architecture["apply_lag_seconds"], 2)
        self.assertEqual(architecture["replication_delay_seconds"], 2)
        self.assertEqual(architecture["lag_time_computed"], "07/24/2026 10:36:52")

    def test_oracle_reports_rac_instances(self):
        instance = DBInstance(
            instance_name="mesdb-rac",
            db_type="oracle",
            host="scan.example",
            port=1521,
            username="sys",
            password_cipher="secret",
            service_name="mesdb",
            account_type="sysdba",
        )
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, *args):
            state["sql"] = sql
            if "FROM gv$instance" in sql:
                cursor.description = [
                    (name,)
                    for name in (
                        "inst_id",
                        "instance_number",
                        "instance_name",
                        "host_name",
                        "host_ip",
                        "status",
                        "version",
                        "thread#",
                    )
                ]
            elif "FROM v$dataguard_config" in sql:
                cursor.description = [("db_unique_name",)]
            elif "FROM v$archive_dest_status" in sql:
                cursor.description = [(name,) for name in ("dest_id", "status", "type", "database_mode", "recovery_mode", "gap_status", "db_unique_name", "destination")]

        def fetchone():
            if "name, db_unique_name, database_role" in state["sql"]:
                return ("MESDB", "mesdb", "PRIMARY", "READ WRITE", "MAXIMUM PERFORMANCE")
            if "instance_name, host_name, version, status FROM v$instance" in state["sql"]:
                return ("mesdb1", "db01", "19.0.0.0.0", "OPEN")
            return (1,)

        def fetchall():
            if "FROM gv$instance" in state["sql"]:
                return [
                    (1, 1, "mesdb1", "db01", "10.0.0.11", "OPEN", "19.0.0.0.0", 1),
                    (2, 2, "mesdb2", "db02", "10.0.0.12", "OPEN", "19.0.0.0.0", 2),
                ]
            if "FROM v$dataguard_config" in state["sql"]:
                return [("mesdb",)]
            return []

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall
        connection = MagicMock()
        connection.cursor.return_value = cursor
        context = MagicMock()
        context.__enter__.return_value = connection

        with (
            patch("app.collectors.oracle.oracle_connection.oracle_connection", return_value=context),
            patch("app.collectors.oracle.oracle_connection.build_oracle_dsn", return_value="oracle-dsn"),
        ):
            result = run_oracle_connection_test(instance)

        architecture = result["architecture"]
        self.assertEqual(architecture["architecture_type"], "rac")
        self.assertEqual(architecture["instance_count"], 2)
        self.assertEqual(architecture["host_ip"], "10.0.0.11")
        self.assertEqual(architecture["members"][0]["name"], "mesdb2")
        self.assertEqual(architecture["members"][0]["host"], "10.0.0.12")
        self.assertEqual(architecture["members"][0]["reported_host"], "db02")
        self.assertEqual(architecture["members"][0]["health_status"], "healthy")


if __name__ == "__main__":
    unittest.main()
