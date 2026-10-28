import hashlib
import json
import unittest
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

from app.collectors.oracle.oracle_inspection_profile import profile_from_values
from app.services.inspection import oracle_script_checks as checks


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
SOURCE_HASHES = {
    "dbcheck10g.sql": "aa29d875627eab2cc9ff811ec97a806a75132cad11a169c07fcc7e7a51abdf08",
    "dbcheck_noncdb.sql": "838dab5c8470de57abe59086f28978fa20f90c6f266d5ec56ccb2d8a094e7174",
    "dbcheck_cdb.sql": "17f35c36c0ac7c5115ca399288138c965a2902786e17c24244ab198093c8ce54",
    "mysql_check.sh": "4a14485268028cd5fdb788eca1ddccaf40708dbfa1862b08dacbf9bb06849cc7",
    "mysql_check.ps1": "feceacd9bbc771a22694bd0d3636ec1236804226327f96a5b2f6a1fa934e6b61",
    "pg_check.sh": "9402cc03052a41dc14a6c265a3250cc9395140466d9adcde0172f2d93a9503d9",
    "pg_check.ps1": "ea1a53381f486addc79e204ceb5703f4535f6e97dbe3e04a4ee3b2be6c0cbaf3",
    "mssql_check.sh": "bcae5684a62e2d625b80832a560d4590d6b63d7f13c52c71ecff106969dd647a",
    "oscheck.sh": "87d1bc33ff37f5634cffadd10b5ac81fa6085d730c95b36b9982fecf1376e32a",
    "oscheck.ps1": "34b323be1355887ddcf6f14f4fb76b89bc12cb99e61279f75a691fc8845af93b",
}
PROFILE_SOURCES = {
    "10g": ("dbcheck10g.sql", 53, 19, "10.2.0.5.0"),
    "noncdb": ("dbcheck_noncdb.sql", 56, 20, "11.2.0.4.0"),
    "cdb": ("dbcheck_cdb.sql", 56, 20, "19.0.0.0.0"),
}


def _connection():
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = (1,)
    cursor.description = [("STATUS",)]
    cursor.fetchmany.return_value = [("VALID",)]
    return connection


def _dg_section():
    return next(
        section
        for section in checks.parse_script_sections(SCRIPT_DIR / "dbcheck_noncdb.sql")
        if section.key == "dg_stats"
    )


class InspectionScriptSourcesTest(unittest.TestCase):
    def test_manifest_matches_exact_source_bytes_and_pinned_hashes(self):
        manifest = json.loads((SCRIPT_DIR / "source-manifest.json").read_text())
        self.assertEqual(manifest["format"], "tracemind-inspection-script-sources")
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["logical_origin"], "user-inspection-scripts")
        entries = manifest["files"]
        self.assertEqual(len(entries), len(SOURCE_HASHES))
        self.assertEqual({entry["filename"] for entry in entries}, set(SOURCE_HASHES))
        for entry in entries:
            name = entry["filename"]
            with self.subTest(filename=name):
                self.assertEqual(PurePosixPath(name).parts, (name,))
                self.assertEqual(entry["source"], f"user-inspection-scripts/{name}")
                self.assertFalse(PurePosixPath(entry["source"]).is_absolute())
                content = (SCRIPT_DIR / name).read_bytes()
                self.assertEqual(entry["bytes"], len(content))
                self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
                self.assertEqual(entry["sha256"], SOURCE_HASHES[name])
                role = "oracle_sql_source" if name.endswith(".sql") else "reference_only"
                self.assertEqual(entry["role"], role)
        self.assertFalse((SCRIPT_DIR / "MySQLShellInstall").exists())

    def test_actual_profile_parser_counts_and_complete_mapping(self):
        for profile, (filename, count, item_count, _) in PROFILE_SOURCES.items():
            with self.subTest(profile=profile):
                sections = checks.parse_script_sections(SCRIPT_DIR / filename)
                self.assertEqual(len(sections), count)
                self.assertEqual(len({section.key for section in sections}), count)
                self.assertEqual(len({section.item_key for section in sections}), item_count)
                self.assertTrue(all(section.item_key for section in sections))
                self.assertEqual([section.order for section in sections], list(range(1, count + 1)))

    def test_profile_specific_sections_are_preserved(self):
        legacy_only = {"foreigno_index", "hwmstat_info"}
        modern_only = {"schema_info_2", "autotask_jobhis", "alertlog_check", "asm_instance", "asmdisk_attr"}
        for profile, (filename, _, _, _) in PROFILE_SOURCES.items():
            with self.subTest(profile=profile):
                keys = {section.key for section in checks.parse_script_sections(SCRIPT_DIR / filename)}
                present, absent = (legacy_only, modern_only) if profile == "10g" else (modern_only, legacy_only)
                self.assertTrue(present <= keys)
                self.assertFalse(absent & keys)

    def test_actual_sources_still_exclude_plsql_charts_and_sqlplus_commands(self):
        for filename, _, _, _ in PROFILE_SOURCES.values():
            with self.subTest(filename=filename):
                for section in checks.parse_script_sections(SCRIPT_DIR / filename):
                    lowered = section.sql.lower()
                    self.assertTrue(checks.SQL_START_RE.match(section.sql))
                    self.assertFalse(checks._is_presentation_sql(section.sql))
                    self.assertNotIn("instinfo_cur.", lowered)
                    self.assertNotIn("sperrorlog", lowered)
                    self.assertNotRegex(
                        lowered,
                        r"(?m)^\s*(?:host[ \t]+\S|(?:spool|commit|delete|alter|declare|begin)\b)",
                    )

    def test_all_profile_sections_have_no_unresolved_variables_with_known_version(self):
        for filename, _, _, version in PROFILE_SOURCES.values():
            with self.subTest(filename=filename):
                substitutions = checks._resolve_sqlplus_substitutions(_connection(), version=version)
                for section in checks.parse_script_sections(SCRIPT_DIR / filename):
                    sql = checks._prepare_sql(section.sql, substitutions)
                    self.assertIsNone(checks.SQLPLUS_VAR_RE.search(sql), section.key)
                    self.assertIsNone(checks._skip_reason(sql), section.key)

    def test_dg_uses_only_version_allowlisted_identifiers_and_quoted_contents(self):
        for version, column, mode, ok in (
            ("11.1.0.7.0", "synchronization_status", "SYNC", "OK"),
            ("11.2.0.4.0", "gap_status", "GAP", "NO GAP"),
            ("12.1.0.2.0", "gap_status", "GAP", "NO GAP"),
            ("19.0.0.0.0", "gap_status", "GAP", "NO GAP"),
        ):
            with self.subTest(version=version):
                sql = checks._prepare_sql(_dg_section().sql, checks._dg_sqlplus_substitutions(version))
                self.assertIn(f"s.{column}", sql)
                self.assertIn(f"'{mode}' = 'SYNC'", sql)
                self.assertIn(f"<> '{ok}'", sql)
                self.assertNotIn(f"s.'{column}'", sql)
                self.assertIsNone(checks._skip_reason(sql))

    def test_dg_unknown_unsupported_and_untrusted_versions_remain_skipped(self):
        for version in (None, "", "unknown", "11", "11.0.0.0", "10.2.0.5.0", "11.1'; DELETE FROM t; --"):
            with self.subTest(version=version):
                substitutions = checks._dg_sqlplus_substitutions(version)
                self.assertEqual(substitutions, {})
                sql = checks._prepare_sql(_dg_section().sql, substitutions)
                self.assertIn("SQL*Plus substitution variables", checks._skip_reason(sql))

    def test_execution_resolves_dg_from_detected_profile_without_database_fragments(self):
        for version, column in (("11.1.0.7.0", "synchronization_status"), ("11.2.0.4.0", "gap_status")):
            with self.subTest(version=version):
                connection = _connection()
                findings = checks._execute_sections(
                    connection,
                    {"version": version, "script_name": "dbcheck_noncdb.sql"},
                    [_dg_section()],
                )
                self.assertEqual(findings[0].raw_data["executed_count"], 1)
                self.assertEqual(findings[0].raw_data["skipped_count"], 0)
                sql = connection.cursor.return_value.execute.call_args.args[0]
                self.assertIn(f"s.{column}", sql)
                self.assertIsNone(checks.SQLPLUS_VAR_RE.search(sql))

    def test_execution_without_version_skips_dg_instead_of_guessing(self):
        connection = _connection()
        findings = checks._execute_sections(connection, {}, [_dg_section()])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].raw_data["executed_count"], 0)
        self.assertEqual(findings[0].raw_data["skipped_count"], 1)
        for call in connection.cursor.return_value.execute.call_args_list:
            self.assertNotIn("gv$archive_dest_status", call.args[0])

    def test_profile_mismatch_still_stops_before_script_parsing_or_execution(self):
        profile = profile_from_values("19.0.0.0.0", "YES")
        with (
            patch.object(checks, "oracle_connection", return_value=nullcontext(object())),
            patch.object(checks, "detect_oracle_profile", return_value=profile),
            patch.object(checks, "parse_script_sections") as parse,
            patch.object(checks, "_execute_sections") as execute,
        ):
            findings = checks.collect_oracle_script_findings(object(), expected_profile="noncdb")
        parse.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].check_status, "warning")
        self.assertEqual(findings[0].metric_value, "degraded")

    def test_existing_extraction_skip_guards_are_not_relaxed(self):
        for sql in (
            "SELECT * FROM sys.tabpart$",
            "SELECT &_unknown FROM dual",
            "SELECT instinfo_cur.inst_id FROM dual",
            "DELETE FROM example_table",
            "BEGIN NULL; END;",
        ):
            with self.subTest(sql=sql):
                self.assertIsNotNone(checks._skip_reason(sql))


if __name__ == "__main__":
    unittest.main()
