from copy import deepcopy
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.slow_sql import DBSlowSQL
from app.services import system_settings
from app.services.inspection import runner, result_writer, oracle_script_checks


SOURCES = ("top_sql_elapsed", "top_sql_buffer_gets", "top_sql_disk_reads")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("No network in runner persistence tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(system_settings, "get_int", lambda db, key, default: default)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oracle-slow-sql.sqlite'}")
    DBInstance.__table__.create(engine)
    DBSlowSQL.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def instance(db):
    row = DBInstance(instance_name="oracle-test", db_type="oracle", host="unused.invalid", port=1521,
                     username="monitor", password_cipher="placeholder")
    db.add(row)
    db.commit()
    return row


def sql_row(sql_id="real-key", child=0):
    return {"sql_id": sql_id, "child_number": child, "schema_name": "APP", "plan_hash": 42,
            "sql_text": "SELECT * FROM orders WHERE id = :id", "elapsed_seconds": 20,
            "executions": 2, "buffer_gets": 100, "disk_reads": 4, "rows_processed": 5}


def metrics():
    return {source: [sql_row()] for source in SOURCES}


def test_actual_three_keys_persist_deduplicated_rows_with_text_schema_and_units(db, instance):
    data = metrics()
    data[SOURCES[1]].append(sql_row("second"))
    data[SOURCES[2]].append(sql_row("real-key", child=1))
    result = runner._save_oracle_slow_sql(db, instance, data)
    db.commit()
    assert result["status"] == "success"
    assert result["count"] == 3
    rows = db.query(DBSlowSQL).order_by(DBSlowSQL.id).all()
    assert [row.id for row in rows] == result["record_ids"]
    for row in rows:
        assert row.sql_text == sql_row()["sql_text"]
        assert row.schema_name == "APP"
        assert row.avg_elapsed_ms == 10000
        assert row.total_elapsed_ms == 20000
        assert row.executions == 2
        assert row.logical_reads == 100
        assert row.risk_level == "critical"


@pytest.mark.parametrize("data", [{}, {"top_sql": [sql_row()]}, {source: [{"error": "denied"}] for source in SOURCES}])
def test_missing_actual_keys_or_errors_are_failed_not_zero_success(db, instance, data):
    result = runner._save_oracle_slow_sql(db, instance, data)
    assert result["status"] == "failed"
    assert result["count"] == 0
    assert result["errors"]
    assert db.query(DBSlowSQL).count() == 0


def test_one_source_failure_keeps_valid_evidence_and_returns_partial(db, instance):
    data = metrics()
    data[SOURCES[1]] = [{"error": "synthetic permission denied"}]
    result = runner._save_oracle_slow_sql(db, instance, data)
    assert result["status"] == "partial"
    assert result["count"] == 1
    assert "top_sql_buffer_gets" in result["errors"][0]
    assert db.query(DBSlowSQL).one().sql_text


def test_explicit_success_empty_is_success_but_warning_evidence_is_failure(db, instance):
    data = {source: [] for source in SOURCES}
    assert runner._save_oracle_slow_sql(db, instance, data)["status"] == "success"
    data["_query_evidence"] = {source: {"status": "warning", "message": "denied"} for source in SOURCES}
    assert runner._save_oracle_slow_sql(db, instance, data)["status"] == "failed"


def test_missing_sql_text_is_not_saved_as_blank_detail(db, instance):
    data = metrics()
    for rows in data.values():
        rows[0]["sql_text"] = ""
    result = runner._save_oracle_slow_sql(db, instance, data)
    assert result["status"] == "failed"
    assert db.query(DBSlowSQL).count() == 0


def test_same_sql_and_child_in_different_containers_are_not_deduplicated(db, instance):
    data = metrics()
    data[SOURCES[0]][0]["con_id"] = 12
    data[SOURCES[1]][0]["con_id"] = 13
    data[SOURCES[2]][0]["con_id"] = 12
    result = runner._save_oracle_slow_sql(db, instance, data)
    assert result["count"] == 2 and result["status"] == "success"


def test_oversized_text_rolls_back_new_batch_but_not_history(db, instance):
    prior = DBSlowSQL(instance_id=instance.id, db_type="oracle", sql_id="history", sql_text="kept")
    db.add(prior)
    db.commit()
    data = metrics()
    data[SOURCES[0]].append({**sql_row("large"), "sql_text": "中" * 21846})
    result = runner._save_oracle_slow_sql(db, instance, data)
    assert result["status"] == "failed" and result["count"] == 0
    assert "65535" in result["errors"][-1]
    assert [row.id for row in db.query(DBSlowSQL).all()] == [prior.id]


def test_savepoint_failure_is_visible_without_rolling_back_callers_changes(db, instance):
    instance.instance_name = "unrelated-pending-change"
    db.flush()
    def fail_insert(*args):
        raise RuntimeError("synthetic insert failure")
    event.listen(DBSlowSQL, "before_insert", fail_insert)
    try:
        result = runner._save_oracle_slow_sql(db, instance, metrics())
    finally:
        event.remove(DBSlowSQL, "before_insert", fail_insert)
    assert result["status"] == "failed"
    assert result["count"] == 0
    assert result["record_ids"] == []
    assert "synthetic insert failure" in result["errors"][-1]
    db.commit()
    assert db.get(DBInstance, instance.id).instance_name == "unrelated-pending-change"
    assert db.query(DBSlowSQL).count() == 0


@pytest.mark.parametrize("fail", [False, True])
def test_collect_oracle_records_persistence_status_in_report_findings(db, instance, monkeypatch, fail):
    data = metrics() if not fail else {source: [{"error": "denied"}] for source in SOURCES}
    monkeypatch.setattr(runner.OracleHealthCollector, "collect", lambda *args, **kwargs: deepcopy(data))
    monkeypatch.setattr(runner, "analyze_oracle_metrics", lambda metrics: [])
    monkeypatch.setattr(oracle_script_checks, "collect_oracle_script_findings", lambda *args, **kwargs: [])
    findings = runner._collect_oracle(db, instance)
    persistence = next(f for f in findings if f.metric_name == "oracle.slow_sql.persistence")
    assert result_writer.finding_execution_status(persistence) == ("failed" if fail else "success")
    assert persistence.raw_data["count"] == (0 if fail else 1)
    assert persistence.raw_data["source_keys"] == list(SOURCES)


def test_unselected_top_sql_is_not_persisted_or_misclassified_as_failure(db, instance, monkeypatch):
    monkeypatch.setattr(runner.OracleHealthCollector, "collect", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner, "analyze_oracle_metrics", lambda metrics: [])
    monkeypatch.setattr(oracle_script_checks, "collect_oracle_script_findings", lambda *args, **kwargs: [])
    save = Mock()
    monkeypatch.setattr(runner, "_save_oracle_slow_sql", save)
    runner._collect_oracle(db, instance, SimpleNamespace(item_config_json=[{"key": "oracle.query:oracle.blocking.seconds"}]))
    save.assert_not_called()
