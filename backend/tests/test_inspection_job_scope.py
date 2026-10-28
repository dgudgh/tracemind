"""Scope and permission gates for the four new inspection job routes.

Asset scope is fail-closed: a path nobody registered is denied, so a new endpoint is either
explicitly reviewed or unusable by restricted accounts. That makes registration a code change,
not a documentation change - and it means the ownership resolver must know what owns a run.

The ownership question is the subtle one: a run is owned by its **job row**. Resolving it
through ``DBCheckResult`` would judge every zero-result run "resource does not exist" and lock
the very operator who has to look at the failure out of the page that reports it.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers the whole model metadata
from app.cmdb.models import AssetInstance
from app.main import AuthMiddleware
from app.models.access_control import (
    AccessRole,
    RoleActionPermission,
    UserAccessRole,
    UserAssetScope,
)
from app.models.check_result import DBCheckResult
from app.models.inspection_job import DBInspectionJob
from app.services.access_control import (
    action_catalog,
    action_for_request,
    ensure_builtin_roles,
)

JOB_ID_ALLOWED = "job-a"
JOB_ID_FOREIGN = "job-b"
NEW_ROUTES = (
    ("GET", "/api/v1/inspection/jobs"),
    ("GET", "/api/v1/inspection/jobs/{job_id}"),
    ("POST", "/api/v1/inspection/jobs/{job_id}/cancel"),
    ("POST", "/api/v1/inspection/jobs/{job_id}/retry"),
)


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    for table in (
        AccessRole.__table__, RoleActionPermission.__table__, UserAccessRole.__table__,
        UserAssetScope.__table__, AssetInstance.__table__, DBCheckResult.__table__,
        DBInspectionJob.__table__,
    ):
        table.create(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    roles = {role.role_key: role for role in ensure_builtin_roles(db)}
    # Asset 11 is the normalised view of native instance 101; asset 12 of 202.
    for asset, native in ((11, 101), (12, 202)):
        db.add(AssetInstance(
            id=asset, native_instance_id=native, stable_key=f"asset:{asset}",
            name=f"asset-{asset}", db_type="mysql", ip="127.0.0.1", port=3306,
            host_id=20 + asset, cluster_id=30 + asset,
            region_id=1, site_id=1, zone_id=1, env_id=1,
        ))
    db.add(UserAssetScope(user_id=7, scope_type="instance", scope_id=11, access_level="operate"))
    for job_id, native in ((JOB_ID_ALLOWED, 101), (JOB_ID_FOREIGN, 202)):
        # Deliberately no DBCheckResult rows: a zero-result run is exactly the case that used to
        # be judged "does not exist".
        db.add(DBInspectionJob(
            id=job_id, instance_id=native, db_type="mysql", requested_by=7,
            trigger_type="manual", policy_id=1,
            policy_snapshot_json={"policy_key": "p", "name": "p"}, policy_revision="0" * 64,
            target_fingerprint="0" * 64, attempt=1, status="failed", stage="failed",
            progress_json=[], timeout_seconds=900, cancel_requested=0,
            created_at=datetime(2026, 10, 20, 10, 0, 0),
        ))
    db.commit()
    yield SimpleNamespace(
        engine=engine, db=db, SessionLocal=SessionLocal, roles=roles, paths=[],
    )
    db.close()
    engine.dispose()


def _assign(env, role_key: str) -> None:
    env.db.query(UserAccessRole).delete()
    env.db.add(UserAccessRole(user_id=7, role_id=env.roles[role_key].id))
    env.db.commit()


def _middleware_client(env, info: dict) -> TestClient:
    env.paths.clear()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    async def endpoint(request: Request):
        env.paths.append(request.url.path)
        return {"ok": True}

    for path in (
        "/api/v1/inspection/jobs", "/api/v1/inspection/jobs/{job_id}",
        "/api/v1/inspection/jobs/{job_id}/cancel", "/api/v1/inspection/jobs/{job_id}/retry",
    ):
        app.add_api_route(path, endpoint, methods=["GET", "POST"])

    client = TestClient(app)
    patchers = [
        patch("app.main.get_token_info", side_effect=lambda token: dict(info)),
        patch("app.core.database.SessionLocal", env.SessionLocal),
    ]
    for patcher in patchers:
        patcher.start()
    client.patchers = patchers
    return client


def _call(env, info, method: str, path: str):
    """One request through the real AuthMiddleware, with the paths it actually reached."""
    client = _middleware_client(env, info)
    try:
        response = client.request(
            method, path, headers={"Authorization": "Bearer test-session"},
        )
        response.reached = list(env.paths)
        return response
    finally:
        client.close()
        for patcher in client.patchers:
            patcher.stop()


RESTRICTED_USER = {"source": "db", "user_id": 7, "role": "user", "username": "op"}


def test_job_routes_map_to_known_permissions():
    known = {item["action_key"] for item in action_catalog()}
    for method, path in NEW_ROUTES:
        action = action_for_request(method, path)
        assert action in known, f"{method} {path} has no known permission"
    # Reading a run and controlling one are deliberately different actions.
    assert action_for_request("GET", "/api/v1/inspection/jobs") == "inspection.read"
    assert action_for_request("GET", "/api/v1/inspection/jobs/job-a") == "inspection.read"
    assert action_for_request("POST", "/api/v1/inspection/jobs/job-a/cancel") == "inspection.execute"
    assert action_for_request("POST", "/api/v1/inspection/jobs/job-a/retry") == "inspection.execute"


def test_all_four_routes_are_registered_in_the_application():
    from app.api.v1 import routers

    registered = {
        (method, "/api/v1" + route.path)
        for router in routers for route in router.routes
        for method in route.methods
    }
    missing = [route for route in NEW_ROUTES if route not in registered]
    assert missing == []


def test_reader_without_inspection_read_is_denied_instead_of_shown_an_empty_list(env):
    role = AccessRole(role_key="asset_only", name="Asset only", status="active")
    env.db.add(role)
    env.db.flush()
    env.db.add(RoleActionPermission(role_id=role.id, action_key="asset.read"))
    env.db.add(UserAccessRole(user_id=7, role_id=role.id))
    env.db.commit()

    response = _call(env, RESTRICTED_USER, "GET", "/api/v1/inspection/jobs")

    assert response.status_code == 403, response.text
    assert env.paths == [], "a denied request must never reach the handler"


def test_own_zero_result_run_is_readable_and_a_foreign_run_is_not(env):
    _assign(env, "viewer")

    allowed = _call(env, RESTRICTED_USER, "GET", f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}")
    foreign = _call(env, RESTRICTED_USER, "GET", f"/api/v1/inspection/jobs/{JOB_ID_FOREIGN}")
    missing = _call(env, RESTRICTED_USER, "GET", "/api/v1/inspection/jobs/no-such-job")

    assert allowed.status_code == 200, allowed.text
    assert foreign.status_code == 403
    assert missing.status_code == 403, "a missing run must fail closed, not fall through"
    assert allowed.reached == [f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}"]
    assert foreign.reached == [] and missing.reached == []


def test_cancel_and_retry_are_control_not_read(env):
    _assign(env, "viewer")
    # A read-only role may not control a run, not even its own.
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}/cancel").status_code == 403
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}/retry").status_code == 403

    _assign(env, "dba_operator")
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}/cancel").status_code == 200
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_FOREIGN}/cancel").status_code == 403
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_ALLOWED}/retry").status_code == 200
    assert _call(env, RESTRICTED_USER, "POST", f"/api/v1/inspection/jobs/{JOB_ID_FOREIGN}/retry").status_code == 403


def test_job_collection_is_registered_as_a_filtered_collection(env):
    _assign(env, "viewer")

    allowed = _call(env, RESTRICTED_USER, "GET", "/api/v1/inspection/jobs")
    widened = _call(env, RESTRICTED_USER, "GET", "/api/v1/inspection/jobs?instance_id=202")

    assert allowed.status_code == 200
    assert allowed.reached == ["/api/v1/inspection/jobs"]
    # A query parameter that points outside the grant does not widen it.
    assert widened.status_code == 403
    assert widened.reached == []


def test_handler_filters_the_list_by_scope(env):
    from app.api.v1.inspection import api_get_inspection_job, api_list_inspection_jobs

    _assign(env, "viewer")
    request = SimpleNamespace(
        state=SimpleNamespace(auth=dict(RESTRICTED_USER)), headers={},
    )

    listing = api_list_inspection_jobs(
        request, instance_id=None, db_type=None, status=None, outcome=None,
        trigger_type=None, include_legacy=False, page=1, page_size=50, db=env.db,
    )

    assert [item["job_id"] for item in listing["items"]] == [JOB_ID_ALLOWED]
    assert listing["total"] == 1

    with pytest.raises(Exception) as denied:
        api_get_inspection_job(JOB_ID_FOREIGN, request, db=env.db)
    assert getattr(denied.value, "status_code", None) == 403
