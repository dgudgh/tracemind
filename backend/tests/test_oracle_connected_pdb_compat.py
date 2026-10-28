from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import socket

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.services.inspection import oracle_cdb_compat as compat
from app.services.inspection import oracle_script_checks as checks
from app.services.inspection import report_builder, result_writer


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dbcheck_cdb.sql"
MODERN = {key: "VARCHAR2" for key in (
    "PATCH_ID", "DESCRIPTION", "ACTION", "STATUS", "ACTION_TIME", "SOURCE_VERSION", "TARGET_VERSION",
)}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("isolated compatibility tests must not connect")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def sections():
    return {s.key: s for s in checks.parse_script_sections(SCRIPT) if s.key in compat.SECTION_HASHES}


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self.rows = []

    def execute(self, sql, binds=None):
        assert sql.lstrip().upper().startswith("SELECT")
        self.conn.calls.append(sql)
        if self.conn.failure and self.conn.failure in sql:
            raise RuntimeError("synthetic permission failure")
        if sql == compat.IDENTITY_SQL:
            columns, rows = ["CON_ID", "CON_NAME"], [(self.conn.con_id, self.conn.name)]
        elif sql == compat.COLUMNS_SQL:
            dictionary = self.conn.patch_columns if binds["view_name"] == "DBA_REGISTRY_SQLPATCH" else {"HIGH_VALUE": "LONG"}
            columns, rows = ["COLUMN_NAME", "DATA_TYPE"], list(dictionary.items())
        elif "FROM dba_registry_sqlpatch p ORDER BY" in sql:
            columns = ["PATCH_ID", "DESCRIPTION", "VERSION", "ACTION_STATUS", "ACTION_TIME", "RECORD_SOURCE"]
            rows = [("1", "RU", "19.26", "APPLY / SUCCESS", "2026-01-01 01:02:03.123456", "SQLPATCH")]
        elif "FROM dba_registry_history h" in sql:
            columns, rows = ["PATCH_ID", "RECORD_SOURCE"], [("HISTORY", "REGISTRY_HISTORY")]
        elif sql == compat.RANGE_USERS_SQL:
            columns, rows = ["USERNAME"], [("APP",)]
        elif sql == compat.RANGE_METADATA_SQL:
            columns = ["TABLE_OWNER", "TABLE_NAME", "PARTITION_NAME", "PARTITION_POSITION", "INTERVAL"]
            rows = [("APP", "ORDERS", name, pos, "NO") for pos, name in enumerate(("P_2025", "P_2026", "P_MAX"), 1)]
        elif sql == compat.RANGE_KEYS_SQL:
            columns, rows = ["OWNER", "NAME", "COLUMN_NAME"], [("APP", "ORDERS", "CREATED_AT")]
        elif sql == compat.RANGE_TYPES_SQL:
            columns, rows = ["COLUMN_NAME", "DATA_TYPE"], [("CREATED_AT", "DATE")]
        elif sql == compat.RANGE_RECYCLE_SQL:
            columns, rows = ["FOUND"], []
        elif sql == compat.RANGE_BOUNDARY_SQL:
            columns = ["USERNAME", "TABLENAME", "PARTNAME", "PARTITION_POSITION", "INTERVAL", "MAX_RANGE"]
            rows = [("APP", "ORDERS", "P_2026", 2, "NO", "TO_DATE('2026-10-01','YYYY-MM-DD')")]
        elif sql == compat.WINDOWS_SQL:
            columns = ["WINDOW_NAME", "NEXT_START_DATE", "LAST_START_DATE"]
            rows = [("MONDAY_WINDOW", "2026-09-14 22:00:00.123456789 ASIA/SHANGHAI CST +08:00", None)]
        else:
            raise AssertionError(sql)
        if self.conn.empty and sql not in (compat.IDENTITY_SQL, compat.COLUMNS_SQL):
            rows = []
        if self.conn.overflow and sql == compat.RANGE_METADATA_SQL:
            rows = rows * (compat.MAX_PARTITION_METADATA_ROWS + 1)
        self.description = [(c,) for c in columns]
        self.rows = rows

    def fetchmany(self, count):
        return self.rows[:count]

    def close(self):
        pass


class Connection:
    def __init__(self, *, con_id=12, name="PDB17", failure=None, empty=False, overflow=False):
        self.con_id, self.name = con_id, name
        self.failure, self.empty, self.overflow = failure, empty, overflow
        self.patch_columns = dict(MODERN)
        self.call_timeout = 7000
        self.calls = []

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        pass


@pytest.mark.parametrize("key", list(compat.SECTION_HASHES))
def test_fingerprinted_source_executes_only_connected_pdb_selects(sections, key):
    conn = Connection()
    result = compat.execute_connected_pdb_section(sections[key], conn, max_rows=200, expected_container="PDB17")
    assert result["coverage_complete"]
    assert not result["cdb_coverage_complete"]
    assert result["scope"] == "connected_pdb_only"
    assert result["template_sha256"] == sha256(SCRIPT.read_bytes()).hexdigest()
    assert result["original_sql_sha256"] == compat.SECTION_HASHES[key]
    assert all(r["CON_ID"] == 12 and r["CONTAINER_NAME"] == "PDB17" for r in result["rows"])
    assert all(s.lstrip().upper().startswith("SELECT") for s in conn.calls)
    assert result["session_changes"] == []
    assert conn.call_timeout == 7000


def test_modern_patch_version_and_missing_bundle_do_not_lose_history(sections):
    conn = Connection()
    result = compat.execute_connected_pdb_section(sections["database_patch"], conn, max_rows=200)
    data_sql = [s["sql"] for s in result["actual_statements"] if s["kind"] == "data"]
    assert "p.target_version version" in data_sql[0]
    assert "p.source_version, p.target_version" in data_sql[0]
    assert "p.bundle_id" not in data_sql[1]
    assert "NOT EXISTS" not in data_sql[1]
    assert {r["RECORD_SOURCE"] for r in result["rows"]} == {"SQLPATCH", "REGISTRY_HISTORY"}


def test_legacy_patch_columns_preserve_supported_dedup(sections):
    conn = Connection()
    conn.patch_columns = {k: v for k, v in MODERN.items() if k not in {"SOURCE_VERSION", "TARGET_VERSION"}}
    conn.patch_columns.update(VERSION="VARCHAR2", BUNDLE_ID="NUMBER")
    result = compat.execute_connected_pdb_section(sections["database_patch"], conn, max_rows=200)
    data_sql = [s["sql"] for s in result["actual_statements"] if s["kind"] == "data"]
    assert "p.version version" in data_sql[0]
    assert "p.bundle_id = h.id" in data_sql[1]


def test_high_value_uses_local_long_without_losing_original_predicates(sections):
    result = compat.execute_connected_pdb_section(sections["range_par_extend"], Connection(), max_rows=200)
    assert "TO_DATE(" in result["rows"][0]["MAX_RANGE"]
    statements = result["actual_statements"]
    assert compat.RANGE_SQL not in [s["sql"] for s in statements]
    long_reads = [s for s in statements if s["kind"].endswith("_long")]
    assert len(long_reads) == 2
    assert all(s["binds"] == {"owner": "APP", "table_name": "ORDERS", "partition_name": "P_2026"} for s in long_reads)
    assert all("JOIN" not in s["sql"] and "cdb_tab_partitions" not in s["sql"] for s in long_reads)
    assert result["partition_scan"]["complete"]
    assert result["partition_scan"]["penultimate_candidates"] == 1


def test_autotask_preserves_named_zone_fraction_and_offset_in_server_text(sections):
    result = compat.execute_connected_pdb_section(sections["autotask_windows"], Connection(), max_rows=200)
    assert "123456789 ASIA/SHANGHAI CST +08:00" in result["rows"][0]["NEXT_START_DATE"]
    assert "TO_CHAR(w.next_start_date, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD TZH:TZM')" in compat.WINDOWS_SQL
    assert "TO_CHAR(w.last_start_date, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD TZH:TZM')" in compat.WINDOWS_SQL


@pytest.mark.parametrize("con_id,name", [(1, "CDB$ROOT"), (2, "PDB$SEED")])
def test_root_and_seed_never_masquerade_as_complete_cdb(sections, con_id, name):
    conn = Connection(con_id=con_id, name=name)
    with pytest.raises(compat.OracleCompatibilityError, match="CDB-wide") as caught:
        compat.execute_connected_pdb_section(sections["range_par_extend"], conn, max_rows=200)
    assert not caught.value.evidence["coverage_complete"]
    assert conn.calls == [compat.IDENTITY_SQL]
    assert conn.call_timeout == 7000


def test_profile_mismatch_and_modified_sql_fail_closed(sections):
    conn = Connection()
    with pytest.raises(compat.OracleCompatibilityError, match="differs"):
        compat.execute_connected_pdb_section(sections["database_patch"], conn, max_rows=200, expected_container="OTHER")
    conn.calls.clear()
    with pytest.raises(compat.OracleCompatibilityError, match="explicit review"):
        compat.execute_connected_pdb_section(replace(sections["database_patch"], sql="SELECT 1 FROM dual"), conn, max_rows=200)
    assert not conn.calls


@pytest.mark.parametrize("failure,overflow", [("dba_registry_history", False), (None, True)])
def test_partial_query_and_row_limit_fail_with_actual_evidence(sections, failure, overflow):
    key = "range_par_extend" if overflow else "database_patch"
    conn = Connection(failure=failure, overflow=overflow)
    with pytest.raises(compat.OracleCompatibilityError) as caught:
        compat.execute_connected_pdb_section(sections[key], conn, max_rows=200)
    evidence = caught.value.evidence
    assert not evidence["coverage_complete"]
    assert evidence["actual_statements"][-1]["status"] == "failed"
    assert evidence["truncated"] is overflow
    assert conn.call_timeout == 7000


def test_successful_empty_is_explicit_no_matching_rows(sections):
    result = compat.execute_connected_pdb_section(sections["range_par_extend"], Connection(empty=True), max_rows=200)
    assert result["coverage_complete"]
    assert result["rows"] == []
    assert result["container_coverage"][0]["status"] == "no_matching_rows"


def test_source_to_findings_to_sqlite_to_report_retains_actual_sql_and_scope(sections, tmp_path):
    conn = Connection()
    findings = checks._execute_sections(conn, {"script_name": "dbcheck_cdb.sql", "con_name": "PDB17"}, list(sections.values()))
    assert findings[0].raw_data["executed_count"] == 3
    assert findings[0].raw_data["failed_count"] == 0
    engine = create_engine(f"sqlite:///{tmp_path / 'oracle-compat.sqlite'}")
    DBInstance.__table__.create(engine)
    DBCheckResult.__table__.create(engine)
    with Session(engine) as db:
        instance = DBInstance(instance_name="oracle-test", db_type="oracle", host="unused.invalid", port=1521,
                              username="monitor", password_cipher="placeholder")
        db.add(instance)
        db.commit()
        rows = result_writer.save_findings(db, instance, "pdb-compat-test", findings)
        assert len(rows) == 4
        assert all(result_writer.finding_execution_status(row) == "success" for row in rows)
        report = report_builder.build_report_payload(db, "pdb-compat-test", instance.id)
        assert report["task"]["task_status"] == "success"
        for row in report["report"]["raw_appendix"]["rows"][1:]:
            raw = row["raw_data"]
            assert raw["scope"] == "connected_pdb_only"
            assert raw["compatibility"]["original_sql_sha256"] == compat.SECTION_HASHES[raw["key"]]
            assert raw["sql"] == ";\n\n".join(s["sql"] for s in raw["compatibility"]["actual_statements"])
            assert "PDB17" in row["finding"]
    engine.dispose()


def test_failed_adapter_remains_failed_finding_with_partial_rows(sections):
    findings = checks._execute_sections(Connection(failure="dba_registry_history"),
                                        {"script_name": "dbcheck_cdb.sql"}, [sections["database_patch"]])
    assert findings[0].raw_data["failed_count"] == 1
    assert findings[1].raw_data["compatibility"]["rows"][0]["RECORD_SOURCE"] == "SQLPATCH"
    assert result_writer.finding_execution_status(findings[1]) == "failed"
