"""Relational equivalence and failure tests; SQLite fixtures are not live acceptance."""

from collections import Counter
from pathlib import Path
import sqlite3

import pytest

from app.services.inspection import oracle_cdb_compat as compat
from app.services.inspection.oracle_script_checks import parse_script_sections


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def execute(self, sql, binds):
        assert sql.lstrip().upper().startswith("SELECT")
        self.sql = sql
        self.conn.calls.append((sql, dict(binds)))
        self.conn.counts[sql] += 1
        if self.conn.before:
            self.conn.before(sql, binds, self.conn.counts[sql])
        if sql == compat.IDENTITY_SQL:
            columns, self.rows = ["CON_ID", "CON_NAME"], [(12, "PDB17")]
        elif sql == compat.COLUMNS_SQL:
            columns, self.rows = ["COLUMN_NAME", "DATA_TYPE"], [("HIGH_VALUE", "LONG")]
        else:
            # Only Oracle's existence-row syntax changes; predicates and bindings are real SQL.
            sqlite_sql = sql.replace(" AND ROWNUM = 1", " LIMIT 1")
            with_cursor = self.conn.db.execute(sqlite_sql, binds)
            columns = [d[0].upper() for d in with_cursor.description]
            self.rows = with_cursor.fetchall()
            with_cursor.close()
        self.description = [(name,) for name in columns]

    def fetchmany(self, count):
        rows = self.rows
        if self.conn.transform:
            rows = self.conn.transform(self.sql, self.conn.counts[self.sql], rows)
        return rows[:count]

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE dba_users (username TEXT, account_status TEXT);
            CREATE TABLE dba_tab_partitions (table_owner TEXT, table_name TEXT,
                partition_name TEXT, partition_position INTEGER, interval TEXT, high_value TEXT);
            CREATE TABLE dba_part_key_columns (owner TEXT, name TEXT, column_name TEXT);
            CREATE TABLE dba_tab_columns (owner TEXT, table_name TEXT, column_name TEXT, data_type TEXT);
            CREATE TABLE dba_recyclebin (owner TEXT, object_name TEXT);
        """)
        self.db.execute("INSERT INTO dba_users VALUES ('APP', 'OPEN')")
        self.call_timeout = 4321
        self.calls, self.cursors = [], []
        self.counts = Counter()
        self.before = self.transform = None

    def cursor(self):
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def table(self, name, *, owner="APP", positions=(1, 2, 3), intervals=None,
              keys=("D",), types=(("D", "DATE"),), recycled=False, boundary="MAXVALUE"):
        for index, pos in enumerate(positions):
            self.db.execute("INSERT INTO dba_tab_partitions VALUES (?, ?, ?, ?, ?, ?)",
                            (owner, name, f"P_{pos}", pos, intervals[index] if intervals else "NO", boundary))
        self.db.executemany("INSERT INTO dba_part_key_columns VALUES (?, ?, ?)",
                            [(owner, name, key) for key in keys])
        self.db.executemany("INSERT INTO dba_tab_columns VALUES (?, ?, ?, ?)",
                            [(owner, name, col, kind) for col, kind in types])
        if recycled:
            self.db.executemany("INSERT INTO dba_recyclebin VALUES (?, ?)", [(owner, name)] * 3)


@pytest.fixture
def conn():
    connection = Connection()
    yield connection
    assert connection.call_timeout == 4321
    assert all(cursor.closed for cursor in connection.cursors)
    connection.db.close()


@pytest.fixture
def section():
    script = Path(__file__).resolve().parents[1] / "scripts" / "dbcheck_cdb.sql"
    return next(s for s in parse_script_sections(script) if s.key == "range_par_extend")


def collect(section, conn, max_rows=200):
    return compat.execute_connected_pdb_section(section, conn, max_rows=max_rows, expected_container="PDB17")


def test_matches_original_relational_query_without_weakening_predicates(section, conn):
    conn.db.executemany("INSERT INTO dba_users VALUES (?, ?)",
                       [("CLOSED", "LOCKED"), ("SYS", "OPEN"), ("other-owner", "OPEN")])
    conn.table("DATE_KEY", boundary="TO_DATE('2026-10-01','YYYY-MM-DD')")
    conn.table("TIMESTAMP_KEY", types=(("D", "TIMESTAMP(9) WITH TIME ZONE"),))
    conn.table("COMPOSITE", keys=("N", "D"), types=(("N", "NUMBER"), ("D", "DATE")))
    conn.table("INTERVAL_LAST", intervals=("NO", "NO", "YES"))
    conn.table("INTERVAL_SELECTED", intervals=("NO", "YES", "YES"))
    conn.table("GAP", positions=(1, 2, 4))
    conn.table("SINGLE", positions=(1,))
    conn.table("NON_KEY_DATE", keys=("N",), types=(("N", "NUMBER"), ("D", "DATE")))
    conn.table("RECYCLED", recycled=True)
    conn.table("CLOSED_TABLE", owner="CLOSED")
    conn.table("SYSTEM_TABLE", owner="SYS")
    conn.table("DATE_KEY", owner="other-owner", keys=("N",), types=(("N", "NUMBER"),))
    conn.table("quoted' table", owner="other-owner")
    expected = conn.db.execute(compat.RANGE_SQL).fetchall()
    result = collect(section, conn)
    actual = [(r["USERNAME"], r["TABLENAME"], r["PARTNAME"], r["MAX_RANGE"]) for r in result["rows"]]
    assert actual == expected
    assert {r[1] for r in actual} == {"DATE_KEY", "TIMESTAMP_KEY", "COMPOSITE", "INTERVAL_LAST", "quoted' table"}
    assert all(r[2] == "P_2" for r in actual)
    assert result["partition_scan"]["long_reads"] == 2 * len(expected)
    assert compat.RANGE_SQL not in [sql for sql, _ in conn.calls]
    assert all("HIGH_VALUE" not in sql.upper() or sql == compat.RANGE_BOUNDARY_SQL for sql, _ in conn.calls)
    bound = [(sql, binds) for sql, binds in conn.calls if binds.get("table_name") == "quoted' table"]
    assert bound and all("quoted' table" not in sql for sql, _ in bound)
    assert result["coverage_complete"] and not result["cdb_coverage_complete"]


@pytest.mark.parametrize("sql", [compat.RANGE_USERS_SQL, compat.RANGE_METADATA_SQL, compat.RANGE_KEYS_SQL,
                                 compat.RANGE_TYPES_SQL, compat.RANGE_RECYCLE_SQL, compat.RANGE_BOUNDARY_SQL])
@pytest.mark.parametrize("occurrence", [1, 2])
def test_any_failed_phase_cannot_publish_success(section, conn, sql, occurrence):
    conn.table("T")

    def fail(actual, binds, seen):
        if actual == sql and seen == occurrence:
            raise RuntimeError("synthetic dictionary permission or timeout failure")

    conn.before = fail
    with pytest.raises(compat.OracleCompatibilityError, match="synthetic") as caught:
        collect(section, conn)
    evidence = caught.value.evidence
    assert not evidence["coverage_complete"]
    assert not evidence["partition_scan"]["complete"]
    assert evidence["rows"] == []
    assert evidence["actual_statements"][-1]["status"] == "failed"


@pytest.mark.parametrize("sql", [compat.RANGE_USERS_SQL, compat.RANGE_METADATA_SQL, compat.RANGE_KEYS_SQL,
                                 compat.RANGE_TYPES_SQL, compat.RANGE_RECYCLE_SQL, compat.RANGE_BOUNDARY_SQL])
def test_dictionary_or_long_change_between_reads_fails(section, conn, sql):
    conn.table("T")

    def change(actual, seen, rows):
        if actual != sql or seen != 2:
            return rows
        if sql == compat.RANGE_RECYCLE_SQL:
            return [(1,)]
        if sql == compat.RANGE_BOUNDARY_SQL:
            return [(*rows[0][:-1], "TO_DATE('2030-01-01','YYYY-MM-DD')")]
        return []

    conn.transform = change
    with pytest.raises(compat.OracleCompatibilityError, match="changed") as caught:
        collect(section, conn)
    assert not caught.value.evidence["coverage_complete"]
    assert not caught.value.evidence["partition_scan"]["complete"]


@pytest.mark.parametrize("kind", ["metadata", "keys", "candidates", "results"])
def test_bounds_fail_without_reading_truncated_long_subset(section, conn, monkeypatch, kind):
    conn.table("A")
    conn.table("B")
    if kind == "metadata":
        monkeypatch.setattr(compat, "MAX_PARTITION_METADATA_ROWS", 5)
    elif kind == "keys":
        monkeypatch.setattr(compat, "MAX_PARTITION_METADATA_ROWS", 6)
        conn.db.executemany("INSERT INTO dba_part_key_columns VALUES ('APP', 'A', ?)", [(str(i),) for i in range(10)])
    elif kind == "candidates":
        monkeypatch.setattr(compat, "MAX_PARTITION_CANDIDATES", 1)
    with pytest.raises(compat.OracleCompatibilityError, match="limit") as caught:
        collect(section, conn, max_rows=1 if kind == "results" else 200)
    assert not caught.value.evidence["coverage_complete"]
    assert compat.RANGE_BOUNDARY_SQL not in [sql for sql, _ in conn.calls]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "wrong_identity", "null", "empty", "not_text"])
def test_invalid_long_never_becomes_empty_success(section, conn, kind):
    conn.table("T")

    def change(sql, seen, rows):
        if sql != compat.RANGE_BOUNDARY_SQL:
            return rows
        if kind == "missing":
            return []
        if kind == "duplicate":
            return rows * 2
        if kind == "wrong_identity":
            return [("OTHER", *rows[0][1:])]
        return [(*rows[0][:-1], {"null": None, "empty": "  ", "not_text": 4}[kind])]

    conn.transform = change
    with pytest.raises(compat.OracleCompatibilityError) as caught:
        collect(section, conn)
    assert not caught.value.evidence["coverage_complete"]
    assert caught.value.evidence["rows"] == []


@pytest.mark.parametrize("position", [0, -1, "invalid"])
def test_invalid_partition_position_fails(section, conn, position):
    conn.table("T", positions=(position,))
    with pytest.raises(compat.OracleCompatibilityError, match="Invalid partition position"):
        collect(section, conn)


def test_duplicate_partition_position_fails(section, conn):
    conn.table("T", positions=(1, 1, 2))
    with pytest.raises(compat.OracleCompatibilityError, match="Duplicate partition position"):
        collect(section, conn)


@pytest.mark.parametrize("phase", ["execute", "fetch"])
def test_total_deadline_covers_execute_and_fetch_without_extending_timeout(section, conn, monkeypatch, phase):
    now = [100.0]
    monkeypatch.setattr(compat, "monotonic", lambda: now[0])
    conn.table("T")

    def before(sql, binds, seen):
        assert 0 < conn.call_timeout <= 10000
        if sql == compat.RANGE_BOUNDARY_SQL and phase == "execute":
            now[0] += compat.SECTION_TIMEOUT_SECONDS + 1

    def fetch(sql, seen, rows):
        assert 0 < conn.call_timeout <= 10000
        if sql == compat.RANGE_BOUNDARY_SQL and phase == "fetch":
            now[0] += compat.SECTION_TIMEOUT_SECONDS + 1
        return rows

    conn.before, conn.transform = before, fetch
    with pytest.raises(compat.OracleCompatibilityError, match="deadline") as caught:
        collect(section, conn)
    assert not caught.value.evidence["coverage_complete"]
    assert caught.value.evidence["actual_statements"][-1]["status"] == "failed"


def test_successful_empty_still_rechecks_catalog_and_eligibility(section, conn):
    conn.table("RECYCLED", recycled=True)
    result = collect(section, conn)
    assert result["coverage_complete"] and result["rows"] == []
    assert result["container_coverage"][0]["status"] == "no_matching_rows"
    assert conn.counts[compat.RANGE_METADATA_SQL] == 2
    assert conn.counts[compat.RANGE_RECYCLE_SQL] == 2
    assert conn.counts[compat.RANGE_BOUNDARY_SQL] == 0
