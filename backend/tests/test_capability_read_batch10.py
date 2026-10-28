"""C13 batch 10: investigation bundle, config summary, log query and APM trace."""
from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.observability import ObservabilitySource
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services.observability import apm, log_query


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            User(id=1, username="c13g-admin", password_hash="hash", role="admin", status="active"),
            DBInstance(id=10, instance_name="c13g-db", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", password_cipher="env://C13G", enabled=1),
            ObservabilitySource(id=1, source_name="logs", source_type="elasticsearch",
                                api_url="http://logs.invalid", enabled=1),
            ObservabilitySource(id=2, source_name="apm", source_type="jaeger",
                                api_url="http://apm.invalid", enabled=1),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def read(db, capability, **kwargs):
    entry = registry._entry(capability)
    return registry.execute_read(db, capability, entry["version"], actor={"user_id": 1}, **kwargs)


def test_investigation_bundle_returns_five_sections(db):
    value = read(db, "diagnosis.investigation.read")
    assert set(value) >= {"items", "matches", "captures", "probe_leases", "kernel_capabilities"}


def test_config_summary_is_scoped_and_read_only(db):
    value = read(db, "operations.config_governance.read")
    assert value["summary"]["scoped_instance_count"] == 1
    assert value["drifts"] == []


def test_a_log_query_uses_an_explicit_source_and_records_an_audit(db, monkeypatch):
    monkeypatch.setattr(log_query, "fetch_log_payload", lambda source, query: {"hits": {"hits": [
        {"_id": "1", "_source": {"@timestamp": "2026-10-20T10:00:00Z", "message": "boom", "level": "error"}},
    ]}})
    now = datetime.datetime.now()
    value = read(db, "monitor.log.read", instance_id=10, params={
        "source_id": 1, "start_time": (now - datetime.timedelta(hours=1)).isoformat(),
        "end_time": now.isoformat(), "keyword": "boom", "limit": 10,
    })
    assert value["count"] == 1 and value["items"][0]["message"] == "boom"


def test_log_query_without_a_source_is_refused_not_empty(db):
    now = datetime.datetime.now()
    with pytest.raises(registry.CapabilityError) as error:
        read(db, "monitor.log.read", instance_id=10, params={
            "source_id": 99, "start_time": (now - datetime.timedelta(hours=1)).isoformat(),
            "end_time": now.isoformat(), "limit": 10,
        })
    assert error.value.code == "invalid_params"


def test_apm_trace_returns_spans_from_the_adapter(db, monkeypatch):
    monkeypatch.setattr(apm, "fetch_trace_payload", lambda source, trace_id: {"data": [{
        "traceID": trace_id,
        "spans": [{"spanID": "s1", "operationName": "SELECT", "startTime": 1_700_000_000_000_000,
                   "duration": 1000, "tags": [{"key": "span.kind", "value": "server"}]}],
    }]})
    value = read(db, "monitor.apm_trace.read", instance_id=10,
                 params={"source_id": 2, "trace_id": "trace-1"})
    assert value["trace_id"] == "trace-1" and value["total"] == 1
