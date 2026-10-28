import unittest
from unittest.mock import MagicMock, patch

from app.collectors.sqlserver.sqlserver_connection import (
    _hadr_endpoint_identity,
    _replica_server_identity,
    test_sqlserver_connection as run_sqlserver_connection_test,
)
from app.models.db_instance import DBInstance


def _instance() -> DBInstance:
    return DBInstance(
        instance_name="sql-seed",
        db_type="sqlserver",
        host="sql01.example",
        port=1433,
        username="monitor",
        password_cipher="not-used",
        database_name="master",
    )


def _connection_context(cursor):
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor_context
    context = MagicMock()
    context.__enter__.return_value = connection
    return context


class SQLServerDiscoveryPeerMetadataTest(unittest.TestCase):
    def test_secondary_upstream_uses_group_scoped_dmv_and_degrades_on_missing_or_ambiguous_identity(self):
        cases = [
            ([{"availability_group_id":"AG-1","primary_replica":"SQL01\\MES"}], False, "SQL01\\MES"),
            ([{"availability_group_id":"OTHER","primary_replica":"SQL01\\MES"}], False, None),
            ([{"availability_group_id":"AG-1","primary_replica":"SQL01\\MES"},
              {"availability_group_id":"AG-1","primary_replica":"SQL03\\MES"}], False, None),
            ([], False, None), ([], True, None),
            ([{"availability_group_id":"AG-1","primary_replica":"SQL02\\MES"}], False, None),
            ([{"availability_group_id":"AG-1","primary_replica":"SQL01\\MES"},
              {"availability_group_id":"AG-1","primary_replica":"sql01\\mes"}], False, "sql01\\mes"),
        ]
        for rows, denied, expected in cases:
            with self.subTest(rows=rows,denied=denied):
                cursor = MagicMock(); state = {}
                def execute(sql,args=None):
                    state["sql"] = sql
                    if "sys.dm_hadr_availability_group_states" in sql:
                        self.assertEqual(args, ("AG-1",))
                        if denied:raise PermissionError("synthetic permission denial")
                def fetchone():
                    sql=state["sql"]
                    if sql.startswith("SELECT @@VERSION"):
                        return {"version":"SQL Server 2022","server_name":"SQL02\\MES","machine_name":"SQL02","instance_name":"MES","local_tcp_port":1433}
                    if "IsHadrEnabled" in sql:return {"is_hadr_enabled":1}
                    if "SELECT TOP (1)" in sql:return {"role_desc":"SECONDARY","replica_id":"R-2","group_id":"AG-1","group_name":"MES-AG"}
                    return {"cnt":1}
                cursor.execute.side_effect=execute;cursor.fetchone.side_effect=fetchone
                cursor.fetchall.side_effect=lambda: rows if "sys.dm_hadr_availability_group_states" in state["sql"] else []
                with patch("app.collectors.sqlserver.sqlserver_connection.sqlserver_connection",return_value=_connection_context(cursor)):
                    result=run_sqlserver_connection_test(_instance())
                self.assertEqual(result["architecture"].get("upstream_server_name"), expected)
                self.assertEqual(result["architecture"]["availability_group_id"], "AG-1")

    def test_multiple_local_groups_do_not_silently_choose_first_group(self):
        cursor = MagicMock(); state = {}
        cursor.execute.side_effect = lambda sql,args=None: state.update(sql=sql)
        def fetchone():
            sql=state["sql"]
            if sql.startswith("SELECT @@VERSION"):
                return {"version":"SQL Server 2022","server_name":"SQL01","local_tcp_port":1433}
            if "IsHadrEnabled" in sql:return {"is_hadr_enabled":1}
            if "SELECT TOP (1)" in sql:
                self.assertIn("local_group_count",sql)
                return {"role_desc":"PRIMARY","replica_id":"R-1","group_id":"AG-1","group_name":"first","local_group_count":2}
            return {"cnt":1}
        cursor.fetchone.side_effect=fetchone
        with patch("app.collectors.sqlserver.sqlserver_connection.sqlserver_connection",return_value=_connection_context(cursor)):
            result=run_sqlserver_connection_test(_instance())
        architecture=result["architecture"]
        self.assertTrue(architecture["availability_group_ambiguous"])
        self.assertIsNone(architecture["availability_group_id"])
        self.assertEqual(architecture["role_type"],"unknown")
        self.assertEqual(architecture["members"],[])
        self.assertNotIn("upstream_server_name",architecture)
        self.assertTrue(any("多个可用性组" in warning for warning in result["warnings"]))

    def test_replica_server_name_owns_tds_port_not_hadr_endpoint(self):
        self.assertEqual(
            _replica_server_identity("sql02.example,1444"),
            ("sql02.example", None, 1444),
        )
        self.assertEqual(
            _replica_server_identity("SQL03\\MES"),
            ("SQL03", "MES", None),
        )
        self.assertEqual(
            _hadr_endpoint_identity("TCP://sql03.example:5022"),
            ("sql03.example", 5022),
        )

    def test_always_on_members_keep_identity_and_safe_login_candidates(self):
        state = {"sql": "", "args": None}
        cursor = MagicMock()

        def execute(sql, args=None):
            state.update(sql=sql, args=args)

        def fetchone():
            sql = state["sql"]
            if sql.startswith("SELECT @@VERSION"):
                return {
                    "version": "SQL Server 2022",
                    "database_name": "master",
                    "current_login": "monitor",
                    "server_name": "SQL01\\MES",
                    "machine_name": "SQL01",
                    "instance_name": "MES",
                    "local_tcp_port": 1544,
                }
            if "IsHadrEnabled" in sql:
                return {"is_hadr_enabled": 1}
            if "SELECT TOP (1)" in sql:
                return {
                    "role_desc": "PRIMARY",
                    "replica_id": "REPLICA-LOCAL",
                    "group_id": "AG-GROUP-1",
                    "group_name": "MES-AG",
                }
            return {"cnt": 1}

        def fetchall():
            if "FROM sys.availability_replicas" not in state["sql"]:
                return []
            self.assertEqual(state["args"], ("AG-GROUP-1",))
            return [
                {
                    "replica_id": "REPLICA-LOCAL",
                    "availability_group_id": "AG-GROUP-1",
                    "replica_server_name": "SQL01\\MES",
                    "endpoint_url": "TCP://sql01.example:5022",
                    "is_local": 1,
                    "role_desc": "PRIMARY",
                    "connected_state_desc": "CONNECTED",
                    "synchronization_health_desc": "HEALTHY",
                },
                {
                    "replica_id": "REPLICA-EXPLICIT",
                    "availability_group_id": "AG-GROUP-1",
                    "replica_server_name": "sql02.example,1444",
                    "endpoint_url": "TCP://sql02.example:5022",
                    "is_local": 0,
                    "role_desc": "SECONDARY",
                    "connected_state_desc": "CONNECTED",
                    "synchronization_health_desc": "HEALTHY",
                },
                {
                    "replica_id": "REPLICA-NAMED",
                    "availability_group_id": "AG-GROUP-1",
                    "replica_server_name": "SQL03\\REPORTING",
                    "endpoint_url": "TCP://sql03.example:5022",
                    "is_local": 0,
                    "role_desc": None,
                    "connected_state_desc": None,
                    "synchronization_health_desc": None,
                },
            ]

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall

        with patch(
            "app.collectors.sqlserver.sqlserver_connection.sqlserver_connection",
            return_value=_connection_context(cursor),
        ):
            result = run_sqlserver_connection_test(_instance())

        architecture = result["architecture"]
        self.assertEqual(architecture["replica_id"], "REPLICA-LOCAL")
        self.assertEqual(architecture["member_key"], "replica-local")
        self.assertEqual(architecture["availability_group_id"], "AG-GROUP-1")
        self.assertEqual(architecture["server_name"], "SQL01\\MES")
        self.assertEqual(architecture["machine_name"], "SQL01")
        self.assertEqual(architecture["instance_name"], "MES")
        self.assertEqual(architecture["port"], 1544)
        self.assertFalse(architecture["port_inferred"])

        explicit, named = architecture["members"]
        self.assertEqual(explicit["replica_id"], "REPLICA-EXPLICIT")
        self.assertEqual(explicit["server_name"], "sql02.example,1444")
        self.assertEqual(explicit["host"], "sql02.example")
        self.assertEqual(explicit["port"], 1444)
        self.assertFalse(explicit["port_inferred"])
        self.assertEqual(explicit["hadr_endpoint_port"], 5022)
        self.assertEqual(explicit["health_status"], "healthy")

        self.assertEqual(named["replica_id"], "REPLICA-NAMED")
        self.assertEqual(named["server_name"], "SQL03\\REPORTING")
        self.assertEqual(named["host"], "SQL03")
        self.assertEqual(named["instance_name"], "REPORTING")
        self.assertNotIn("\\", named["host"])
        self.assertEqual(named["port"], 1433)
        self.assertTrue(named["port_inferred"])
        self.assertEqual(named["port_source"], "seed_candidate")
        self.assertTrue(named["identity_verification_required"])
        self.assertEqual(named["role_type"], "unknown")
        self.assertEqual(named["health_status"], "unknown")
        self.assertNotEqual(named["port"], named["hadr_endpoint_port"])

    def test_missing_group_identity_does_not_publish_unverified_members(self):
        state = {"sql": ""}
        cursor = MagicMock()
        cursor.execute.side_effect = lambda sql, args=None: state.update(sql=sql)
        cursor.fetchone.side_effect = lambda: (
            {
                "version": "SQL Server 2022",
                "database_name": "master",
                "current_login": "monitor",
                "server_name": "SQL01",
                "machine_name": "SQL01",
                "instance_name": None,
                "local_tcp_port": None,
            }
            if state["sql"].startswith("SELECT @@VERSION")
            else {"is_hadr_enabled": 1}
            if "IsHadrEnabled" in state["sql"]
            else {"role_desc": "SECONDARY"}
            if "SELECT TOP (1)" in state["sql"]
            else {"cnt": 1}
        )

        with patch(
            "app.collectors.sqlserver.sqlserver_connection.sqlserver_connection",
            return_value=_connection_context(cursor),
        ):
            result = run_sqlserver_connection_test(_instance())

        architecture = result["architecture"]
        self.assertEqual(architecture["role_type"], "replica")
        self.assertEqual(architecture["members"], [])
        self.assertTrue(architecture["port_inferred"])
        self.assertEqual(result["capability"], "limited")
        self.assertTrue(any("未取得本地可用性组标识" in warning for warning in result["warnings"]))
        self.assertFalse(any("FROM sys.availability_replicas" in call.args[0] for call in cursor.execute.call_args_list))

    def test_identity_query_failure_falls_back_without_claiming_actual_port(self):
        state = {"sql": ""}
        cursor = MagicMock()

        def execute(sql, args=None):
            state["sql"] = sql
            if "CONNECTIONPROPERTY('local_tcp_port')" in sql:
                raise PermissionError("metadata denied")

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = lambda: (
            {"version": "SQL Server 2019", "database_name": "master", "current_login": "monitor"}
            if state["sql"].startswith("SELECT @@VERSION")
            else {"is_hadr_enabled": 0}
            if "IsHadrEnabled" in state["sql"]
            else {"cnt": 1}
        )

        with patch(
            "app.collectors.sqlserver.sqlserver_connection.sqlserver_connection",
            return_value=_connection_context(cursor),
        ):
            result = run_sqlserver_connection_test(_instance())

        architecture = result["architecture"]
        self.assertEqual(architecture["port"], 1433)
        self.assertTrue(architecture["port_inferred"])
        self.assertIsNone(architecture["server_name"])
        self.assertEqual(result["capability"], "limited")
        self.assertTrue(any("已降级采集" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
