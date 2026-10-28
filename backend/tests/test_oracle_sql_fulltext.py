from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.collectors.oracle.sql_text import MAX_SQL_TEXT_BYTES, materialize_sql_rows, read_sql_text, validate_stored_sql_text
from app.services import slow_sql_analysis


class Lob:
    def __init__(self, value, owner):
        self.value, self.owner = value, owner

    def size(self):
        assert not self.owner.closed
        return len(self.value.encode("utf-16-le")) // 2

    def read(self):
        assert not self.owner.closed
        return self.value


class Cursor:
    def __init__(self, rows, *, plan="Plan hash value: 42", current_con=12):
        self.rows, self.plan, self.current_con = rows, plan, current_con
        self.calls, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute(self, sql, binds=None):
        self.calls.append((sql, binds))
        if "DISPLAY_CURSOR" in sql:
            self.result = [{"plan_table_output": self.plan}]
        elif "FROM dual" in sql:
            self.result = [{"con_id": self.current_con}]
        else:
            self.result = self.rows
        self.description = [(key,) for key in self.result[0]] if self.result else []

    def fetchall(self):
        return [tuple(row.values()) for row in self.result]

    def fetchone(self):
        return self.fetchall()[0]


def connection(cursor):
    @contextmanager
    def connect(instance):
        yield SimpleNamespace(version="19.3", cursor=lambda: cursor)
    return connect


def test_list_materializes_full_clob_with_unicode_before_close():
    text = "SELECT '" + "中文🙂" * 1600 + "' FROM dual -- end"
    cursor = Cursor([])
    cursor.rows = [{"sql_id": "abc", "child_number": 3, "con_id": 12, "sql_text": Lob(text, cursor)}]
    with patch.object(slow_sql_analysis, "oracle_connection", connection(cursor)):
        result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"))
    assert result["execution_status"] == "success"
    assert result["rows"][0]["sql_text"] == text
    assert result["rows"][0]["sql_text_status"] == "complete"
    assert cursor.closed
    assert "sql_fulltext AS sql_text" in cursor.calls[-1][0]
    assert "SUBSTR(sql_text" not in cursor.calls[-1][0]


@pytest.mark.parametrize("current_con,plan,status", [(12, "Plan hash value: 42", "collected"),
    (1, "Plan hash value: 42", "not_collected"), (12, "Plan hash value: 99", "not_collected"),
    (12, "NOTE: cannot fetch plan", "not_collected"), (12, "Plan hash value: 420", "not_collected"),
    (12, "select 'Plan hash value: 42' from dual\nPlan hash value: 99", "not_collected"),
    (12, "Plan hash value: 42\nPlan hash value: 99", "not_collected")])
def test_detail_text_and_plan_bind_exact_identity(current_con, plan, status):
    cursor = Cursor([], plan=plan, current_con=current_con)
    cursor.rows = [{"sql_text": Lob("SELECT " + "x" * 2000, cursor), "child_number": 3, "con_id": 12, "plan_hash_value": 42}]
    with patch.object(slow_sql_analysis, "oracle_connection", connection(cursor)):
        result = slow_sql_analysis.get_sql_detail(SimpleNamespace(instance_name="offline"), "abc", child_number=3, con_id=12, plan_hash_value=42)
    assert cursor.closed and result["child_number"] == 3 and result["con_id"] == 12
    assert len(result["sql_fulltext"]) == 2007
    assert cursor.calls[0][1] == {"sql_id": "abc", "child_number": 3, "con_id": 12, "plan_hash_value": 42}
    assert all("dba_hist" not in sql.lower() for sql, _ in cursor.calls)
    plan_calls = [(sql, binds) for sql, binds in cursor.calls if "DISPLAY_CURSOR" in sql]
    if current_con == 12:
        assert plan_calls[0][1] == {"sql_id": "abc", "child_number": 3}
    else:
        assert not plan_calls
    assert result["plan_status"] == status
    if status != "collected":
        assert result["plan_text"] == "" and result["plan_error"]


def test_disappeared_cursor_never_falls_back_to_another_child_or_awr():
    cursor = Cursor([])
    with patch.object(slow_sql_analysis, "oracle_connection", connection(cursor)):
        result = slow_sql_analysis.get_sql_detail(SimpleNamespace(instance_name="offline"), "abc", child_number=9, con_id=12)
    assert result["status"] == "not_found" and not result["sql_fulltext"]
    assert len(cursor.calls) == 1 and cursor.closed


def test_text_size_failure_never_silently_truncates():
    cursor = Cursor([])
    oversized = SimpleNamespace(size=lambda: MAX_SQL_TEXT_BYTES + 1, read=lambda: pytest.fail("must not read oversized CLOB"))
    with pytest.raises(ValueError, match="not truncated"):
        read_sql_text(oversized)
    with pytest.raises(ValueError, match="65535"):
        validate_stored_sql_text("中" * 21846)
    assert len(validate_stored_sql_text("中" * 21845).encode("utf-8")) == 65535


def test_batch_size_and_deadline_are_bounded_and_restore_timeout():
    conn = SimpleNamespace(call_timeout=5000)
    with pytest.raises(ValueError, match="8 MiB"):
        materialize_sql_rows([{"sql_text": "x" * MAX_SQL_TEXT_BYTES} for _ in range(3)], connection=conn)
    assert conn.call_timeout == 5000
    with pytest.raises(TimeoutError, match="deadline"):
        materialize_sql_rows([{"sql_text": "text"}], connection=conn, deadline=0)
    assert conn.call_timeout == 5000
    with pytest.raises(TimeoutError, match="deadline"):
        materialize_sql_rows([], connection=conn, deadline=0)


def test_query_budget_applies_to_execute_and_fetch_even_for_empty_result(monkeypatch):
    from app.collectors.oracle import sql_text
    clock = [0]
    monkeypatch.setattr(slow_sql_analysis, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sql_text, "monotonic", lambda: clock[0])
    cursor = Cursor([])
    conn = SimpleNamespace(version="19.3", cursor=lambda: cursor, call_timeout=10000)
    original_execute, original_fetch = cursor.execute, cursor.fetchall
    observed = []
    def execute(sql, binds=None):
        observed.append(conn.call_timeout)
        clock[0] += 9
        original_execute(sql, binds)
    def fetch():
        observed.append(conn.call_timeout)
        clock[0] += 9
        return original_fetch()
    cursor.execute, cursor.fetchall = execute, fetch
    @contextmanager
    def connect(instance):
        yield conn
    monkeypatch.setattr(slow_sql_analysis, "oracle_connection", connect)
    result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"))
    assert observed == [10000, 10000, 10000, 3000]
    assert result["execution_status"] == "failed" and result["count"] == 0
    assert "deadline" in result["message"]


def test_detail_route_propagates_identity_and_source_failure():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1 import oracle
    from app.core.database import get_db

    app = FastAPI()
    app.get("/oracle/{instance_id}/sql-detail")(oracle.oracle_sql_detail)
    app.dependency_overrides[get_db] = lambda: None
    instance = SimpleNamespace(id=1, instance_name="offline")
    with patch.object(oracle, "_get_oracle_instance", return_value=instance), patch.object(slow_sql_analysis, "get_sql_detail") as detail:
        detail.return_value = {"status": "success"}
        response = TestClient(app).get("/oracle/1/sql-detail?sql_id=abc&child_number=2&con_id=12&plan_hash_value=42")
        assert response.status_code == 200
        detail.assert_called_once_with(instance, "abc", child_number=2, con_id=12, plan_hash_value=42)
        for state, status in (("not_found", 404), ("degraded", 502)):
            detail.return_value = {"status": state, "message": "unavailable"}
            assert TestClient(app).get("/oracle/1/sql-detail?sql_id=abc").status_code == status
        detail.reset_mock()
        assert TestClient(app).get("/oracle/1/sql-detail?sql_id=abc&child_number=-1").status_code == 422
        detail.assert_not_called()
