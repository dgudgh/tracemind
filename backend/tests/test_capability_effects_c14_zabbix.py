"""C14: Zabbix host/problem sync against a real loopback JSON-RPC server."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.external_monitoring import ZabbixConfig, ZabbixHostMapping, ZabbixProblemCache
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services import plan_governance as governance, plan_service as plans

REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 4}


class _Zabbix(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        if method == "user.login":
            result = "token-123"
        elif method == "host.get":
            result = [{"hostid": "1", "host": "db1", "name": "DB1", "status": "0",
                       "interfaces": [{"ip": "10.0.0.1", "main": "1"}]}]
        elif method == "problem.get":
            result = [{"eventid": "100", "objectid": "1", "clock": "1700000000", "name": "CPU high",
                       "severity": "4", "hostid": "1"}]
        else:
            result = []
        body = json.dumps({"jsonrpc": "2.0", "result": result, "id": payload.get("id", 1)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def zabbix():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Zabbix)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ZABBIX_SYNC_PASSWORD", "secret")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            User(id=1, username="c14z-requester", password_hash="unused", role="admin", status="active"),
            User(id=4, username="c14z-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="mysql-10", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", enabled=1, password_cipher="env://SYNTHETIC_ZBX_PASSWORD"),
            AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="asset-10", db_type="mysql",
                          ip="127.0.0.1", port=3306, host_id=10, region_id=1, site_id=1, zone_id=1, env_id=1,
                          status="active"),
        ])
        session.commit()
        registry.sync_definitions(session, actor={"user_id": 1})
        yield session
    engine.dispose()


def _run(db, config_id):
    created = plans.create_plan(db, actor=REQUESTER, capability_id="integration.zabbix.sync",
                                definition_version=3, instance_id=10, params={"config_id": config_id})
    governance.request_approval(db, created["plan_id"], actor=REQUESTER, expected_revision=created["revision"])
    row = plans._load(db, created["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(db, created["plan_id"], actor=APPROVER,
                            expected_revision=created["revision"], expected_binding_hash=row.approval_binding_hash)
    return plans.execute_plan(db, created["plan_id"], actor=REQUESTER, execution_guard=lambda: None)


def test_sync_persists_hosts_and_problems(db, zabbix):
    db.add(ZabbixConfig(id=1, config_name="zbx", base_url=zabbix, username="api",
                        password_cipher="env://ZABBIX_SYNC_PASSWORD", enabled=1))
    db.commit()
    finished = _run(db, 1)
    assert finished["outcome"] == "succeeded", finished
    inline = finished["steps"][0]["result_ref"]["inline"]["data"]
    assert inline["host_count"] == 1 and inline["problem_count"] == 1
    assert db.query(ZabbixHostMapping).count() == 1
    assert db.query(ZabbixProblemCache).count() == 1


def test_unreachable_zabbix_is_not_success(db):
    db.add(ZabbixConfig(id=2, config_name="dead", base_url="http://127.0.0.1:1", username="api",
                        password_cipher="env://ZABBIX_SYNC_PASSWORD", enabled=1))
    db.commit()
    finished = _run(db, 2)
    assert finished["outcome"] != "succeeded"
