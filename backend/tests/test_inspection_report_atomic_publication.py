"""C08: real job/results/index publication, with explicit offline collector fixtures."""
from datetime import timedelta
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from starlette.requests import Request

from app.api.v1 import report, report_center as report_api
from app.cmdb.models import AssetInstance
from app.core.database import get_db
from app.core import jwt_keys
from app.core.config import settings
from app.main import AuthMiddleware
from app.models.access_control import AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope
from app.models.capability_plan import DBCapabilityPlan
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.report_center import ReportArtifact
from app.models.slow_sql import DBSlowSQL
from app.models.user import User
from app.services import access_control, plan_queue, plan_service as plans, report_center, request_scope
from app.services.inspection import jobs, plan_adapter, report_artifacts, run_readback
from test_inspection_plan_adapter import env as adapter_env  # noqa: F401
from test_inspection_plan_publication import accepted
from test_inspection_plan_workflow import prepare, admit
from test_inspection_staged_publication import env, claimed, prepared  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_report_signing_key(tmp_path, monkeypatch):
    path = tmp_path / "report-signing-key.pem"
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(path))
    assert settings.jwt_private_key_path == path.resolve()
    # Exercise the real key loader, signer and verifier, never the platform key.
    key = jwt_keys.signing_key()
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    yield path, key
    assert settings.jwt_private_key_path.is_relative_to(tmp_path.resolve())


def no_publication(db):
    assert db.query(DBSlowSQL).count() == db.query(DBCheckResult).count() == db.query(ReportArtifact).count() == 0


def test_native_plan_publication_is_atomic_signed_scoped_and_has_real_artifact_id(env, monkeypatch):
    env.instance.tenant_id, env.instance.organization_id = 101, 202
    env.db.commit()
    actor, plan, _receipt = accepted(env, monkeypatch)
    original, observations = report_center.sync_source_artifacts, []

    def observe_sync(db, items, **kwargs):
        assert kwargs["commit"] is False and len(items) == 1
        rows = original(db, items, **kwargs)
        assert rows[0].id is not None
        assert db.query(DBSlowSQL).count() == 2 and db.query(DBCheckResult).count() > 0
        with env.factory() as outside:
            no_publication(outside)
            assert outside.get(DBInspectionJob, rows[0].source_id).status == "running"
        observations.append(rows[0].id)
        return rows

    monkeypatch.setattr(report_center, "sync_source_artifacts", observe_sync)
    finished = plan_queue.process_one(env.factory, worker_id="atomic-report-index")
    assert finished["outcome"] == "succeeded" and finished["queue_state"] == "archived", finished
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        inline = saved["steps"][0]["result_ref"]["inline"]
        artifact = verify.query(ReportArtifact).one()
        assert inline["report"] == {"available": True, "state": "synced", "artifacts": [
            {"id": artifact.id, "status": "success", "archive_state": "active"}]}
        assert observations == [artifact.id]
        assert artifact.source_key == f"source:inspection:{inline['job_id']}"
        assert artifact.source_id == inline["job_id"] and artifact.instance_id == env.instance.id
        assert jobs.last_terminal_event(verify.get(DBInspectionJob, inline["job_id"]))["report_artifact_id"] == artifact.id
        assert (artifact.tenant_id, artifact.organization_id) == (101, 202)
        assert artifact.retention_until > report_center.utcnow() + timedelta(days=364)
        assert report_center.verify_artifact(artifact)["valid"]
        assert artifact.content_markdown and artifact.content_html and artifact.signature
        assert artifact.content_json == report._collect_unified_reports(verify, "inspection", 5)[0]
        assert artifact.content_json == report_artifacts.source_item(run_readback.run_summary(
            verify, verify.get(DBInspectionJob, inline["job_id"]),
            result_counts={inline["job_id"]: inline["run"]["result_count"]}))
        read = run_readback.build_run_read(verify, inline["job_id"])
        assert read["reports"]["artifacts"][0]["id"] == artifact.id
        evidence = next(item["raw_data"] for item in read["results"]["items"]
                        if item["metric_name"].endswith(".slow_sql.persistence"))
        assert evidence["count"] == 2
        assert set(evidence["record_ids"]) == {row.id for row in verify.query(DBSlowSQL).all()}
        assert read["report"]["report"]["analysis_source"] == "rule_engine"
        replay = plan_adapter.execute(SimpleNamespace(
            db=verify, actor_id=actor["user_id"], instance=verify.get(type(env.instance), env.instance.id),
            params=verify.get(DBCapabilityPlan, plan["plan_id"]).capability_snapshot_json["steps"][0]["params"],
            execution_id=f"{plan['plan_id']}:1:1", execution_guard=lambda db: True,
        ))
        assert replay["report"] == inline["report"] and verify.query(ReportArtifact).count() == 1
        assert len(json.dumps(inline).encode()) < 8192
        assert "markdown" not in json.dumps(inline) and "export_url" not in json.dumps(inline)
    assert env.source_calls == [env.kind]


def test_manual_catalog_sync_preserves_existing_signed_history_and_retention(env, monkeypatch):
    row = claimed(env)
    findings = prepared(env, row)
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=True)
    artifact = env.db.query(ReportArtifact).one()
    before = {
        name: getattr(artifact, name) for name in (
            "id", "signature", "signing_key_id", "signed_at", "retention_until",
            "created_by", "content_json", "content_markdown", "content_html", "extra_json",
        )
    }
    assert report_center.verify_artifact(artifact)["valid"]

    def unexpected_rewrite(*args, **kwargs):
        pytest.fail("Unchanged source must retain its historical signed representation")

    monkeypatch.setattr(report_center, "_source_markdown", unexpected_rewrite)
    monkeypatch.setattr(report_center, "sign_artifact", unexpected_rewrite)
    for _ in range(2):
        rows = report_api._collect_and_sync(env.db, "later-manual-sync", "inspection", 5, {env.instance.id})
        assert len(rows) == 1 and rows[0].id == before["id"]
        env.db.expire_all()
        stored = env.db.get(ReportArtifact, before["id"])
        assert {name: getattr(stored, name) for name in before} == before
        assert report_center.verify_artifact(stored)["valid"]
        assert env.db.query(ReportArtifact).count() == 1


@pytest.mark.parametrize("failure", ["render", "sign", "insert", "after_flush"])
def test_report_failure_rolls_back_slow_sql_results_terminal_job_and_allocated_artifact(
        env, monkeypatch, failure, isolated_report_signing_key):
    actor, plan, _receipt = accepted(env, monkeypatch)
    allocated = []

    def fail(*args, **kwargs):
        raise RuntimeError("fixture report publication failure")

    def after_flush(db, _context):
        rows = [row for row in db.identity_map.values() if isinstance(row, ReportArtifact)]
        if rows:
            allocated.extend(row.id for row in rows)
            fail()

    if failure == "render":
        monkeypatch.setattr(report_center, "_source_markdown", fail)
    elif failure == "sign":
        path, _key = isolated_report_signing_key
        path.write_bytes(b"fixture corrupted private key")
    elif failure == "insert":
        event.listen(ReportArtifact, "before_insert", fail)
    else:
        event.listen(env.factory.class_, "after_flush_postexec", after_flush)
    try:
        finished = plan_queue.process_one(env.factory, worker_id="report-index-failure")
    finally:
        if failure == "insert":
            event.remove(ReportArtifact, "before_insert", fail)
        elif failure == "after_flush":
            event.remove(env.factory.class_, "after_flush_postexec", after_flush)
    assert finished["outcome"] == "failed", finished
    with env.factory() as verify:
        no_publication(verify)
        row = verify.query(DBInspectionJob).one()
        assert row.status == "failed" and jobs.failure_code(row) == "inspection_report_index_failed"
        assert "report_artifact_id" not in jobs.last_terminal_event(row)
        read = run_readback.build_run_read(verify, row.id)
        assert read["results"]["total_count"] == 0 and read["report"] is None
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert saved["steps"][0]["result_ref"]["inline"]["error_code"] == "inspection_report_index_failed"
        assert saved["steps"][0]["result_ref"]["inline"]["report"]["artifacts"] == []
        if failure == "after_flush":
            assert allocated and all(verify.get(ReportArtifact, identity) is None for identity in allocated)


def test_final_guard_refusal_after_artifact_flush_rolls_back_every_publication(env, monkeypatch):
    allowed = [True]
    original, ids = report_center.sync_source_artifacts, []

    def revoke_after_flush(db, items, **kwargs):
        rows = original(db, items, **kwargs)
        ids.extend(row.id for row in rows)
        allowed[0] = False
        return rows

    monkeypatch.setattr(report_center, "sync_source_artifacts", revoke_after_flush)
    ctx = SimpleNamespace(
        db=env.db, actor_id=env.actor.id, instance=env.instance,
        params=plan_adapter.prepare_params(env.db, env.instance, {"policy_id": env.policy.id}),
        execution_id="report-guard:1:1", execution_guard=lambda db: allowed[0],
    )
    with pytest.raises(plan_adapter.InspectionPlanError, match="inspection_execution_guard_rejected"):
        plan_adapter.execute(ctx)
    with env.factory() as verify:
        no_publication(verify)
        row = verify.query(DBInspectionJob).one()
        assert row.status == "running" and row.finished_at is None
        assert ids and all(verify.get(ReportArtifact, identity) is None for identity in ids)


def test_real_queue_commit_fence_covers_artifact_and_cannot_publish_a_lost_owners_failure(env, monkeypatch):
    _actor, plan, _receipt = accepted(env, monkeypatch)
    guards, ids = [], []
    initialize = plan_queue.ExecutionGuard.__init__
    original = report_center.sync_source_artifacts

    def initialized(guard, claim, db):
        initialize(guard, claim, db)
        guards.append(guard)

    def lose_token_after_flush(db, items, **kwargs):
        rows = original(db, items, **kwargs)
        ids.extend(row.id for row in rows)
        guards[-1].claim["owner_token"] = "stale-owner-claim"
        return rows

    monkeypatch.setattr(plan_queue.ExecutionGuard, "__init__", initialized)
    monkeypatch.setattr(report_center, "sync_source_artifacts", lose_token_after_flush)
    finished = plan_queue.process_one(env.factory, worker_id="lost-parent-report-owner")
    assert finished["queue_state"] == "ownership_lost" and finished["requires_attention"], finished
    with env.factory() as verify:
        no_publication(verify)
        job = verify.query(DBInspectionJob).one()
        saved = verify.get(DBCapabilityPlan, plan["plan_id"])
        assert job.status == saved.status == "running" and saved.outcome is None
        assert job.finished_at is None and saved.finished_at is None
        assert ids and all(verify.get(ReportArtifact, identity) is None for identity in ids)


def test_explicit_index_opt_out_stays_compatible_and_commit_false_never_commits(env):
    row = claimed(env)
    findings = prepared(env, row)
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=False)
    assert env.db.query(ReportArtifact).count() == 0
    source = report_artifacts.source_item(run_readback.run_summary(
        env.db, row, result_counts={row.id: env.db.query(DBCheckResult).count()}))
    commits = []
    event.listen(env.db, "after_commit", lambda db: commits.append(True))
    artifact = report_center.sync_source_artifacts(env.db, [source], commit=False)[0]
    identity = artifact.id
    assert identity is not None and commits == []
    env.db.rollback()
    with env.factory() as verify:
        assert verify.get(ReportArtifact, identity) is None
        assert verify.get(DBInspectionJob, row.id).status == "success"
        assert verify.query(DBSlowSQL).count() == 2 and verify.query(DBCheckResult).count() > 0
    artifact = report_center.sync_source_artifacts(env.db, [source])[0]
    assert artifact.id is not None and commits == [True]


def test_job_commit_can_explicitly_publish_the_same_atomic_index_without_adapter(env):
    row = claimed(env)
    findings = prepared(env, row)
    assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=True)
    with env.factory() as verify:
        artifact = verify.query(ReportArtifact).one()
        read = run_readback.build_run_read(verify, row.id)
        assert artifact.source_id == row.id and artifact.status == read["run"]["outcome"] == "success"
        assert jobs.last_terminal_event(verify.get(DBInspectionJob, row.id))["report_artifact_id"] == artifact.id
        assert read["reports"]["artifacts"][0]["id"] == artifact.id
        assert verify.query(DBSlowSQL).count() == 2 and report_center.verify_artifact(artifact)["valid"]


@pytest.mark.parametrize("refuse", [False, True])
def test_existing_index_reuses_real_id_preserves_lifecycle_and_rolls_back_updates(env, monkeypatch, refuse):
    row = claimed(env)
    source = report_artifacts.source_item(run_readback.run_summary(env.db, row, result_counts={}))
    artifact = report_center.sync_source_artifacts(env.db, [source])[0]
    report_center.archive_artifact(env.db, artifact, actor="fixture", archive_uri="/private/fixture-index-only")
    identity, signature, retention = artifact.id, artifact.signature, artifact.retention_until
    findings = prepared(env, row)
    original = report_center.sync_source_artifacts

    def synchronized(db, items, **kwargs):
        rows = original(db, items, **kwargs)
        assert rows[0].id == identity
        if refuse:
            raise RuntimeError("fixture failure after updating existing index")
        return rows

    monkeypatch.setattr(report_center, "sync_source_artifacts", synchronized)
    if refuse:
        with pytest.raises(report_artifacts.ReportIndexPublicationError):
            jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=True)
    else:
        assert jobs.commit_results(env.db, row.id, "owner", findings, sync_report_artifact=True)
    with env.factory() as verify:
        saved = verify.query(ReportArtifact).one()
        assert saved.id == identity and saved.archive_state == "archived" and saved.retention_until == retention
        assert report_center.verify_artifact(saved)["valid"]
        assert saved.status == ("unknown" if refuse else "success")
        if refuse:
            assert saved.signature == signature
            assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
            assert verify.get(DBInspectionJob, row.id).status == "running"
        else:
            value = plan_adapter._result(verify, verify.get(DBInspectionJob, row.id))
            assert value["report"]["artifacts"] == [{"id": identity, "status": "success", "archive_state": "archived"}]
            assert "fixture-index-only" not in json.dumps(value)


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
@pytest.mark.parametrize("statuses,outcome", [(["success"], "success"), (["success", "failed"], "partial"),
                                             (["failed"], "failed")])
def test_four_engine_result_outcomes_match_real_atomic_artifact(adapter_env, monkeypatch, engine, statuses, outcome):
    env = adapter_env
    env.result_statuses = statuses
    actor, reviewer, plan = prepare(env, monkeypatch, engine=engine)
    admit(env, actor, reviewer, plan)
    finished = plan_queue.process_one(env.factory, worker_id="four-engine-index")
    assert finished["outcome"] == ("succeeded" if outcome == "success" else outcome)
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        artifact = verify.query(ReportArtifact).one()
        assert artifact.status == outcome and artifact.content_json["status"] == outcome
        assert artifact.score is None if outcome != "success" else artifact.score is not None
        assert saved["steps"][0]["result_ref"]["inline"]["report"]["artifacts"][0]["id"] == artifact.id
        read = run_readback.build_run_read(verify, artifact.source_id)
        assert read["report"]["task"]["task_status"] == read["run"]["outcome"] == outcome
        assert report_center.verify_artifact(artifact)["valid"]


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
@pytest.mark.parametrize("termination", ["cancelled", "force_closed", "interrupted", "timeout"])
def test_cancelled_and_uncertain_without_normal_report_do_not_create_artifact(adapter_env, monkeypatch,
                                                                           engine, termination):
    env = adapter_env
    actor, reviewer, plan = prepare(env, monkeypatch, engine=engine)
    admit(env, actor, reviewer, plan)

    def terminate():
        with env.factory() as control:
            row = control.query(DBInspectionJob).filter_by(status="running").one()
            if termination in {"cancelled", "force_closed"}:
                jobs.cancel(control, row, requested_by=actor["user_id"],
                            now=jobs.utcnow() - timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1))
            if termination == "force_closed":
                assert jobs.reap_overdue(control) == 1
            elif termination == "interrupted":
                assert jobs.reap_interrupted(control, now=row.lease_until + timedelta(seconds=1)) == 1
            elif termination == "timeout":
                jobs.finish_error(control, row.id, row.owner_token, "timed_out", "fixture timeout", code="timeout")

    env.action = terminate
    finished = plan_queue.process_one(env.factory, worker_id="no-invented-report")
    assert finished["outcome"] == ("cancelled" if termination == "cancelled" else "unknown"), finished
    assert finished["queue_state"] == ("archived" if termination == "cancelled" else "reconciliation_required")
    with env.factory() as verify:
        assert verify.query(ReportArtifact).count() == verify.query(DBCheckResult).count() == 0
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert saved["steps"][0]["result_ref"]["inline"]["report"]["available"] is False
        row = verify.query(DBInspectionJob).one()
        read = run_readback.build_run_read(verify, row.id)
        assert read["report"] is None and read["reports"]["state"] == "not_synced"
        assert read["reports"]["artifacts"] == []


@pytest.mark.parametrize("view", ["artifact", "source"])
def test_report_detail_exports_and_real_auth_scope_apply_to_atomic_artifact(env, monkeypatch, view, request):
    _actor, _plan, _receipt = accepted(env, monkeypatch)
    assert plan_queue.process_one(env.factory, worker_id="scope-index")["outcome"] == "succeeded"
    with env.factory() as db:
        artifact = db.query(ReportArtifact).one()
        artifact_id, job_id = artifact.id, artifact.source_id
        user = User(username="report-scope-reader", password_hash="unused", role="user", status="active")
        role = AccessRole(role_key="report-scope-reader", name="Reader", status="active")
        db.add_all([user, role])
        db.flush()
        user_id = user.id
        db.add_all([UserAccessRole(user_id=user.id, role_id=role.id),
                    RoleActionPermission(role_id=role.id, action_key="report.read"),
                    RoleActionPermission(role_id=role.id, action_key="report.export")])
        asset = AssetInstance(stable_key="report-source", name="Source", db_type=env.kind, ip="127.0.0.1", port=3306,
                              host_id=101, region_id=1, site_id=1, zone_id=1, env_id=1,
                              native_instance_id=env.instance.id)
        db.add(asset)
        db.flush()
        scope = UserAssetScope(user_id=user.id, scope_type="instance", scope_id=asset.id, access_level="read")
        db.add(scope)
        foreign = DBInstance(instance_name="foreign-report-source", db_type=env.kind,
                             host="fixture.invalid", port=3306, username="fixture",
                             password_cipher="unused", enabled=1)
        db.add(foreign)
        db.commit()
        foreign_id, role_id = foreign.id, role.id
        foreign_job_id = jobs.enqueue(db, foreign, env.kind, env.policy, requested_by=env.actor.id).id
        asset_id = asset.id
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(report_api.router, prefix="/api/v1")
    app.include_router(report.router, prefix="/api/v1")

    def dependency():
        with env.factory() as db:
            yield db

    app.dependency_overrides[get_db] = dependency
    monkeypatch.setattr("app.core.database.SessionLocal", env.factory)
    monkeypatch.setattr("app.main.get_token_info", lambda token: {
        "source": "db", "user_id": user_id, "username": "report-scope-reader", "role": "user"})
    headers = {"Authorization": "Bearer explicit-fixture"}
    source_paths = [
        (f"/api/v1/reports/{job_id}/detail?instance_id={env.instance.id}", "report.read", "detail"),
        (f"/api/v1/reports/{job_id}/detail", "report.read", "detail"),
        (f"/api/v1/reports/inspections/{env.instance.id}/{job_id}", "report.read", "payload"),
        (f"/api/v1/reports/{job_id}/export?instance_id={env.instance.id}&format=markdown", "report.export", "markdown"),
        (f"/api/v1/reports/{job_id}/export?instance_id={env.instance.id}&format=html", "report.export", "html"),
        (f"/api/v1/reports/inspections/{env.instance.id}/{job_id}/export-word", "report.export", "word"),
    ]
    proofs = []
    with env.factory() as db:
        assert access_control.effective_action_keys(db, user_id) == (True, {"report.read", "report.export"})
        actual_scope = access_control.resolve_scope(db, user_id)
        assert actual_scope.restricted and actual_scope.native_instance_ids == {env.instance.id}
        assert actual_scope.normalized_instance_ids == {asset_id} and foreign_id not in actual_scope.native_instance_ids
        if view == "source":
            for path, action, _format in source_paths:
                parsed = urlsplit(path)
                assert access_control.action_for_request("GET", parsed.path) == action
                assert access_control.is_action_allowed(db, user_id, action) == (True, True)
                probe = Request({"type": "http", "method": "GET", "path": parsed.path,
                                 "query_string": parsed.query.encode(), "headers": [], "app": app})
                probe.state.auth = {"source": "db", "user_id": user_id, "role": "user"}
                matched = request_scope._matched_route(probe)
                assert matched is not None and matched[1]["task_id"] == job_id
                template, params = matched
                rule = request_scope._rule(template, "GET", params)
                targets = request_scope._Targets(db, actual_scope, template, False)
                try:
                    targets.reference("task_id", job_id)
                    resolution = "resolved"
                except ValueError as exc:
                    resolution = str(exc)
                proofs.append({"url": path, "action": action, "action_allowed": True,
                               "native_scope": sorted(actual_scope.native_instance_ids), "matched": template,
                               "rule": None if rule is None else rule.location,
                               "task_resolution": resolution,
                               "scope_allowed": request_scope.scope_allows_request_sync(db, user_id, probe, b"")})
    request.node.user_properties.append(("source_scope_proof", json.dumps(proofs)))
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/reports/catalog/{artifact_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["verification"]["valid"]
        assert detail.json()["source_id"] == job_id
        for format in ("markdown", "html", "json", "word"):
            exported = client.get(f"/api/v1/reports/catalog/{artifact_id}/download", params={"format": format}, headers=headers)
            assert exported.status_code == 200 and exported.content, exported.text
            assert exported.headers["x-report-sha256"]
            if format == "word":
                with ZipFile(io.BytesIO(exported.content)) as docx:
                    assert docx.testzip() is None and "word/document.xml" in docx.namelist()
        paths = [f"/api/v1/reports/catalog/{artifact_id}", f"/api/v1/reports/catalog/{artifact_id}/download"]
        allowed_responses = []
        if view == "source":
            for path, _action, format in source_paths:
                response = client.get(path, headers=headers)
                allowed_responses.append((path, format, response))
            paths += [path for path, _action, _format in source_paths]
            for foreign_path in (
                f"/api/v1/reports/{foreign_job_id}/detail?instance_id={env.instance.id}",
                f"/api/v1/reports/{foreign_job_id}/export?instance_id={env.instance.id}",
                f"/api/v1/reports/inspections/{env.instance.id}/{foreign_job_id}/export-word",
                f"/api/v1/reports/{job_id}/detail?instance_id={foreign_id}",
                f"/api/v1/reports/inspections/{foreign_id}/{job_id}/export-word",
                f"/api/v1/reports/missing-job/detail?instance_id={env.instance.id}",
            ):
                assert client.get(foreign_path, headers=headers).status_code == 403, foreign_path
        with env.factory() as db:
            db.query(RoleActionPermission).filter_by(role_id=role_id, action_key="report.export").delete()
            db.commit()
        for path in [f"/api/v1/reports/catalog/{artifact_id}/download", *(
                path for path, action, _format in source_paths if view == "source" and action == "report.export")]:
            denied = client.get(path, headers=headers)
            assert denied.status_code == 403 and denied.json()["required_action"] == "report.export", denied.text
        assert client.get(f"/api/v1/reports/catalog/{artifact_id}", headers=headers).status_code == 200
        with env.factory() as db:
            db.add(RoleActionPermission(role_id=role_id, action_key="report.export"))
            db.query(UserAssetScope).filter_by(user_id=user_id).update({"scope_id": 999999})
            db.commit()
        for path in paths:
            assert client.get(path, headers=headers).status_code == 403, path
        statuses = {path: response.status_code for path, _format, response in allowed_responses}
        request.node.user_properties.append(("source_http_statuses", json.dumps(statuses)))
        assert all(code == 200 for code in statuses.values()), {"statuses": statuses, "proofs": proofs}
        for path, format, response in allowed_responses:
            if format == "word":
                with ZipFile(io.BytesIO(response.content)) as docx:
                    assert docx.testzip() is None and job_id.encode() in docx.read("word/document.xml")
            elif format == "payload":
                assert response.json()["task"]["task_id"] == job_id
            else:
                assert job_id in response.text, path


def test_existing_full_source_report_builders_keep_real_payload_and_exports(env, monkeypatch):
    _actor, _plan, _receipt = accepted(env, monkeypatch)
    assert plan_queue.process_one(env.factory, worker_id="source-rendering")["outcome"] == "succeeded"
    with env.factory() as db:
        artifact = db.query(ReportArtifact).one()
        job_id = artifact.source_id
        payload = report.api_inspection_detail(env.instance.id, job_id, db=db)
        assert payload["task"]["task_id"] == job_id and payload["findings"]
        assert payload["report"]["analysis_source"] == "rule_engine"
        detail = report.api_inspection_structured_detail(job_id, instance_id=env.instance.id, db=db)
        assert detail["raw_payload"] == payload and job_id in detail["markdown"]
        assert job_id in report._build_inspection_html(detail)
        persistence = next(item["raw_data"]["raw_data"] for item in payload["findings"]
                           if item["raw_data"]["metric_name"].endswith(".slow_sql.persistence"))
        assert set(persistence["record_ids"]) == {row.id for row in db.query(DBSlowSQL).all()}
        with ZipFile(report._build_inspection_docx(payload)) as docx:
            assert docx.testzip() is None and job_id.encode() in docx.read("word/document.xml")
