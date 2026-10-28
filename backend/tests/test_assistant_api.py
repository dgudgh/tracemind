"""T04 HTTP boundary: real routing/auth/readers, isolated metadata, no app lifespan."""
from datetime import datetime
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import main
from app.api.v1 import assistant
from app.cmdb.models import AssetInstance
from app.core import auth, database
from app.models.access_control import (
    AccessRole, AuthSession, RoleActionPermission, SecurityAuditEvent, UserAccessRole, UserAssetScope,
)
from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.system_setting import DBSystemSetting
from app.models.user import User
from app.services import access_control, capability_registry as registry
from app.services.capability_declarations import CAPABILITY_DECLARATIONS


PREFIX = "/api/v1/assistant"
QUERY = "inspection.run.query"
TEMPLATE = "inspection.template.read"
MATRIX = "operations.capability_matrix.read"
SECRET = "T04-synthetic-secret-must-not-echo"
MISSING_PLAN = "pl" + "f" * 26
REVIEW_REQUEST = "req-review-123"
MISSING_BATCH = "CB" + "f" * 28
MISSING_EXECUTION = "2147483647"
ASSISTANT_ROUTES = (
    ("GET", "/capabilities"),
    ("GET", "/capabilities/" + QUERY),
    ("POST", "/dispatch-intent"),
    ("GET", "/dispatch-intent/" + REVIEW_REQUEST),
    ("GET", "/dispatch-metrics"),
    ("POST", "/plans"),
    ("GET", "/plans"),
    ("GET", "/plans/" + MISSING_PLAN),
    ("POST", "/plans/" + MISSING_PLAN + "/execute"),
    ("GET", "/plans/" + MISSING_PLAN + "/validation"),
    ("POST", "/plans/" + MISSING_PLAN + "/revisions"),
    ("GET", "/plans/" + MISSING_PLAN + "/approval"),
    ("POST", "/plans/" + MISSING_PLAN + "/approval/request"),
    ("POST", "/plans/" + MISSING_PLAN + "/approval/approve"),
    ("POST", "/plans/" + MISSING_PLAN + "/approval/revoke"),
    ("POST", "/batches/prepare"),
    ("POST", "/batches"),
    ("GET", "/batches"),
    ("GET", "/batches/preparation"),
    ("GET", "/batches/admission"),
    ("GET", "/batches/" + MISSING_BATCH),
    ("POST", "/batches/" + MISSING_BATCH + "/cancel"),
    ("GET", "/executions/" + MISSING_EXECUTION + "/control"),
    ("POST", "/executions/" + MISSING_EXECUTION + "/pause"),
    ("POST", "/executions/" + MISSING_EXECUTION + "/resume"),
    ("POST", "/executions/" + MISSING_EXECUTION + "/stop"),
    ("POST", "/executions/" + MISSING_EXECUTION + "/probe"),
    ("GET", "/workflows"),
    ("GET", "/workflows/preparation"),
    ("GET", "/workflows/providers"),
    ("GET", "/workflows/proposals"),
    ("POST", "/workflows/proposals"),
    ("GET", "/workflows/inspection.jobs.list/versions/1"),
    ("POST", "/workflows/match"),
    ("POST", "/workflows/inspection.jobs.list/versions/1/prepare"),
    ("POST", "/workflows/plans/" + MISSING_PLAN + "/revisions"),
)


@pytest.fixture(scope="module")
def signing_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("assistant-jwt") / "signing.pem"


@pytest.fixture
def api(tmp_path, monkeypatch, signing_key_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'assistant.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    models = (User, AuthSession, AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope,
              SecurityAuditEvent, AssetInstance, DBInstance, DBInspectionJob, DBInspectionPolicy,
              DBCheckResult, DBCapabilityDefinition, DBCapabilityPlan, DBCapabilityPlanRevision,
              DBCapabilityPlanStep, DBCapabilityBatch, DBCapabilityExecution, DBSystemSetting)
    database.Base.metadata.create_all(engine, tables=[model.__table__ for model in models])
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(signing_key_path))
    tokens = {}
    with factory() as db:
        roles = {row.role_key: row.id for row in access_control.ensure_builtin_roles(db)}
        for user_id, name in enumerate(("admin", "reader", "other", "scoped", "ungranted"), 1):
            db.add(User(id=user_id, username="assistant-" + name, password_hash="unused",
                        role="admin" if user_id == 1 else "user", status="active"))
            if user_id in (2, 3, 4):
                db.add(UserAccessRole(user_id=user_id, role_id=roles["viewer"]))
        for native_id, normalized_id in ((10, 110), (20, 120)):
            db.add(DBInstance(id=native_id, instance_name=f"assistant-{native_id}", db_type="mysql",
                              host="127.0.0.1", port=3306, username="unused",
                              password_cipher="env://T04_UNUSED_PASSWORD", enabled=1))
            db.add(AssetInstance(id=normalized_id, native_instance_id=native_id,
                                 stable_key=f"assistant:{native_id}", name=f"assistant-{native_id}",
                                 db_type="mysql", ip="127.0.0.1", port=3306, host_id=normalized_id,
                                 region_id=1, site_id=1, zone_id=1, env_id=1, status="active"))
            db.add(DBInspectionJob(id=f"t04-job-{native_id}", instance_id=native_id, db_type="mysql",
                                   requested_by=1, trigger_type="manual", policy_id=1,
                                   policy_snapshot_json={}, policy_revision="t04", target_fingerprint="t04",
                                   status="queued", stage="queued", created_at=datetime(2026, 10, 20)))
        db.add(UserAssetScope(user_id=4, scope_type="instance", scope_id=110, access_level="read"))
        db.add(DBInspectionPolicy(id=1, policy_key="t04-policy", name="T04 local template",
                                  db_types_json=["mysql"], enabled=1))
        db.commit()
        registry.sync_definitions(db, actor={"user_id": 1})
        for user in db.query(User).all():
            tokens[user.id] = auth.issue_user_token(
                user, source="identity" if user.id == 3 else "db", db=db,
            )["token"]
        assert db.query(AuthSession).count() == 5
        assert db.autoflush is False

    def isolated_db():
        with factory() as db:
            yield db

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.add_middleware(main.AuthMiddleware)
    app.dependency_overrides[database.get_db] = isolated_db
    try:
        with TestClient(app) as client:
            def request(method, path, *, user=2, headers=None, **kwargs):
                incoming = {} if user is None else {"Authorization": "Bearer " + tokens[user]}
                incoming.update(headers or {})
                return client.request(method, PREFIX + path, headers=incoming, **kwargs)

            yield SimpleNamespace(app=app, client=client, request=request, factory=factory,
                                  engine=engine, tokens=tokens, roles=roles)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


_VERSIONS = {entry["capability_id"]: entry["version"] for entry in CAPABILITY_DECLARATIONS}


def payload(capability=QUERY, **kwargs):
    return {"capability_id": capability, "definition_version": _VERSIONS.get(capability, 1), **kwargs}


def create(api, capability=QUERY, *, user=2, **kwargs):
    response = api.request("POST", "/plans", user=user, json=payload(capability, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def assert_rejected(response, status, code=None):
    assert response.status_code == status, response.text
    assert SECRET not in response.text
    if code:
        assert response.json()["detail"] == {"code": code}


def assert_no_plans(api):
    with api.factory() as db:
        for model in (DBCapabilityPlan, DBCapabilityPlanStep, DBCapabilityPlanRevision):
            assert db.query(model).count() == 0


def test_only_reviewed_operations_and_inline_openapi_bodies(api):
    operations = {(method, route.path) for route in api.app.routes for method in route.methods}
    expected = {(method, PREFIX + path.replace(MISSING_PLAN, "{plan_id}").replace(MISSING_BATCH, "{batch_id}")
                 .replace(MISSING_EXECUTION, "{capability_execution_id}")
                 .replace("/workflows/inspection.jobs.list/versions/1", "/workflows/{workflow_id}/versions/{workflow_version}")
                 .replace("/capabilities/" + QUERY, "/capabilities/{capability}")
                 .replace("/dispatch-intent/" + REVIEW_REQUEST, "/dispatch-intent/{request_id}"))
                for method, path in ASSISTANT_ROUTES}
    assert operations == expected
    spec = api.app.openapi()
    create_op = spec["paths"][PREFIX + "/plans"]["post"]
    schema = create_op["requestBody"]["content"]["application/json"]["schema"]
    assert create_op["requestBody"]["required"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"capability_id", "definition_version"}
    assert schema["properties"]["steps"]["anyOf"][0]["maxItems"] == 20
    assert "$ref" not in json.dumps(schema)
    assert {"200", "201", "422"} <= create_op["responses"].keys()
    dispatch_schema = spec["paths"][PREFIX + "/dispatch-intent"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert dispatch_schema["additionalProperties"] is False
    assert set(dispatch_schema["required"]) == {"text"}
    assert dispatch_schema["properties"]["text"]["maxLength"] == 2000
    execute = spec["paths"][PREFIX + "/plans/{plan_id}/execute"]["post"]
    empty = execute["requestBody"]["content"]["application/json"]["schema"]
    assert empty["type"] == "object" and empty["additionalProperties"] is False
    assert not ({"PlanCreateInput", "PlanStepInput", "PlanInputRef"}
                & spec.get("components", {}).get("schemas", {}).keys())


@pytest.mark.parametrize("method,path", ASSISTANT_ROUTES)
def test_every_route_requires_real_authentication_and_asset_read(api, method, path):
    body = {"json": payload()} if method == "POST" and path == "/plans" else (
        {"json": {}} if method == "POST" else {})
    assert_rejected(api.request(method, path, user=None, **body), 401)
    assert_rejected(api.request(method, path, user=None,
                                headers={"Authorization": "Bearer " + SECRET}, **body), 401)
    assert_rejected(api.request(method, path, user=5, **body), 403)
    assert_no_plans(api)


def test_actual_cached_jwt_is_rejected_after_persistent_session_revocation(api):
    assert api.request("GET", "/capabilities").status_code == 200
    token = api.tokens[2]
    assert token in auth.AUTH_TOKENS
    with api.factory() as db:
        session = db.query(AuthSession).filter_by(user_id=2).one()
        session.revoked_at = auth._utc_now_naive()
        session.revoke_reason = "t04-independent-session"
        db.commit()
    assert token in auth.AUTH_TOKENS
    for method, path in ASSISTANT_ROUTES:
        assert_rejected(api.request(method, path, json={} if method == "POST" else None), 401)
    assert token not in auth.AUTH_TOKENS
    assert api.request("GET", "/capabilities", user=3).status_code == 200
    assert_no_plans(api)


def test_fresh_principal_storage_failure_is_safe_503(api, monkeypatch, caplog):
    def unavailable(db, actor):
        raise OperationalError("SELECT principal", {"credential": SECRET}, RuntimeError(SECRET))

    monkeypatch.setattr(registry, "_principal", unavailable)
    caplog.clear()
    assert_rejected(api.request("GET", "/capabilities"), 503, "plan_storage_unavailable")
    assert SECRET not in caplog.text
    assert_no_plans(api)


def test_list_callback_storage_failure_and_failed_rollback_are_safe_503(api, monkeypatch, caplog):
    rollbacks = []

    def unavailable(*args, **kwargs):
        raise OperationalError("SELECT plans", {"credential": SECRET}, RuntimeError(SECRET))

    def failing_rollback_db():
        with api.factory() as db:
            def failed_rollback():
                rollbacks.append(True)
                raise OperationalError("ROLLBACK", {}, RuntimeError(SECRET))

            monkeypatch.setattr(db, "rollback", failed_rollback)
            yield db

    api.app.dependency_overrides[database.get_db] = failing_rollback_db
    monkeypatch.setattr(assistant.plans, "list_plans", unavailable)
    caplog.clear()
    assert_rejected(api.request("GET", "/plans"), 503, "plan_storage_unavailable")
    assert rollbacks == [True]
    assert "assistant_rollback_failed" in caplog.text and SECRET not in caplog.text
    assert_no_plans(api)


def test_catalog_filters_and_discovery_never_create_plans_or_seed_templates(api):
    result = api.request("GET", "/capabilities?engine=mysql&kind=query&execution=server")
    assert result.status_code == 200
    body = result.json()
    assert body["total"] == body["summary"]["count"] == len(body["items"]) > 0
    assert body["summary"]["by_kind"] == {"query": body["total"]}
    assert all(item["kind"] == "query" and item["execution"] == "server" for item in body["items"])
    assert QUERY in {item["capability_id"] for item in body["items"]}
    detail = api.request("GET", "/capabilities/" + QUERY)
    assert detail.status_code == 200 and detail.json()["capability_id"] == QUERY
    with api.factory() as db:
        row = db.get(DBCapabilityDefinition, QUERY)
        row.enabled = 0
        db.commit()
    for query, visible in (("", False), ("?include_disabled=true", True)):
        items = api.request("GET", "/capabilities" + query).json()["items"]
        assert (QUERY in {item["capability_id"] for item in items}) is visible
    assert_no_plans(api)
    with api.factory() as db:
        assert db.query(DBInspectionPolicy).count() == 1


def test_create_201_reuse_200_conflict_409_and_missing_input_422_persisted(api):
    first = api.request("POST", "/plans", json=payload(), headers={"Idempotency-Key": "t04-key"})
    assert first.status_code == 201
    plan = first.json()
    assert plan["status"] == "validated" and plan["outcome"] is None and not plan["reused"]
    again = api.request("POST", "/plans", json=payload(), headers={"Idempotency-Key": "t04-key"})
    assert again.status_code == 200
    assert again.json()["plan_id"] == plan["plan_id"] and again.json()["reused"]
    assert_rejected(api.request("POST", "/plans", json=payload(params={"page": 2}),
                                headers={"Idempotency-Key": "t04-key"}), 409, "idempotency_conflict")
    missing = api.request("POST", "/plans", user=1, json=payload("inspection.run.execute"))
    assert missing.status_code == 422
    needs = missing.json()
    assert needs["status"] == "settled" and needs["outcome"] == "needs_input"
    assert needs["missing_params"] == ["instance_id", "policy_id"] and needs["result_ref"] is None
    assert needs["steps"][0]["status"] == "skipped"
    assert api.request("GET", "/plans/" + needs["plan_id"], user=1).json() == needs
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityPlanRevision).count() == 2
        assert db.query(DBCapabilityPlanStep).count() == 2


@pytest.mark.parametrize("capability,schema", [
    (QUERY, "inspection.run_list/v1"), (TEMPLATE, "inspection.policy_list/v1"),
    (MATRIX, "operations.capability_matrix"),
])
def test_execute_real_reader_and_reopen_persisted_detail(api, capability, schema):
    plan = create(api, capability)
    path = "/plans/" + plan["plan_id"]
    response = api.request("POST", path + "/execute", json={})
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["status"] == "settled" and done["outcome"] == "succeeded"
    assert done["requested_by"] == 2 and done["approval_state"] == "not_required"
    step = done["steps"][0]
    assert step["status"] == "succeeded" and step["result_ref"]["kind"] == "capability_read"
    inline = step["result_ref"]["inline"]
    assert step["result_ref"]["schema"] == schema
    if capability == QUERY:
        assert inline["schema"] == schema and inline["total"] == 2
        assert {item["instance_id"] for item in inline["items"]} == {10, 20}
    elif capability == TEMPLATE:
        assert inline["total"] == 1 and inline["items"][0]["policy_key"] == "t04-policy"
    else:
        assert len(inline["instances"]) == 2 and inline["items"]
    with api.factory() as fresh:
        assert fresh.get(DBCapabilityPlan, plan["plan_id"]).result_ref_json == done["result_ref"]
        saved = fresh.query(DBCapabilityPlanStep).filter_by(plan_id=plan["plan_id"]).one()
        assert saved.result_ref_json == step["result_ref"]
        assert fresh.query(DBInspectionPolicy).count() == 1
    assert api.request("GET", path).json() == done
    repeat = api.request("POST", path + "/execute", json={})
    assert repeat.status_code == 200 and repeat.json()["reused"] is True
    assert repeat.json()["steps"] == done["steps"]


def test_read_only_scoped_user_can_use_all_six_routes_with_native_target(api):
    for path in ("/capabilities", "/capabilities/" + QUERY):
        assert api.request("GET", path, user=4).status_code == 200
    template = api.request("GET", "/capabilities/" + TEMPLATE, user=4).json()
    assert template["unavailable_reason"] == "scope_not_supported" and not template["plan_executable"]
    plan = create(api, user=4, instance_id=10)
    path = "/plans/" + plan["plan_id"]
    assert api.request("GET", path, user=4).status_code == 200
    listed = api.request("GET", "/plans", user=4)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    done = api.request("POST", path + "/execute", user=4, json={})
    assert done.status_code == 200 and done.json()["outcome"] == "succeeded"
    result = done.json()["steps"][0]["result_ref"]["inline"]
    assert result["total"] == 1 and {item["instance_id"] for item in result["items"]} == {10}


@pytest.mark.parametrize("extra", [
    {}, {"instance_id": 20}, {"instance_id": 110},
    {"params": {"instance_id": 10}}, {"instance_id": 10, "params": {"nested": {"instance_id": 20}}},
])
def test_scoped_create_cannot_use_missing_normalized_foreign_or_nested_target(api, extra):
    assert_rejected(api.request("POST", "/plans", user=4, json=payload(**extra)), 403)
    assert_no_plans(api)


def test_plan_ownership_is_not_inferred_from_shared_asset_or_identity_source(api):
    plan = create(api, instance_id=10)
    path = "/plans/" + plan["plan_id"]
    for user in (3, 4):
        assert_rejected(api.request("GET", path, user=user), 404, "plan_not_found")
        assert_rejected(api.request("POST", path + "/execute", user=user, json={}), 404, "plan_not_found")
        assert api.request("GET", "/plans", user=user).json()["total"] == 0
    assert api.request("GET", path, user=1).status_code == 200
    assert_rejected(api.request("POST", path + "/execute", user=1, json={}), 404, "plan_not_found")
    assert api.request("GET", path).json()["status"] == "validated"


def test_scoped_detail_execute_and_list_ignore_allowed_target_hints_for_foreign_plan(api):
    plan = create(api, instance_id=20)
    with api.factory() as db:
        db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"))
        db.commit()
    path = "/plans/" + plan["plan_id"]
    assert api.request("GET", "/plans").json()["total"] == 0
    for suffix in ("", "?instance_id=10"):
        assert_rejected(api.request("GET", path + suffix), 403)
        assert_rejected(api.request("POST", path + "/execute" + suffix, json={"instance_id": 10}), 403)
    assert_rejected(api.request("POST", "/plans", json=payload(instance_id=10, parent_plan_id=plan["plan_id"])), 403)


def test_prior_template_plan_hidden_after_scope_restriction_even_with_allowed_target(api):
    primary = create(api, TEMPLATE, instance_id=10)
    secondary = create(api, instance_id=10, steps=[
        {"seq": 1, **payload()}, {"seq": 2, **payload(TEMPLATE), "depends_on": [1]},
    ])
    safe = create(api, instance_id=10)
    for plan in (primary, secondary):
        assert api.request("POST", "/plans/" + plan["plan_id"] + "/execute", json={}).status_code == 200
    with api.factory() as db:
        db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=110, access_level="read"))
        db.commit()
    listed = api.request("GET", "/plans?instance_id=10").json()
    assert listed["total"] == 1 and [row["plan_id"] for row in listed["items"]] == [safe["plan_id"]]
    for plan in (primary, secondary):
        path = "/plans/" + plan["plan_id"]
        assert_rejected(api.request("GET", path), 403, "scope_denied")
        assert_rejected(api.request("POST", path + "/execute", json={}), 403, "scope_denied")


@pytest.mark.parametrize("secondary", [False, True], ids=["primary_unsupported", "secondary_unsupported"])
def test_unsupported_engine_history_remains_listed_but_never_bypasses_business_permission(api, secondary):
    unsupported = "performance.awr.query"
    if secondary:
        plan = create(api, instance_id=10, steps=[
            {"seq": 1, **payload()}, {"seq": 2, **payload(unsupported), "depends_on": [1]},
        ])
    else:
        plan = create(api, unsupported, instance_id=10)
    safe = create(api, instance_id=10)
    path = "/plans/" + plan["plan_id"]
    assert plan["status"] == "settled" and plan["outcome"] == "unsupported"
    assert plan["outcome_kind"] == "engine_not_supported" and plan["result_ref"] is None
    assert api.request("GET", path).json() == plan
    listed = api.request("GET", "/plans?instance_id=10&outcome=unsupported")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert [row["plan_id"] for row in listed.json()["items"]] == [plan["plan_id"]]
    with api.factory() as db:
        assert db.query(RoleActionPermission).filter_by(
            role_id=api.roles["viewer"], action_key="operations.read",
        ).delete() == 1
        db.commit()
    visible = api.request("GET", "/plans").json()
    assert visible["total"] == 1 and [row["plan_id"] for row in visible["items"]] == [safe["plan_id"]]
    assert api.request("GET", "/plans?outcome=unsupported").json()["total"] == 0
    assert_rejected(api.request("GET", path), 403, "permission_denied")
    assert_rejected(api.request("POST", path + "/execute", json={}), 403, "permission_denied")


def test_list_summaries_are_owner_filtered_paginated_and_never_select_lobs(api):
    first = create(api, params={"page": 1})
    second = create(api, params={"page": 2})
    other = create(api, user=3)
    assert api.request("POST", "/plans/" + first["plan_id"] + "/execute", json={}).status_code == 200
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "db_capability_plan" in statement:
            statements.append(statement.lower())

    event.listen(api.engine, "before_cursor_execute", capture)
    try:
        pages = [api.request("GET", f"/plans?page={page}&page_size=1") for page in (1, 2, 3)]
    finally:
        event.remove(api.engine, "before_cursor_execute", capture)
    assert all(response.status_code == 200 for response in pages)
    values = [response.json() for response in pages]
    assert all(body["total"] == 2 and body["page_size"] == 1 for body in values)
    assert [body["page"] for body in values] == [1, 2, 3]
    assert [body["items"][0]["plan_id"] for body in values[:2]] == [second["plan_id"], first["plan_id"]]
    assert values[2]["items"] == []
    for body in values:
        for row in body["items"]:
            assert not {"steps", "params", "result_ref", "capability_snapshot_json"} & row.keys()
            assert row["plan_id"] != other["plan_id"]
    assert statements and any("count(" in sql for sql in statements)
    assert any("limit" in sql for sql in statements)
    for sql in statements:
        assert not any(column in sql for column in (
            "capability_snapshot_json", "result_ref_json", "precondition_json", "snapshot_json",
        )), sql
    filtered = api.request("GET", "/plans?status=settled&outcome=succeeded&capability_id=" + QUERY).json()
    assert filtered["total"] == 1 and filtered["items"][0]["plan_id"] == first["plan_id"]


@pytest.mark.parametrize("secondary", [False, True])
def test_grant_revocation_hides_primary_and_secondary_steps_in_list_detail_and_execute(api, secondary):
    if secondary:
        steps = [{"seq": 1, **payload()}, {"seq": 2, **payload(MATRIX), "depends_on": [1]}]
        plan = create(api, steps=steps)
        safe = create(api)
        revoked = "operations.read"
    else:
        plan = create(api)
        safe = create(api, MATRIX)
        revoked = "inspection.read"
    path = "/plans/" + plan["plan_id"]
    assert api.request("POST", path + "/execute", json={}).json()["outcome"] == "succeeded"
    with api.factory() as db:
        assert db.query(RoleActionPermission).filter_by(role_id=api.roles["viewer"], action_key=revoked).delete() == 1
        db.commit()
    listed = api.request("GET", "/plans").json()
    assert listed["total"] == 1 and [row["plan_id"] for row in listed["items"]] == [safe["plan_id"]]
    assert_rejected(api.request("GET", path), 403, "permission_denied")
    assert_rejected(api.request("POST", path + "/execute", json={}), 403, "permission_denied")
    with api.factory() as db:
        assert db.get(DBCapabilityPlan, plan["plan_id"]).outcome == "succeeded"


@pytest.mark.parametrize("raw,code", [
    ('{"capability_id":"' + SECRET + '","capability_id":"' + QUERY + '","definition_version":1}', "invalid_json"),
    (json.dumps(payload())[:-1] + ',"params":{"page":1,"page":2}}', "invalid_json"),
    (json.dumps(payload())[:-1] + ',"params":{"page":NaN}}', "invalid_json"),
    (json.dumps(payload())[:-1] + ',"params":{"page":Infinity}}', "invalid_json"),
    (json.dumps(payload())[:-1] + ',"params":{"page":-Infinity}}', "invalid_json"),
    ('{"' + SECRET + '":', "invalid_json"),
    ('["' + SECRET + '"]', "invalid_json"),
    (b'{"value":"\xff"}', "invalid_json"),
    (json.dumps(payload(params={"sql": SECRET})), "plan_raw_payload_rejected"),
    (json.dumps(payload(params={"nested": [{"command": SECRET}]})), "plan_raw_payload_rejected"),
    (json.dumps(payload(params={"handler": SECRET})), "request_supplied_handler_rejected"),
    (json.dumps(payload(params={"nested": {"password": SECRET}})), "plan_sensitive_payload_rejected"),
    (json.dumps(payload(unknown=SECRET)), "invalid_plan_request"),
    (json.dumps(payload(requested_by=1)), "invalid_plan_request"),
    (json.dumps(payload(definition_version=True)), "invalid_plan_request"),
    (json.dumps(payload(steps=[{"seq": 1, **payload(), "unknown": SECRET}])), "invalid_plan_request"),
    (json.dumps(payload(params={"unknown": SECRET})), "invalid_params"),
    (json.dumps(payload(params={"page": True})), "invalid_params"),
])
def test_raw_create_rejects_unsafe_json_without_echo_or_persisting(api, caplog, raw, code):
    caplog.clear()
    response = api.request("POST", "/plans", content=raw, headers={"Content-Type": "application/json"})
    assert_rejected(response, 400, code)
    assert SECRET not in caplog.text
    assert_no_plans(api)


@pytest.mark.parametrize("method,path", [("POST", "/plans"), ("POST", "/plans/" + MISSING_PLAN + "/execute")])
def test_body_size_limit_and_media_type_fail_without_echo(api, caplog, method, path):
    raw = json.dumps({"value": SECRET, "padding": "x" * assistant.MAX_BODY_BYTES})
    assert_rejected(api.request(method, path, content=raw, headers={"Content-Type": "application/json"}),
                    413, "request_too_large")
    assert_rejected(api.request(method, path, content=SECRET, headers={"Content-Type": "text/plain"}),
                    415, "json_required")
    assert SECRET not in caplog.text
    assert_no_plans(api)


@pytest.mark.parametrize("override", [
    {"params": {"page": 2}}, {"instance_id": 20}, {"capability_id": MATRIX},
    {"steps": []}, {"approval_state": "approved"}, {"unknown": SECRET},
    {"password": SECRET},
])
def test_execution_overrides_never_change_frozen_plan_or_run_readers(api, caplog, override):
    plan = create(api)
    path = "/plans/" + plan["plan_id"]
    caplog.clear()
    assert_rejected(api.request("POST", path + "/execute", json=override), 400, "execution_params_not_allowed")
    assert SECRET not in caplog.text
    assert api.request("GET", path).json() == plan
    with api.factory() as db:
        row = db.query(DBCapabilityPlanStep).filter_by(plan_id=plan["plan_id"]).one()
        assert row.status == "pending" and row.result_ref_json is None


@pytest.mark.parametrize("raw,code", [
    ('{"value":"' + SECRET + '","value":1}', "invalid_json"),
    ('{"value":NaN}', "invalid_json"),
    ('{"sql":"SELECT ' + SECRET + '"}', "plan_raw_payload_rejected"),
    ('{"command":"echo ' + SECRET + '"}', "plan_raw_payload_rejected"),
    ('["' + SECRET + '"]', "invalid_json"),
])
def test_execute_raw_json_is_checked_before_reusing_or_running_plan(api, caplog, raw, code):
    plan = create(api)
    path = "/plans/" + plan["plan_id"]
    caplog.clear()
    assert_rejected(api.request("POST", path + "/execute", content=raw,
                                headers={"Content-Type": "application/json"}), 400, code)
    assert SECRET not in caplog.text
    assert api.request("GET", path).json() == plan


def test_explicit_json_empty_execute_body_is_not_an_empty_object(api):
    plan = create(api)
    path = "/plans/" + plan["plan_id"]
    assert_rejected(api.request("POST", path + "/execute", content=b"",
                                headers={"Content-Type": "application/json"}), 400, "invalid_json")
    assert api.request("GET", path).json() == plan


@pytest.mark.parametrize("query", [
    "page=" + SECRET, "page_size=" + SECRET, "instance_id=" + SECRET,
    "page=0", "page_size=101", "instance_id=0",
])
def test_invalid_known_query_values_are_safe_422(api, caplog, query):
    caplog.clear()
    assert_rejected(api.request("GET", "/plans?" + query), 422, "invalid_request")
    assert SECRET not in caplog.text


@pytest.mark.parametrize("query", ["engine=" + SECRET, "kind=" + SECRET, "execution=" + SECRET,
                                      "include_disabled=" + SECRET])
def test_invalid_catalog_query_values_are_safe_422(api, caplog, query):
    caplog.clear()
    assert_rejected(api.request("GET", "/capabilities?" + query), 422, "invalid_request")
    assert SECRET not in caplog.text


@pytest.mark.parametrize("path", ["/plans?unknown=" + SECRET, "/plans?page=1&page=2",
                                     "/capabilities?engine=mysql&engine=oracle"])
def test_unknown_and_duplicate_queries_are_rejected(api, path):
    assert_rejected(api.request("GET", path), 400, "invalid_query")


def test_post_query_overrides_and_unexposed_mutations_are_denied(api):
    plan = create(api, user=4, instance_id=10)
    path = "/plans/" + plan["plan_id"]
    for method, target in (("POST", path + "/approve"), ("POST", path + "/cancel"),
                           ("POST", "/capabilities/sync"), ("PUT", "/plans")):
        assert_rejected(api.request(method, target, user=4, json={"instance_id": 10}), 403)
    assert_rejected(api.request("POST", "/plans?page=1", json=payload()), 400, "invalid_query")
    assert_rejected(api.request("POST", path + "/execute?page=1", user=4, json={}), 400, "invalid_query")
    assert api.request("GET", path, user=4).json() == plan
