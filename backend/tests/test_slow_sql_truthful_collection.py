from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import slow_sql as api
from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.models.slow_sql import DBSlowSQL
from app.models.system_setting import DBSystemSetting
from app.services import slow_sql, slow_sql_analysis


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Slow SQL isolated tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (DBInstance, DBSlowSQL, DBSystemSetting):
        model.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(DBInstance(id=1, instance_name="offline", db_type="oracle", host="192.0.2.1", port=1521,
                               username="offline", password_cipher="unused", enabled=1))
        session.commit()
        yield session
    engine.dispose()


def row(sql_id="abc", **values):
    return {"sql_id": sql_id, "sql_text": "select 1 from dual", "schema_name": "TEST", "executions": 2,
            "elapsed_time_us": 6000000, "buffer_gets": 30, "disk_reads": 2, "rows_processed": 4,
            "child_number": 0, "con_id": 12, **values}


def snapshot(rows):
    return {"status": "success", "execution_status": "success", "rows": rows, "count": len(rows),
            "metadata": {"source": "oracle.v$sql", "container_name": "PDB17", "statistics_scope": "cursor_lifetime_cumulative"}}


def old_history(db):
    record = DBSlowSQL(instance_id=1, db_type="oracle", sql_id="old", sql_text="old evidence",
                       capture_time=datetime.now() - timedelta(days=30), ai_summary="existing evidence")
    db.add(record)
    db.commit()
    return record.id


def client(db):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_collection_persists_real_rows_contract_and_keeps_old_history(db):
    old_id = old_history(db)
    rows = [row(), row(child_number=1, elapsed_time_us=12000000), row("fast", elapsed_time_us=5999999)]
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot(rows)) as query:
        result = api.collect_slow_sql(1, hours=1, db=db)
    query.assert_called_once_with(db.get(DBInstance, 1), "top_avg", hours=1)
    assert (result["read_count"], result["matched_count"], result["saved_count"]) == (3, 2, 2)
    assert result["status"] == "success" and result["analysis_status"] == "not_run"
    assert result["deleted_count"] == 0 and result["history_preserved"]
    assert db.get(DBSlowSQL, old_id).sql_text == "old evidence"
    records = db.query(DBSlowSQL).filter(DBSlowSQL.id.in_(result["record_ids"])).order_by(DBSlowSQL.id).all()
    assert [item.avg_elapsed_ms for item in records] == [Decimal("3000"), Decimal("6000")]
    assert all(item.ai_summary is None and item.optimize_suggestion is None for item in records)
    assert [item["child_number"] for item in result["records"]] == [0, 1]
    assert result["records"][0]["con_id"] == 12
    from app.api.v1.report import _collect_unified_reports
    reports = _collect_unified_reports(db, report_type="slow_sql", limit=100)
    assert {item["source_id"] for item in reports} == {old_id, *result["record_ids"]}
    assert all(item["status"] == "captured" for item in reports)


@pytest.mark.parametrize("value", [None, {}, {"execution_status": "success"},
                                    {"execution_status": "failed", "rows": []},
                                    snapshot([{"error": "failed query"}]), snapshot([row(executions="bad")]),
                                    snapshot([row(elapsed_time_us=float("nan"))]), snapshot([row(buffer_gets=2**53)])])
def test_invalid_or_failed_source_never_reports_success_or_deletes_history(db, value):
    old_id = old_history(db)
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=value):
        response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 502
    assert db.query(DBSlowSQL).count() == 1
    assert db.get(DBSlowSQL, old_id).ai_summary == "existing evidence"


@pytest.mark.parametrize("rows", [[], [row(elapsed_time_us=10000)]])
def test_valid_zero_hits_are_explained_without_fabricating_records(db, rows):
    old_history(db)
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot(rows)):
        result = slow_sql.collect(db, db.get(DBInstance, 1))
    assert result["status"] == "no_data" and result["execution_status"] == "success"
    assert result["read_count"] == len(rows)
    assert result["matched_count"] == result["saved_count"] == 0
    assert result["threshold_ms"] == 3000
    assert not result["persisted"] and result["record_ids"] == []
    assert db.query(DBSlowSQL).count() == 1


def test_commit_failure_rolls_back_whole_batch_without_deleting_history(db):
    old_id = old_history(db)
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot([row(), row("second")])), \
            patch.object(db, "commit", side_effect=RuntimeError("synthetic commit failure")):
        response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 500
    assert [item.id for item in db.query(DBSlowSQL).all()] == [old_id]


def test_full_text_survives_storage_list_detail_and_report(db):
    text = "SELECT '" + "中文" * 3000 + "' FROM dual -- end"
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot([row(sql_text=text)])):
        result = slow_sql.collect(db, db.get(DBInstance, 1))
    record = db.get(DBSlowSQL, result["record_ids"][0])
    assert record.sql_text == text and record.create_time == record.capture_time
    assert slow_sql.get_list(db)[0][0].sql_text == text
    from app.api.v1.report import _collect_unified_reports
    from app.services.report_center import _source_markdown, _source_html
    source = _collect_unified_reports(db, report_type="slow_sql")[0]
    assert text in _source_markdown(source)
    import html
    assert html.escape(text) in _source_html(source)


def test_text_capacity_failure_preserves_history_and_entire_batch(db):
    old_id = old_history(db)
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot([row(), row("too-long", sql_text="中" * 21846)])):
        response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 502
    assert "65535" in response.json()["detail"]
    assert [item.id for item in db.query(DBSlowSQL).all()] == [old_id]


def test_ai_analysis_requires_explicit_model_and_context_approval(db):
    old_id = old_history(db)
    with patch.object(db, "commit", side_effect=AssertionError("guidance must not write")):
        response = client(db).post(f"/slow-sql/{old_id}/analyze")
    assert response.status_code == 422
    assert db.get(DBSlowSQL, old_id).ai_summary == "existing evidence"


@pytest.mark.parametrize("hours", [0, -1, 169, "bad"])
def test_collection_rejects_invalid_time_filter_before_reading_source(db, hours):
    with patch.object(slow_sql_analysis, "get_slow_sql_list", side_effect=AssertionError("must not query")):
        assert client(db).post(f"/slow-sql/collect/1?hours={hours}").status_code == 422


class Cursor:
    def __init__(self, rows, calls):
        self.rows, self.calls, self.description = rows, calls, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, binds=None):
        assert sql.startswith("SELECT ")
        self.calls.append((sql, binds))
        self.result = ([{"container_name": "PDB17", "container_id": "12", "instance_name": "mesdb",
                         "database_time": "2026-09-10 10:12:10"}] if "FROM dual" in sql else self.rows)
        self.description = [(key,) for key in self.result[0]] if self.result else []

    def fetchall(self):
        return [tuple(item.values()) for item in self.result]


@pytest.mark.parametrize("category", list(slow_sql_analysis.QUERIES))
def test_realtime_query_has_bound_window_identity_and_truthful_metadata(category):
    calls = []
    conn = SimpleNamespace(version="19.0.0.0.0", call_timeout=None, cursor=lambda: Cursor([row()], calls))
    @contextmanager
    def connect(instance):
        yield conn
    with patch.object(slow_sql_analysis, "oracle_connection", connect):
        result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"), category, 3)
    assert conn.call_timeout == 10000
    assert result["status"] == result["execution_status"] == "success"
    metadata = result["metadata"]
    assert metadata["analysis_status"] == "not_run" and metadata["read_only"] and not metadata["persisted"]
    assert metadata["container_name"] == "PDB17" and not metadata["window_aggregation"]
    assert result["rows"][0]["instance_id"] == 1
    sql, binds = calls[-1]
    if category in {"active", "waiting"}:
        assert binds == {} and not metadata["hours_applied"]
        assert "s.sql_child_number = q.child_number" in sql and "s.con_id = q.con_id" in sql
    else:
        assert binds == {"hours": 3} and "SYSDATE - :hours / 24" in sql
        assert "child_number" in sql and "con_id" in sql and "elapsed_time_us" in sql
        assert metadata["statistics_scope"] == "cursor_lifetime_cumulative"


@pytest.mark.parametrize("count, truncated", [(0, False), (50, False), (51, True)])
def test_realtime_limit_is_not_claimed_to_be_total_distinct_sql(count, truncated):
    calls = []
    rows = [row(child_number=index) for index in range(count)]
    conn = SimpleNamespace(version="19.0.0.0.0", cursor=lambda: Cursor(rows, calls))
    @contextmanager
    def connect(instance):
        yield conn
    with patch.object(slow_sql_analysis, "oracle_connection", connect):
        result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"))
    assert result["count"] == min(count, 50)
    assert result["metadata"]["truncated"] is truncated
    assert result["metadata"]["distinct_sql_ids"] == (1 if count else 0)


def test_failed_read_is_degraded_not_successful_empty():
    with patch.object(slow_sql_analysis, "oracle_connection", side_effect=RuntimeError("synthetic failed read")):
        result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"))
        detail = slow_sql_analysis.get_sql_detail(SimpleNamespace(id=1, instance_name="offline"), "abc123")
    assert result["status"] == "degraded" and result["execution_status"] == "failed"
    assert result["count"] == 0 and not result["metadata"]["persisted"]
    assert result["category"] == "top_avg" and result["rows"] == []
    assert result["message"] and result["warnings"]
    assert isinstance(result["warnings"], list)
    assert detail["sql_id"] == "abc123" and detail["status"] == "degraded"
    assert detail["sql_fulltext"] == "无法获取SQL文本"
    assert detail["plan_text"] == "" and detail["awr_text"] is None
    assert detail["message"] and detail["warnings"]
    assert isinstance(detail["warnings"], list)


@pytest.fixture
def mysql_source(db, monkeypatch):
    import importlib
    import pymysql
    from app.collectors.mysql.mysql_health_collector import MySQLHealthCollector

    connection_module = importlib.import_module("app.collectors.mysql.mysql_connection")
    instance = db.get(DBInstance, 1)
    instance.db_type = "mysql"
    db.commit()
    cursor, connection = MagicMock(), MagicMock()
    cursor.fetchall.return_value = [{"DIGEST": "digest1", "DIGEST_TEXT": "select ?",
        "COUNT_STAR": 2, "TOTAL_SECONDS": Decimal("8"), "AVG_SECONDS": Decimal("4"),
        "SUM_ROWS_EXAMINED": 10}]
    connection.cursor.return_value = cursor
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(pymysql, "connect", connect)
    monkeypatch.setattr(connection_module, "decrypt_password", lambda value: "synthetic-only")
    monkeypatch.setattr(connection_module, "database_tls_material", lambda value: nullcontext({}))
    monkeypatch.setattr(MySQLHealthCollector, "collect", MagicMock(side_effect=AssertionError(
        "Standalone slow SQL collection must not run the full health collector")))
    return cursor, connection, connect


@pytest.mark.parametrize("rows, status", [(None, 502), ([{"error": "denied"}], 502), ([], 200),
    ([{"digest": "digest1", "digest_text": "select ?", "count_star": 2,
       "total_seconds": 8, "avg_seconds": 4, "sum_rows_examined": 10}], 200)])
def test_mysql_contract_is_validated_and_history_is_preserved(db, mysql_source, rows, status):
    cursor, connection, connect = mysql_source
    cursor.fetchall.return_value = rows
    old_id = old_history(db)
    response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == status
    assert db.get(DBSlowSQL, old_id) is not None
    cursor.close.assert_called_once_with()
    connection.close.assert_called_once_with()
    if status == 200:
        result = response.json()
        assert result["read_count"] == len(rows)
        assert result["saved_count"] == len(rows)
        assert result["analysis_status"] == "not_run"


def test_mysql_reads_only_digest_and_bounds_driver_waits(db, mysql_source):
    from app.collectors.mysql.mysql_health_collector import MYSQL_QUERIES

    cursor, connection, connect = mysql_source
    result = client(db).post("/slow-sql/collect/1?hours=3")
    assert result.status_code == 200
    cursor.execute.assert_called_once_with(MYSQL_QUERIES["slow_sql"], None)
    assert connect.call_count == 1
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert connect.call_args.kwargs["read_timeout"] == 10
    cursor.close.assert_called_once_with()
    connection.close.assert_called_once_with()
    body = result.json()
    assert body["saved_count"] == 1
    assert body["source_metadata"]["statistics_scope"] == "digest_cumulative"
    assert not body["source_metadata"]["hours_applied"]
    assert not body["source_metadata"]["window_aggregation"]
    saved = db.get(DBSlowSQL, body["record_ids"][0])
    assert saved.sql_text == "select ?" and saved.avg_elapsed_ms == Decimal("4000")


@pytest.mark.parametrize("stage", ["connect", "execute", "fetchall"])
@pytest.mark.parametrize("failure", [TimeoutError("synthetic read timeout"),
                                      RuntimeError("synthetic access denied")])
def test_mysql_failed_query_is_502_closes_resources_and_preserves_history(db, mysql_source, stage, failure):
    cursor, connection, connect = mysql_source
    old_id = old_history(db)
    (connect if stage == "connect" else getattr(cursor, stage)).side_effect = failure
    response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 502
    assert [item.id for item in db.query(DBSlowSQL).all()] == [old_id]
    if stage != "connect":
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()


def test_mysql_commit_failure_closes_source_and_rolls_back_new_records(db, mysql_source):
    cursor, connection, _ = mysql_source
    old_id = old_history(db)
    with patch.object(db, "commit", side_effect=RuntimeError("synthetic commit failure")):
        response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 500
    assert [item.id for item in db.query(DBSlowSQL).all()] == [old_id]
    cursor.close.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["cursor", "cursor_close", "connection_close"])
def test_mysql_resource_failures_do_not_save_a_successful_batch(db, mysql_source, stage):
    cursor, connection, _ = mysql_source
    old_id = old_history(db)
    method = {"cursor": connection.cursor, "cursor_close": cursor.close,
              "connection_close": connection.close}[stage]
    method.side_effect = RuntimeError("synthetic resource failure")
    response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 502
    assert [item.id for item in db.query(DBSlowSQL).all()] == [old_id]
    connection.close.assert_called_once_with()
    if stage != "cursor":
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("count", [2**31, 2**40, 2**53 - 1])
def test_large_counters_survive_collection_list_and_report_without_rounding(db, count):
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot([
        row(buffer_gets=count, disk_reads=count, rows_processed=count)
    ])):
        result = client(db).post("/slow-sql/collect/1")
    assert result.status_code == 200
    record_id = result.json()["record_ids"][0]
    listed = client(db).get("/slow-sql?instance_id=1").json()["items"][0]
    assert listed["id"] == record_id
    assert listed["logical_reads"] == listed["physical_reads"] == listed["rows_processed"] == count
    from app.api.v1.report import _collect_unified_reports
    from app.services.report_center import _source_markdown

    source = _collect_unified_reports(db, report_type="slow_sql")[0]
    assert source["slow_sql_detail"]["metrics"]["logical_reads"] == count
    assert str(count) in _source_markdown(source)


@pytest.mark.parametrize("count", [3_000_000_000, 2**40])
def test_oracle_checks_elapsed_capacity_in_stored_milliseconds_not_source_microseconds(db, count):
    with patch.object(slow_sql_analysis, "get_slow_sql_list", return_value=snapshot([
        row(executions=count, elapsed_time_us=count * 4_000_000)
    ])):
        response = client(db).post("/slow-sql/collect/1")
    assert response.status_code == 200
    saved = db.get(DBSlowSQL, response.json()["record_ids"][0])
    assert saved.executions == count
    assert saved.avg_elapsed_ms == Decimal("4000")
    assert saved.total_elapsed_ms == count * 4000


def test_microsecond_conversion_retains_exact_decimal_capacity_guard():
    assert slow_sql._number("9999999999999999990", "total_elapsed_ms", scale=Decimal("0.001")) == Decimal(
        "9999999999999999.99")
    with pytest.raises(slow_sql.SlowSQLCollectionError, match="total_elapsed_ms"):
        slow_sql._number("9999999999999999991", "total_elapsed_ms", scale=Decimal("0.001"))


def test_existing_oracle_endpoint_rejects_invalid_hours_before_source_query(db):
    from app.api.v1.oracle import oracle_slow_sql_realtime
    app = FastAPI()
    app.get("/oracle/{instance_id}/slow-sql-realtime")(oracle_slow_sql_realtime)
    app.dependency_overrides[get_db] = lambda: db
    with patch.object(slow_sql_analysis, "get_slow_sql_list", side_effect=AssertionError("must not query")):
        for hours in (0, -1, 169):
            assert TestClient(app).get(f"/oracle/1/slow-sql-realtime?hours={hours}").status_code == 422


def test_pre_cdb_oracle_keeps_non_container_identity_without_invalid_columns():
    calls = []
    conn = SimpleNamespace(version="11.2.0.4.0", cursor=lambda: Cursor([], calls))
    @contextmanager
    def connect(instance):
        yield conn
    with patch.object(slow_sql_analysis, "oracle_connection", connect):
        result = slow_sql_analysis.get_slow_sql_list(SimpleNamespace(id=1, instance_name="offline"))
    assert result["execution_status"] == "success"
    assert "child_number, 0 AS con_id," in calls[-1][0]
    assert "'NON-CDB' AS container_name" in calls[0][0]
