from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models as _models  # noqa: F401 - populate SQLAlchemy metadata
from app.core.database import Base
from app.services.platform_observability import (
    liveness,
    readiness,
    render_runtime_metrics,
    route_template,
    runtime_metrics,
)


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_liveness_does_not_depend_on_database():
    payload = liveness()
    assert payload["status"] == "ok"
    assert payload["uptime_seconds"] >= 0
    assert payload["pid"] > 0


def test_readiness_checks_database_and_required_schema():
    engine, db = _session()
    try:
        ready, payload = readiness(db)
        assert ready is True
        assert payload["status"] == "ready"
        assert payload["checks"]["database"]["latency_ms"] >= 0
        assert payload["checks"]["schema"]["missing_tables"] == []
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_failed_database_probe_does_not_retry_schema_or_expose_driver_details():
    db = Mock()
    db.execute.side_effect = OperationalError("SELECT 1", {}, RuntimeError("private connection details"))
    ready, payload = readiness(db)
    assert ready is False
    assert payload["status"] == "not_ready"
    assert payload["checks"]["database"] == {"status": "failed", "error": "OperationalError"}
    assert payload["checks"]["schema"] == {"status": "not_checked", "reason": "database_unavailable"}
    db.execute.assert_called_once()
    db.rollback.assert_called_once()
    db.connection.assert_not_called()
    db.get_bind.assert_not_called()
    assert "private" not in str(payload)


def test_readiness_uses_one_checkout_for_database_and_schema():
    engine, db = _session()
    checkouts = []
    event.listen(engine, "checkout", lambda *args: checkouts.append(1))
    try:
        assert readiness(db)[0]
        assert len(checkouts) == 1
    finally:
        db.close()
        engine.dispose()


def test_available_database_with_missing_tables_is_not_ready():
    engine = create_engine("sqlite://")
    try:
        with sessionmaker(bind=engine)() as db:
            ready, payload = readiness(db)
            assert ready is False
            assert payload["checks"]["database"]["status"] == "ready"
            assert payload["checks"]["schema"]["status"] == "failed"
            assert "db_instance" in payload["checks"]["schema"]["missing_tables"]
    finally:
        engine.dispose()


def test_runtime_metrics_bound_route_cardinality_and_render_histograms():
    engine, db = _session()
    runtime_metrics.reset_for_tests()
    try:
        runtime_metrics.request_started()
        runtime_metrics.request_finished("get", "/api/v1/assets/instances/{instance_id}", 200, 0.08)
        runtime_metrics.request_started()
        runtime_metrics.request_finished("GET", "/api/v1/assets/instances/{instance_id}", 503, 0.7)
        rendered = render_runtime_metrics(db)
        assert 'route="/api/v1/assets/instances/{instance_id}"' in rendered
        assert 'status_class="2xx"} 1' in rendered
        assert 'status_class="5xx"} 1' in rendered
        assert "tracemind_http_request_duration_seconds_bucket" in rendered
        assert "tracemind_scheduler_active_leases" in rendered
        assert "tracemind_incident_upload_backlog" in rendered
        assert "tracemind_state_transitions_last_5m" in rendered
    finally:
        runtime_metrics.reset_for_tests()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_unmatched_paths_are_collapsed_instead_of_exposing_ids():
    request = SimpleNamespace(
        scope={},
        url=SimpleNamespace(path="/api/v1/assets/instances/secret-customer-id"),
    )
    assert route_template(request) == "/unmatched"
