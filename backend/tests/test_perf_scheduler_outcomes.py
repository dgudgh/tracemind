from datetime import datetime, timedelta
from unittest.mock import patch
import socket

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.db_instance import DBInstance
from app.models.perf_metric import DBPerfMetric
from app.models.perf_metric_policy import DBPerfMetricPolicy
from app.services import perf_baseline, system_settings
from app.tasks import master_scheduler


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Performance scheduler tests must not contact remote systems")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    for model in (DBInstance, DBPerfMetricPolicy, DBPerfMetric):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def seed(sessions, states):
    with sessions() as db:
        for index, state in enumerate(states, 1):
            db.add(DBInstance(id=index, instance_name=f"test-{index}", db_type="oracle",
                              host="192.0.2.1", port=1521, username="test",
                              password_cipher="unused", enabled=0 if state == "disabled" else 1))
            db.add(DBPerfMetricPolicy(instance_id=index, enabled=1, interval_minutes=30))
        db.commit()


def dispatch(sessions, states, retention_error=False):
    def collect(instance):
        state = states[instance.id - 1]
        if state == "failed":
            raise RuntimeError("synthetic connection refused")
        return {} if state == "empty" else {"qps": 12.5}

    with (patch.object(master_scheduler, "SessionLocal", sessions),
          patch.object(perf_baseline, "_collect_oracle", side_effect=collect),
          patch.object(system_settings, "get_int", side_effect=RuntimeError("retention unavailable")
                       if retention_error else None, return_value=90)):
        return master_scheduler.run_due_perf_jobs()


@pytest.mark.parametrize("states,expected", [
    ([], "no_work"), (["success"], "success"), (["failed"], "failed"),
    (["success", "failed"], "partial"), (["disabled"], "skipped"),
    (["empty"], "unknown"),
])
def test_collection_policy_and_dispatch_history_have_truthful_outcomes(sessions, states, expected):
    seed(sessions, states)
    result = dispatch(sessions, states)
    assert result["status"] == expected
    saved = master_scheduler.normalize_business_result(result)
    assert saved["status"] == expected
    assert saved["summary"]["policy_results"] == result["policy_results"]
    with sessions() as db:
        assert db.query(DBPerfMetric).count() == states.count("success")
        policies = db.query(DBPerfMetricPolicy).order_by(DBPerfMetricPolicy.id).all()
        for policy, state in zip(policies, states):
            assert policy.last_status == {"failed": "error", "disabled": "skipped",
                                          "empty": "warning"}.get(state, "success")
            assert policy.next_run_time > policy.last_run_time


@pytest.mark.parametrize("states,expected", [([], "failed"), (["success"], "partial"), (["failed"], "failed")])
def test_retention_failure_is_visible_without_losing_collected_metrics(sessions, states, expected):
    seed(sessions, states)
    result = dispatch(sessions, states, retention_error=True)
    assert result["status"] == expected
    assert result["retention"]["status"] == "failed"
    saved = master_scheduler.normalize_business_result(result)
    assert saved["status"] == expected
    assert saved["summary"]["retention"]["status"] == "failed"
    with sessions() as db:
        assert db.query(DBPerfMetric).count() == states.count("success")


def test_retention_reports_real_work_when_no_policy_is_due(sessions):
    with sessions() as db:
        db.add(DBPerfMetric(instance_id=1, db_type="oracle", metric_name="qps", value=1,
                            capture_time=datetime.now() - timedelta(days=100)))
        db.commit()
    result = dispatch(sessions, [])
    assert result["status"] == "success"
    assert result["retention"] == {"status": "success", "deleted": 1}
    assert result["processed"] == 0


def test_policy_commit_failure_rolls_back_then_continues_and_closes_session(sessions):
    seed(sessions, ["success", "success"])
    db = sessions()
    commit = db.commit
    calls = 0

    def flaky_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("policy update unavailable")
        return commit()

    with (patch.object(master_scheduler, "SessionLocal", return_value=db),
          patch.object(db, "commit", side_effect=flaky_commit),
          patch.object(db, "close", wraps=db.close) as close,
          patch.object(perf_baseline, "_collect_oracle", return_value={"qps": 1}),
          patch.object(system_settings, "get_int", return_value=90)):
        result = master_scheduler.run_due_perf_jobs()
    close.assert_called_once()
    assert result["status"] == "partial"
    assert [row["status"] for row in result["policy_results"]] == ["failed", "success"]
    assert result["policy_results"][0]["policy_status_persisted"] is False
    with sessions() as fresh:
        assert fresh.query(DBPerfMetric).count() == 2
        assert fresh.query(DBPerfMetricPolicy).filter_by(instance_id=2).one().last_status == "success"


def test_partial_collector_result_does_not_hide_error_behind_nonzero_count(sessions):
    seed(sessions, ["success"])
    with sessions() as db, patch.object(perf_baseline, "collect_perf_metrics", return_value={
        "count": 1, "errors": ["synthetic metric unavailable"],
    }):
        policy = db.query(DBPerfMetricPolicy).one()
        result = perf_baseline.run_collect_by_policy(db, policy)
        assert result["status"] == policy.last_status == "partial"
        assert "synthetic metric unavailable" in result["message"]


@pytest.mark.parametrize("status,expected", [("failed", "partial"), ({}, "partial"), ("success", "success")])
def test_retention_evidence_cannot_be_hidden_by_successful_dispatch(status, expected):
    result = master_scheduler.normalize_business_result({"status": "success", "retention": {"status": status}})
    assert result["status"] == expected
