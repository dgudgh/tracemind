"""Independent C08 task ownership cases through real HTTP authorization and writers."""
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.v1 import report, report_center as report_api
from app.cmdb.models import AssetInstance
from app.core.database import get_db
from app.main import AuthMiddleware
from app.models.access_control import AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.models.user import User
from app.services import access_control, request_scope
from app.services.inspection import jobs, result_writer, run_readback
from test_inspection_plan_adapter import finding
from test_inspection_report_atomic_publication import isolated_report_signing_key  # noqa: F401
from test_inspection_staged_publication import env, claimed, prepared  # noqa: F401


def source_urls(instance_id, task_id, *, hints_only=False):
    urls = [
        f"/api/v1/reports/{task_id}/detail?instance_id={instance_id}",
        f"/api/v1/reports/inspections/{instance_id}/{task_id}",
        f"/api/v1/reports/{task_id}/export?instance_id={instance_id}&format=markdown",
        f"/api/v1/reports/{task_id}/export?instance_id={instance_id}&format=html",
        f"/api/v1/reports/inspections/{instance_id}/{task_id}/export-word",
    ]
    if not hints_only:
        urls.append(f"/api/v1/reports/{task_id}/detail")
    return urls


@pytest.fixture
def source_scope(env, monkeypatch):
    row = claimed(env)
    findings = prepared(env, row)
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=True)
    job_id = row.id
    with env.factory() as db:
        artifact_id = db.query(ReportArtifact).one().id
        reader = User(username="source-task-reader", password_hash="unused", role="user", status="active")
        role = AccessRole(role_key="source-task-reader", name="Reader", status="active")
        foreign = DBInstance(instance_name="other-source", db_type=env.kind, host="fixture.invalid",
                             port=3306, username="fixture", password_cipher="unused", enabled=1)
        db.add_all([reader, role, foreign])
        db.flush()
        reader_id, foreign_id = reader.id, foreign.id
        db.add_all([UserAccessRole(user_id=reader.id, role_id=role.id),
                    RoleActionPermission(role_id=role.id, action_key="report.read"),
                    RoleActionPermission(role_id=role.id, action_key="report.export")])
        assets = []
        for native in (env.instance.id, foreign_id):
            asset = AssetInstance(stable_key=f"source-scope-{native}", name=f"Source {native}",
                                  db_type=env.kind, ip="127.0.0.1", port=3306, host_id=101,
                                  region_id=1, site_id=1, zone_id=1, env_id=1, native_instance_id=native)
            db.add(asset)
            db.flush()
            assets.append(asset.id)
        db.add(UserAssetScope(user_id=reader.id, scope_type="instance", scope_id=assets[0], access_level="read"))
        db.commit()
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(report_api.router, prefix="/api/v1")
    app.include_router(report.router, prefix="/api/v1")

    def dependency():
        with env.factory() as db:
            yield db

    calls = []
    build = report.report_builder.build_report_payload

    def observed_build(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return build(*args, **kwargs)

    app.dependency_overrides[get_db] = dependency
    monkeypatch.setattr("app.core.database.SessionLocal", env.factory)
    monkeypatch.setattr("app.main.get_token_info", lambda token: {
        "source": "db", "user_id": reader_id, "username": "source-task-reader", "role": "user"})
    monkeypatch.setattr(report.report_builder, "build_report_payload", observed_build)
    with TestClient(app) as client:
        yield SimpleNamespace(env=env, factory=env.factory, app=app, client=client, calls=calls,
                              headers={"Authorization": "Bearer explicit-offline-fixture"},
                              reader_id=reader_id, instance_id=env.instance.id, foreign_id=foreign_id,
                              foreign_asset_id=assets[1], job_id=job_id, artifact_id=artifact_id)


def grant_both(state):
    with state.factory() as db:
        db.add(UserAssetScope(user_id=state.reader_id, scope_type="instance",
                              scope_id=state.foreign_asset_id, access_level="read"))
        db.commit()
        scope = access_control.resolve_scope(db, state.reader_id)
        assert scope.restricted and scope.native_instance_ids == {state.instance_id, state.foreign_id}


def save_legacy_rows(state, task_id, instance_id):
    with state.factory() as db:
        target = db.get(DBInstance, instance_id)
        rows = result_writer.save_findings(db, target, task_id, [finding(state.env.kind)])
        assert rows and all(row.instance_id == instance_id for row in rows)


def check_http(state, urls, expected):
    initial_calls = len(state.calls)
    responses = []
    for url in urls:
        parsed = urlsplit(url)
        probe = Request({"type": "http", "method": "GET", "path": parsed.path,
                         "query_string": parsed.query.encode(), "headers": [], "app": state.app})
        probe.state.auth = {"source": "db", "user_id": state.reader_id, "role": "user"}
        with state.factory() as db:
            required = access_control.action_for_request("GET", parsed.path)
            assert access_control.is_action_allowed(db, state.reader_id, required) == (True, True)
            assert request_scope.scope_allows_request_sync(db, state.reader_id, probe, b"") is (expected != 403)
        response = state.client.get(url, headers=state.headers)
        assert response.status_code == expected, (url, response.status_code, response.text)
        responses.append(response)
    if expected == 403:
        assert len(state.calls) == initial_calls, "Rejected resource must never reach the report builder"
    else:
        assert len(state.calls) == initial_calls + len(urls)
    return responses


def test_legacy_without_job_uses_actual_result_owner_and_still_denies_foreign_hint(source_scope):
    state = source_scope
    task_id, foreign_task = "legacy-source-local", "legacy-source-foreign"
    save_legacy_rows(state, task_id, state.instance_id)
    save_legacy_rows(state, foreign_task, state.foreign_id)
    with state.factory() as db:
        assert db.get(DBInspectionJob, task_id) is db.get(DBInspectionJob, foreign_task) is None
        assert {value[0] for value in db.query(DBCheckResult.instance_id).filter_by(task_id=task_id).distinct()} == {state.instance_id}
    responses = check_http(state, source_urls(state.instance_id, task_id), 200)
    assert responses[0].json()["task_id"] == task_id
    assert responses[1].json()["task"]["instance_id"] == state.instance_id
    check_http(state, source_urls(state.instance_id, foreign_task), 403)
    with state.factory() as db:
        assert db.query(ReportArtifact).count() == 1
        assert db.query(ReportArtifact).filter_by(source_id=task_id).first() is None


@pytest.mark.parametrize("terminal", ["queued", "cancelled", "force_closed"])
def test_real_zero_result_job_is_authorized_but_report_handler_returns_404_without_artifact(source_scope, terminal):
    state = source_scope
    with state.factory() as db:
        row = jobs.enqueue(db, db.get(DBInstance, state.instance_id), state.env.kind,
                           state.env.policy, requested_by=state.env.actor.id)
        task_id = row.id
        if terminal == "cancelled":
            jobs.cancel(db, row, requested_by=state.env.actor.id)
            assert row.status == "cancelled" and jobs.failure_code(row) == "cancelled_before_run"
        elif terminal == "force_closed":
            assert jobs.claim(db, task_id, "zero-result-owner")
            jobs.cancel(db, row, requested_by=state.env.actor.id,
                        now=jobs.utcnow() - timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1))
            assert jobs.reap_overdue(db) == 1
            db.refresh(row)
            assert jobs.failure_code(row) == "cancelled_force_closed"
        assert db.query(DBCheckResult).filter_by(task_id=task_id).count() == 0
    responses = check_http(state, source_urls(state.instance_id, task_id), 404)
    assert all(response.json()["detail"] == "巡检记录不存在" for response in responses)
    with state.factory() as db:
        read = run_readback.build_run_read(db, task_id)
        assert read["report"] is None and read["reports"]["artifacts"] == []
        assert db.query(ReportArtifact).count() == 1
        assert db.query(ReportArtifact).filter_by(source_id=task_id).first() is None


@pytest.mark.parametrize("legacy", [False, True])
def test_wrong_instance_hint_is_rejected_even_with_both_native_scopes(source_scope, legacy):
    state = source_scope
    task_id = "legacy-dual-scope" if legacy else state.job_id
    if legacy:
        save_legacy_rows(state, task_id, state.instance_id)
    grant_both(state)
    check_http(state, source_urls(state.instance_id, task_id), 200)
    bad_urls = source_urls(state.foreign_id, task_id, hints_only=True)
    bad_urls += [
        f"/api/v1/reports/{task_id}/detail?instance_id={state.instance_id}&instance_id={state.foreign_id}",
        f"/api/v1/reports/{task_id}/detail?instance_id={state.foreign_id}&instance_id={state.instance_id}",
        f"/api/v1/reports/inspections/{state.instance_id}/{task_id}?instance_id={state.foreign_id}",
    ]
    check_http(state, bad_urls, 403)


@pytest.mark.parametrize("both_scopes", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_mixed_owner_results_cannot_be_hidden_by_an_allowed_instance_hint(source_scope, both_scopes, legacy):
    state = source_scope
    task_id = "legacy-mixed-source" if legacy else state.job_id
    if legacy:
        save_legacy_rows(state, task_id, state.instance_id)
    save_legacy_rows(state, task_id, state.foreign_id)
    if both_scopes:
        grant_both(state)
    with state.factory() as db:
        assert {value[0] for value in db.query(DBCheckResult.instance_id).filter_by(task_id=task_id).distinct()} == {
            state.instance_id, state.foreign_id}
        if legacy:
            assert db.get(DBInspectionJob, task_id) is None
        else:
            assert db.get(DBInspectionJob, task_id).instance_id == state.instance_id
    check_http(state, source_urls(state.instance_id, task_id, hints_only=legacy), 403)
    with state.factory() as db:
        assert db.query(ReportArtifact).count() == 1
        assert db.get(ReportArtifact, state.artifact_id).source_id == state.job_id
