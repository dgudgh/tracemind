"""Regression coverage for template scope and truthful slow-SQL persistence."""
import socket
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.analyzers.rules import Finding
from app.collectors.mysql.mysql_health_collector import MYSQL_QUERIES, MySQLHealthCollector
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.slow_sql import DBSlowSQL
from app.services import system_settings
from app.services.inspection import report_builder, result_writer, runner, script_db_checks


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("No network in inspection quality tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(system_settings, "get_int", lambda db, key, default: default)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    for model in (DBInstance, DBSlowSQL, DBCheckResult):
        model.__table__.create(engine)
    with Session(engine) as session:
        target = DBInstance(instance_name="synthetic", db_type="mysql", host="unused.invalid",
                            port=3306, username="monitor", password_cipher="unused", enabled=1)
        session.add(target)
        session.flush()
        session.add(DBSlowSQL(instance_id=target.id, db_type="mysql", sql_id="history", sql_text="keep"))
        session.commit()
        yield session
    engine.dispose()


def mysql_row(**changes):
    return {"digest": "selected", "schema_name": "APP", "digest_text": "select 1",
            "count_star": 2, "avg_seconds": "1.5", "total_seconds": "3",
            "sum_rows_examined": 4, **changes}


@pytest.fixture
def mysql_source(monkeypatch):
    legacy = Mock(side_effect=AssertionError("Full health collection is forbidden"))
    monkeypatch.setattr(MySQLHealthCollector, "collect", legacy)
    factory = MagicMock()
    cursor = factory.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [mysql_row()]
    monkeypatch.setattr(runner, "mysql_connection", factory, raising=False)
    finding = Finding("Basic", "Version", "normal", "version", "8", "", "synthetic", "", {})
    script = Mock(return_value=[finding])
    monkeypatch.setattr(script_db_checks, "collect_mysql_script_findings", script)
    return factory, cursor, legacy, script


@pytest.mark.parametrize("items", [
    [], [{"key": "mysql.script.basic_info"}],
    [{"key": "mysql.script.long_queries", "enabled": False}],
    [{"key": "mysql.slow_queries.per_min", "enabled": "false"}],
    [{"key": "mysql.innodb_buffer_pool.hit_pct"}],
    [{"key": "mysql.script.long_queries", "source_key": "mysql.script.basic_info"}],
])
def test_unselected_mysql_never_runs_extra_collector(db, mysql_source, items):
    factory, cursor, legacy, script = mysql_source
    findings = runner._collect_mysql(db, db.query(DBInstance).one(), SimpleNamespace(item_config_json=items))
    legacy.assert_not_called()
    factory.assert_not_called()
    assert len(findings) == 1
    assert db.query(DBSlowSQL).count() == 1


@pytest.mark.parametrize("key", [None, "performance.slow_sql", "mysql.script.long_queries",
                                 "mysql.slow_queries.per_min", "mysql.script.performance"])
@pytest.mark.parametrize("empty", [False, True])
def test_selected_mysql_uses_one_bounded_digest_query(db, mysql_source, key, empty):
    factory, cursor, legacy, script = mysql_source
    cursor.fetchall.return_value = [] if empty else [mysql_row()]
    target = db.query(DBInstance).one()
    policy = None if key is None else SimpleNamespace(item_config_json=[{"key": "alias", "source_key": key}])
    findings = runner._collect_mysql(db, target, policy)
    legacy.assert_not_called()
    factory.assert_called_once_with(target, read_timeout=10)
    cursor.execute.assert_called_once_with(MYSQL_QUERIES["slow_sql"])
    factory.return_value.__enter__.return_value.cursor.return_value.__exit__.assert_called_once()
    factory.return_value.__exit__.assert_called_once()
    saved = next(item for item in findings if item.metric_name == "mysql.slow_sql.persistence")
    assert result_writer.finding_execution_status(saved) == "success"
    assert saved.raw_data["count"] == (0 if empty else 1)
    result_writer.save_findings(db, target, "selected", findings)
    report = report_builder.build_report_payload(db, "selected", target.id)
    assert report["task"]["task_status"] == "success"
    assert db.query(DBSlowSQL).count() == (1 if empty else 2)
    if not empty:
        row = db.query(DBSlowSQL).filter_by(sql_id="selected").one()
        assert row.id in saved.raw_data["record_ids"]
        assert row.avg_elapsed_ms == Decimal("1500") and row.executions == 2


@pytest.mark.parametrize("failure", ["connect", "query", "insert"])
def test_mysql_failure_is_reported_and_preserves_callers_transaction(db, mysql_source, failure):
    factory, cursor, legacy, script = mysql_source
    target = db.query(DBInstance).one()
    target.instance_name = "pending-caller-change"
    db.flush()
    def fail_insert(*args):
        raise RuntimeError("synthetic insert failure")
    if failure == "connect":
        factory.return_value.__enter__.side_effect = RuntimeError("synthetic connection failure")
    elif failure == "query":
        cursor.execute.side_effect = TimeoutError("synthetic query timeout")
    else:
        event.listen(DBSlowSQL, "before_insert", fail_insert)
    try:
        findings = runner._collect_mysql(db, target, SimpleNamespace(item_config_json=[{"key": "mysql.script.long_queries"}]))
    finally:
        if failure == "insert":
            event.remove(DBSlowSQL, "before_insert", fail_insert)
    saved = next(item for item in findings if item.metric_name == "mysql.slow_sql.persistence")
    assert result_writer.finding_execution_status(saved) == "failed"
    assert saved.raw_data["count"] == 0 and saved.raw_data["record_ids"] == []
    assert saved.raw_data["errors"]
    result_writer.save_findings(db, target, "failed", findings)
    report = report_builder.build_report_payload(db, "failed", target.id)
    assert report["task"]["task_status"] == "partial" and report["task"]["overall_score"] is None
    assert db.query(DBSlowSQL).one().sql_id == "history"
    assert db.query(DBInstance).one().instance_name == "pending-caller-change"
    legacy.assert_not_called()


@pytest.mark.parametrize("rows", [None, [{"error": "denied"}], [mysql_row(count_star=2**53)],
    [mysql_row(avg_seconds="NaN")], [mysql_row(total_seconds="Infinity")], [mysql_row(digest_text="")]])
def test_invalid_mysql_batch_is_failed_without_history_loss(db, rows):
    outcome = runner._save_mysql_slow_sql(db, db.query(DBInstance).one(), {"slow_sql": rows})
    assert outcome["status"] == "failed" and outcome["count"] == 0 and outcome["errors"]
    db.commit()
    assert db.query(DBSlowSQL).one().sql_id == "history"


def test_mysql_large_counters_are_not_clamped(db):
    outcome = runner._save_mysql_slow_sql(db, db.query(DBInstance).one(), {"slow_sql": [
        mysql_row(count_star=3_000_000_000, sum_rows_examined=2**53 - 1)
    ]})
    db.commit()
    assert outcome["status"] == "success"
    record = db.get(DBSlowSQL, outcome["record_ids"][0])
    assert record.executions == 3_000_000_000
    assert record.rows_processed == 2**53 - 1


def test_mysql_select_only_outer_rollback_removes_new_rows(db):
    target = db.query(DBInstance).one()
    driver = db.connection().connection.driver_connection
    assert db.in_transaction() and not driver.in_transaction
    outcome = runner._save_mysql_slow_sql(db, target, {"slow_sql": [mysql_row()]})
    assert outcome["status"] == "success" and outcome["count"] == 1
    db.rollback()
    with Session(db.get_bind()) as verify:
        assert [row.sql_id for row in verify.query(DBSlowSQL).all()] == ["history"]


def test_mysql_select_only_report_insert_failure_rolls_back_slow_sql(db, mysql_source):
    target = db.query(DBInstance).one()
    driver = db.connection().connection.driver_connection
    assert db.in_transaction() and not driver.in_transaction
    findings = runner._collect_mysql(db, target, SimpleNamespace(
        item_config_json=[{"key": "mysql.script.long_queries"}],
    ))
    saved = next(item for item in findings if item.metric_name == "mysql.slow_sql.persistence")
    assert saved.raw_data["status"] == "success" and saved.raw_data["count"] == 1

    def fail_report_insert(*args):
        raise RuntimeError("synthetic report insert failure")

    event.listen(DBCheckResult, "before_insert", fail_report_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic report insert failure"):
            result_writer.save_findings(db, target, "report-failed", findings)
    finally:
        event.remove(DBCheckResult, "before_insert", fail_report_insert)
        db.rollback()
    with Session(db.get_bind()) as verify:
        assert [row.sql_id for row in verify.query(DBSlowSQL).all()] == ["history"]
        assert verify.query(DBCheckResult).count() == 0


def test_mysql_existing_sqlite_transaction_is_not_restarted(db):
    target = db.query(DBInstance).one()
    target.instance_name = "pending-caller-change"
    db.flush()
    assert db.connection().connection.driver_connection.in_transaction
    outcome = runner._save_mysql_slow_sql(db, target, {"slow_sql": [mysql_row()]})
    assert outcome["status"] == "success"
    db.commit()
    with Session(db.get_bind()) as verify:
        assert verify.query(DBSlowSQL).count() == 2
        assert verify.query(DBInstance).one().instance_name == "pending-caller-change"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_mysql_savepoint_preserves_non_sqlite_transaction_path(dialect):
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect
    outcome = runner._save_mysql_slow_sql(session, SimpleNamespace(id=1), {"slow_sql": [mysql_row()]})
    assert outcome["status"] == "success"
    session.begin_nested.assert_called_once_with()
    session.connection.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def oracle_metrics(**changes):
    row = {"sql_id": "oracle-selected", "sql_text": "select 1 from dual", "schema_name": "APP",
           "con_id": 12, "child_number": 0, "plan_hash": 42, "executions": 2,
           "elapsed_seconds": 3, "buffer_gets": 100, "disk_reads": 0, "rows_processed": 4, **changes}
    return {key: [dict(row)] for key in ("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads")}


@pytest.mark.parametrize("field", ["executions", "buffer_gets", "disk_reads", "rows_processed"])
@pytest.mark.parametrize("value", [2**53, "NaN", "Infinity", -1, "1.5", True])
def test_oracle_invalid_counters_fail_without_clamping(db, field, value):
    target = db.query(DBInstance).one()
    target.instance_name = "pending-caller-change"
    db.flush()
    outcome = runner._save_oracle_slow_sql(db, target, oracle_metrics(**{field: value}))
    assert outcome["status"] == "failed" and outcome["errors"]
    assert outcome["count"] == 0 and outcome["record_ids"] == []
    db.commit()
    assert db.query(DBSlowSQL).one().sql_id == "history"
    assert db.query(DBInstance).one().instance_name == "pending-caller-change"


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "-Infinity", -1, "10000000000000"])
def test_oracle_invalid_elapsed_time_fails(db, value):
    outcome = runner._save_oracle_slow_sql(db, db.query(DBInstance).one(), oracle_metrics(elapsed_seconds=value))
    assert outcome["status"] == "failed" and outcome["count"] == 0 and outcome["errors"]
    assert db.query(DBSlowSQL).one().sql_id == "history"


@pytest.mark.parametrize("count", [0, 2_147_483_647, 2**31, 2**40])
def test_oracle_representable_boundary_is_exact(db, count):
    outcome = runner._save_oracle_slow_sql(db, db.query(DBInstance).one(), oracle_metrics(
        executions=count, buffer_gets=count, disk_reads=count, rows_processed=count,
        elapsed_seconds=Decimal(count) * Decimal("0.8")))
    db.commit()
    assert outcome["status"] == "success"
    row = db.query(DBSlowSQL).filter_by(sql_id="oracle-selected").one()
    assert row.executions == row.logical_reads == row.physical_reads == row.rows_processed == count
    assert row.avg_elapsed_ms == (800 if count else 0)


def test_oracle_json_safe_read_counter_boundary_is_exact(db):
    count = 2**53 - 1
    outcome = runner._save_oracle_slow_sql(db, db.query(DBInstance).one(), oracle_metrics(
        buffer_gets=count, disk_reads=count, rows_processed=count))
    db.commit()
    assert outcome["status"] == "success"
    row = db.query(DBSlowSQL).filter_by(sql_id="oracle-selected").one()
    assert row.logical_reads == row.physical_reads == row.rows_processed == count
