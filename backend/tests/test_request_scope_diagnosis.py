"""Diagnosis request-scope checks use isolated SQLite and inert HTTP routes."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.routing import Route

from app.cmdb.models import AssetInstance
from app.models.access_control import UserAssetScope
from app.models.diagnosis_center import DiagIncident
from app.services import access_control, request_scope


PREFIX = "/api/v1"
INCIDENTS = "/diagnosis-center/incidents"
DETAIL = INCIDENTS + "/{incident_id}"
DIAGNOSE = "/diagnosis-center/diagnose"
ROUTES = (
    ("GET", INCIDENTS),
    ("GET", DETAIL),
    ("POST", DIAGNOSE),
)


async def inert_endpoint(request):
    raise AssertionError("Scope tests must not execute a business HTTP handler")


def route(template, method):
    return Route(PREFIX + template, inert_endpoint, methods=[method])


def make_request(path, method="GET", payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    url, _, query = (PREFIX + path).partition("?")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http",
        "method": method,
        "path": url,
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "app": SimpleNamespace(routes=[route(template, verb) for verb, template in ROUTES]),
    }, receive)
    request.state.auth = {"user_id": 7, "source": "db", "role": "user"}
    return request, body


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (AssetInstance, UserAssetScope, DiagIncident):
        model.__table__.create(engine)
    with Session(engine, autoflush=False) as session:
        for normalized, native in ((110, 10), (120, 20)):
            session.add(AssetInstance(
                id=normalized,
                native_instance_id=native,
                stable_key=f"diagnosis:{native}",
                name=f"diagnosis-{native}",
                db_type="mysql",
                ip="127.0.0.1",
                port=3306,
                host_id=normalized,
                region_id=1,
                site_id=1,
                zone_id=1,
                env_id=1,
                status="active",
            ))
        session.add_all([
            UserAssetScope(user_id=7, scope_type="instance", scope_id=110, access_level="read"),
            UserAssetScope(user_id=8, scope_type="instance", scope_id=110, access_level="operate"),
            DiagIncident(
                id=1,
                incident_no="INC-ALLOWED",
                instance_id=10,
                instance_name="diagnosis-10",
                db_type="mysql",
                title="allowed",
                started_at=datetime(2026, 10, 23),
            ),
            DiagIncident(
                id=2,
                incident_no="INC-FOREIGN",
                instance_id=20,
                instance_name="diagnosis-20",
                db_type="mysql",
                title="foreign",
                started_at=datetime(2026, 10, 23),
            ),
            DiagIncident(
                id=3,
                incident_no="INC-UNOWNED",
                instance_id=None,
                title="unowned",
                started_at=datetime(2026, 10, 23),
            ),
        ])
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture(params=["sync", "async"])
def allows(db, request):
    def check(path, method="GET", payload=None, *, user_id=7, minimum="read"):
        incoming, body = make_request(path, method, payload)
        with patch.object(access_control, "resolve_scope", wraps=access_control.resolve_scope) as resolve:
            if request.param == "sync":
                result = request_scope.scope_allows_request_sync(db, user_id, incoming, body)
            else:
                result = asyncio.run(request_scope.scope_allows_request(db, user_id, incoming))
        resolve.assert_called_once_with(db, user_id, minimum_access=minimum)
        assert not incoming.scope.get("path_params"), "scope check must run before routing"
        return result

    return check


def test_incident_collection_requires_explicit_allowed_instance_filter(allows):
    assert allows(INCIDENTS + "?instance_id=10")
    assert not allows(INCIDENTS)
    assert not allows(INCIDENTS + "?instance_id=")
    assert not allows(INCIDENTS + "?instance_id=20")
    assert not allows(INCIDENTS + "?instance_id=10&instance_id=20")


@pytest.mark.parametrize("incident_id,expected", [(1, True), (2, False), (3, False), (999, False)])
def test_incident_detail_resolves_persisted_instance_owner(allows, incident_id, expected):
    assert allows(DETAIL.replace("{incident_id}", str(incident_id))) is expected


def test_incident_detail_owner_cannot_be_replaced_by_query_hint(allows):
    assert not allows(DETAIL.replace("{incident_id}", "2") + "?instance_id=10")
    assert not allows(DETAIL.replace("{incident_id}", "1") + "?instance_id=20")


def test_direct_diagnosis_requires_top_level_operate_scoped_instance(allows):
    assert allows(DIAGNOSE, "POST", {"instance_id": 10, "symptom": "connections blocked"},
                  user_id=8, minimum="operate")
    assert not allows(DIAGNOSE, "POST", {"instance_id": 20, "symptom": "foreign"},
                      user_id=8, minimum="operate")
    assert not allows(DIAGNOSE, "POST", {"symptom": "missing target"},
                      user_id=8, minimum="operate")
    assert not allows(DIAGNOSE + "?instance_id=10", "POST", {"symptom": "query is not the body anchor"},
                      user_id=8, minimum="operate")
    assert not allows(DIAGNOSE, "POST", {"context": {"instance_id": 10}},
                      user_id=8, minimum="operate")
    assert not allows(DIAGNOSE, "POST", {"instance_id": True},
                      user_id=8, minimum="operate")


def test_read_only_instance_scope_cannot_start_direct_diagnosis(allows):
    assert not allows(DIAGNOSE, "POST", {"instance_id": 10, "symptom": "blocked"},
                      user_id=7, minimum="operate")
