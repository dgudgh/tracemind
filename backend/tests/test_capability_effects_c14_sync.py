"""C14: Prometheus standard-metric sync against a real loopback HTTP endpoint."""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.observability import ObservabilityCurrentMetric, ObservabilitySource
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


class _Prometheus(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"instance": "db1:9104", "job": "mysql"}, "value": [int(time.time()), "1"]}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def prometheus():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Prometheus)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id=1, username="c14s-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14s-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_SYNC_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="127.0.0.1", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _run(db, source_id):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="integration.prometheus.sync",
                                definition_version=3, instance_id=10, params={"source_id": source_id})
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_sync_rows_are_the_evidence(db, prometheus):
    db.add(ObservabilitySource(id=1, source_name="prom", source_type="prometheus",
                               api_url=prometheus, enabled=1, timeout_seconds=5))
    db.commit()
    finished = _run(db, 1)
    assert finished["outcome"] == "succeeded", finished
    stored = finished["steps"][0]["result_ref"]["inline"]["data"]["stored_count"]
    assert stored > 0
    assert db.query(ObservabilityCurrentMetric).filter_by(source_id=1).count() == stored


def test_an_unreachable_source_is_failed_not_success(db):
    db.add(ObservabilitySource(id=2, source_name="dead", source_type="prometheus",
                               api_url="http://127.0.0.1:1", enabled=1, timeout_seconds=2))
    db.commit()
    finished = _run(db, 2)
    assert finished["outcome"] == "failed"
    assert finished["outcome_kind"] == "sync_failed"
