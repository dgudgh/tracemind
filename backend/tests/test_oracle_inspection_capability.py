import unittest
from pathlib import Path
from types import SimpleNamespace

from app.collectors.oracle.oracle_connection import probe_oracle_inspection_capability
from app.collectors.oracle.oracle_health_collector import ORACLE_QUERY_ITEM_KEYS
from app.services.inspection.oracle_script_checks import (
    ScriptSqlSection,
    _execute_sections,
    _prepare_sql,
    _skip_reason,
    enabled_oracle_item_keys,
    parse_script_sections,
)
from app.services.inspection.result_writer import account_capability_summary


class _ProbeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql):
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise RuntimeError("ORA-01031: insufficient privileges")

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class _ProbeConnection:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.rollback_count = 0

    def cursor(self):
        return _ProbeCursor(self)

    def rollback(self):
        self.rollback_count += 1


class OracleInspectionCapabilityTest(unittest.TestCase):
    def test_system_account_is_standard_not_sysdba(self):
        capability = probe_oracle_inspection_capability(_ProbeConnection(), "system")

        self.assertEqual(capability["capability"], "standard_inspection")
        self.assertEqual(capability["capability_label"], "SYSTEM 标准巡检")
        self.assertEqual(capability["status"], "normal")
        self.assertEqual(len(capability["sql"]), 3)
        self.assertEqual(capability["row_count"], 3)
        self.assertEqual(
            [row["status"] for row in capability["rows"]],
            ["available", "available", "available"],
        )

    def test_missing_catalog_access_is_reported_as_limited(self):
        connection = _ProbeConnection("dba_hist_snapshot")

        capability = probe_oracle_inspection_capability(connection, "system")

        self.assertEqual(capability["capability"], "limited_inspection")
        self.assertIn("awr_catalog", capability["missing_capabilities"])
        self.assertEqual(connection.rollback_count, 1)

    def test_report_task_uses_probed_capability_label(self):
        row = SimpleNamespace(
            metric_name="account_capability",
            metric_value="standard_inspection",
            check_status="normal",
            raw_data={
                "capability": "standard_inspection",
                "capability_label": "SYSTEM 标准巡检",
                "status": "normal",
            },
        )

        summary = account_capability_summary([row])

        self.assertEqual(summary["account_capability_label"], "SYSTEM 标准巡检")
        self.assertFalse(summary["read_only"])


class OracleScriptExecutionTest(unittest.TestCase):
    def test_noncdb_parser_does_not_extract_sql_from_plsql_blocks(self):
        script = Path(__file__).parents[1] / "scripts" / "dbcheck_noncdb.sql"

        sections = parse_script_sections(script)

        self.assertGreater(len(sections), 50)
        self.assertFalse(any("instinfo_cur." in section.sql.lower() for section in sections))
        self.assertFalse(any("document.getelementbyid" in section.sql.lower() for section in sections))
        self.assertFalse(any("new chart" in section.sql.lower() for section in sections))

    def test_template_items_select_only_their_mapped_sql_sections(self):
        script = Path(__file__).parents[1] / "scripts" / "dbcheck_noncdb.sql"
        sections = parse_script_sections(script)
        selected = enabled_oracle_item_keys(
            [
                {"key": "oracle.controlfile.count", "enabled": True},
                {"key": "oracle.redo.member_count", "enabled": False},
            ]
        )
        matching = [section.key for section in sections if section.item_key in selected]

        self.assertEqual(matching, ["control_file"])

    def test_daily_template_expands_to_real_oracle_items(self):
        selected = enabled_oracle_item_keys(
            [{"key": "space.capacity", "enabled": True}]
        )

        self.assertIn("oracle.tablespace.used_pct", selected)
        self.assertIn("oracle.fra.used_pct", selected)
        self.assertNotIn("oracle.security.account_status", selected)

    def test_oracle_collector_queries_are_bound_to_template_items(self):
        selected = enabled_oracle_item_keys(
            [{"key": "oracle.blocking.seconds", "enabled": True}]
        )
        query_names = {
            name
            for name, item_keys in ORACLE_QUERY_ITEM_KEYS.items()
            if item_keys & selected
        }

        self.assertEqual(query_names, {"locks"})

    def test_skipped_optional_sql_is_coverage_metadata_not_a_health_result(self):
        section = ScriptSqlSection(
            order=1,
            key="range_par_extend",
            title="Range Partition Extend Check",
            sql="SELECT * FROM sys.tabpart$ WHERE ROWNUM = 1",
            line_number=100,
            item_key="oracle.objects.segment_fragmentation",
        )

        class Cursor:
            def execute(self, _sql):
                raise AssertionError("skipped SQL must not execute")

            def close(self):
                pass

        class Connection:
            def cursor(self):
                return Cursor()

        findings = _execute_sections(
            Connection(),
            {"script_name": "dbcheck_noncdb.sql"},
            [section],
            filtered_by_template=True,
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].check_item, "模板 SQL 执行覆盖")
        self.assertEqual(findings[0].check_status, "info")
        self.assertEqual(findings[0].raw_data["skipped_count"], 1)
        self.assertEqual(findings[0].raw_data["evidence_kind"], "execution_summary")
        self.assertEqual(findings[0].raw_data["row_count"], 1)
        self.assertEqual(findings[0].raw_data["rows"][0]["status"], "skipped")

    def test_sys_internal_base_tables_are_optional(self):
        reason = _skip_reason("SELECT * FROM sys.tabpart$ WHERE ROWNUM = 1")

        self.assertIn("SYS internal dictionary", reason)

    def test_resolved_snapshot_values_are_numeric_literals(self):
        sql = """
            WITH snapshots AS (
              SELECT b.snap_id FROM dba_hist_snapshot b, dba_hist_snapshot e
              WHERE b.dbid(+) = e.dbid
                AND b.snap_id = &_snap_beg
                AND e.snap_id = &_snap_end
            )
            SELECT * FROM snapshots;
        """

        prepared = _prepare_sql(sql, {"&_snap_beg": "101", "&_snap_end": "102"})

        self.assertIn("b.snap_id = 101", prepared)
        self.assertIn("e.snap_id = 102", prepared)
        self.assertNotIn("&_snap", prepared)
        self.assertNotIn("SELECT MIN(snap_id)", prepared)

    def test_scheduler_timezone_values_are_rendered_as_text(self):
        sql = """
            SELECT w.window_name, w.next_start_date, w.last_start_date
            FROM dba_autotask_window_clients c, dba_scheduler_windows w
            WHERE c.window_name = w.window_name;
        """

        prepared = _prepare_sql(sql, {})

        self.assertIn("TO_CHAR(w.next_start_date", prepared)
        self.assertIn("TO_CHAR(w.last_start_date", prepared)


if __name__ == "__main__":
    unittest.main()
