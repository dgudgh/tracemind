"""Assistant boundary checks use isolated SQLite, never a live database or API."""

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.routing import Mount, Route

from app.cmdb.models import AssetInstance
from app.models.access_control import UserAssetScope
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_plan import DBCapabilityPlan
from app.models.inspection_policy import DBInspectionPolicy
from app.services import access_control, request_scope


PREFIX = "/api/v1"
CAPABILITY = "inspection.run.query"
ALLOWED, FOREIGN, GLOBAL, MISSING = ("pl" + digit * 26 for digit in "1234")
CREATE = "/assistant/plans"
DISPATCH = "/assistant/dispatch-intent"
DETAIL = "/assistant/plans/{plan_id}"
EXECUTE = DETAIL + "/execute"
CATALOG_DETAIL = "/assistant/capabilities/{capability}"
PROPOSALS = "/assistant/workflows/proposals"
PROVIDERS = "/assistant/workflows/providers"
ROUTES = (
    ("GET", "/assistant/capabilities"), ("GET", CATALOG_DETAIL),
    ("POST", DISPATCH),
    ("POST", CREATE), ("GET", CREATE),
    ("GET", DETAIL), ("POST", EXECUTE),
    ("GET", PROPOSALS), ("POST", PROPOSALS), ("GET", PROVIDERS),
)


async def inert_endpoint(request):
    raise AssertionError("Scope tests must not execute a business HTTP handler")


def route(template, method="GET"):
    return Route(PREFIX + template, inert_endpoint, methods=[method])


def make_request(path, method="GET", payload=None, *, auth=None, routes=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    url, _, query = (PREFIX + path).partition("?")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": method, "path": url,
        "query_string": query.encode(), "headers": [(b"content-type", b"application/json")],
        "app": SimpleNamespace(routes=(
            [route(template, verb) for verb, template in ROUTES] if routes is None else routes
        )),
    }, receive)
    request.state.auth = auth if auth is not None else {"user_id": 7, "source": "db", "role": "user"}
    return request, body


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    for model in (AssetInstance, UserAssetScope, DBCapabilityPlan, AIProviderConfig, DBInspectionPolicy):
        model.__table__.create(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all([
            AIProviderConfig(id=1, provider_name="enabled", provider_type="ollama", base_url="http://127.0.0.1:11434",
                             model_name="m1", enabled=1, is_default=1, timeout_seconds=30),
            AIProviderConfig(id=2, provider_name="disabled", provider_type="ollama", base_url="http://127.0.0.1:11434",
                             model_name="m2", enabled=0, is_default=0, timeout_seconds=30),
            DBInspectionPolicy(id=1, policy_key="p-on", name="on", db_types_json=["mysql"], enabled=1),
            DBInspectionPolicy(id=2, policy_key="p-off", name="off", db_types_json=["mysql"], enabled=0),
        ])
        # Deliberately distinct normalized/native IDs catch namespace confusion.
        for normalized, native in ((110, 10), (120, 20)):
            session.add(AssetInstance(
                id=normalized, native_instance_id=native, stable_key=f"scope:{native}",
                name=f"scope-{native}", db_type="mysql", ip="127.0.0.1", port=3306,
                host_id=normalized, region_id=1, site_id=1, zone_id=1, env_id=1, status="active",
            ))
        session.add_all([
            UserAssetScope(user_id=7, scope_type="instance", scope_id=110, access_level="read"),
            UserAssetScope(user_id=8, scope_type="instance", scope_id=110, access_level="operate"),
            UserAssetScope(user_id=10, scope_type="all", access_level="read"),
        ])
        for identity, native in ((ALLOWED, 10), (FOREIGN, 20), (GLOBAL, None)):
            session.add(DBCapabilityPlan(
                plan_id=identity, capability_id=CAPABILITY, definition_version=1,
                capability_snapshot_json={}, instance_id=native, requested_by=7,
                trigger_type="manual", idempotency_key=identity, created_at=datetime(2026, 10, 20),
            ))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture(params=["sync", "async"])
def allows(db, request):
    def check(path, method="GET", payload=None, *, user_id=7, minimum="read", **kwargs):
        incoming, body = make_request(path, method, payload, **kwargs)
        with patch.object(access_control, "resolve_scope", wraps=access_control.resolve_scope) as resolve:
            if request.param == "sync":
                result = request_scope.scope_allows_request_sync(db, user_id, incoming, body)
            else:
                result = asyncio.run(request_scope.scope_allows_request(db, user_id, incoming))
        resolve.assert_called_once_with(db, user_id, minimum_access=minimum)
        assert not incoming.scope.get("path_params"), "The check must run before routing"
        return result
    return check


@pytest.mark.parametrize("path", ["/assistant/capabilities", "/assistant/capabilities/" + CAPABILITY])
def test_catalog_and_real_capability_parameter_are_safe_metadata(allows, path):
    assert allows(path)
    assert allows(path + "?capability_id=" + CAPABILITY)
    assert not allows(path + "?instance_id=20")
    assert not allows(path, "POST", {"instance_id": 10}, minimum="operate")


def test_dispatch_intent_is_read_scoped_to_the_explicit_instance(allows):
    assert allows(DISPATCH, "POST", {"text": "查看当前实例", "instance_id": 10}, minimum="read")
    assert not allows(DISPATCH, "POST", {"text": "查看其他实例", "instance_id": 20}, minimum="read")
    assert not allows(DISPATCH, "POST", {"text": "没有目标"}, minimum="read")
    assert not allows(DISPATCH + "?instance_id=20", "POST",
                      {"text": "冲突目标", "instance_id": 10}, minimum="read")


@pytest.mark.parametrize("capability", [
    "inspection.run.query", "operations.capability_matrix.read", "a.b.c", "a" * 60 + ".b.c",
])
def test_semantic_capability_ids_are_not_asset_resources(allows, capability):
    assert allows("/assistant/capabilities/" + capability)
    assert allows(CREATE, "POST", {
        "instance_id": 10, "capability_id": capability,
        "steps": [{"capability_id": capability, "params": {"instance_id": 10}}],
    })
    assert allows(CREATE + "?capability_id=" + capability)


@pytest.mark.parametrize("capability", [
    "", "a.b", "a.b.c.d", "A.b.c", "a.b.c-", "a.b.c\n", "a" * 61 + ".b.c",
    "10", 10, True, None, [CAPABILITY], {"instance_id": 10},
])
def test_capability_exemption_is_syntax_and_type_bounded(allows, capability):
    assert not allows(CREATE, "POST", {"instance_id": 10, "capability_id": capability})
    assert not allows(CREATE, "POST", {"instance_id": 10, "steps": [{"capability_id": capability}]})
    if isinstance(capability, str) and capability and "\n" not in capability:
        assert not allows("/assistant/capabilities/" + capability)


def test_read_only_native_scope_can_create_execute_and_read_plan(allows):
    assert allows(CREATE, "POST", {"instance_id": 10, "capability_id": CAPABILITY})
    assert allows(DETAIL.replace("{plan_id}", ALLOWED))
    assert allows(EXECUTE.replace("{plan_id}", ALLOWED), "POST", {})
    assert not allows(CREATE, "POST", {"instance_id": 110, "capability_id": CAPABILITY})


@pytest.mark.parametrize("identity", [FOREIGN, GLOBAL, MISSING, "10", "pl" + "f" * 27, "pl" + "x" * 26])
def test_persisted_plan_scope_cannot_be_replaced_by_allowed_hints(allows, identity):
    for template, method in ((DETAIL, "GET"), (EXECUTE, "POST")):
        path = template.replace("{plan_id}", identity) + "?instance_id=10"
        assert not allows(path, method, {"instance_id": 10})


@pytest.mark.parametrize("parent,expected", [(ALLOWED, True), (FOREIGN, False), (GLOBAL, False), (MISSING, False)])
def test_parent_plan_id_is_a_string_resource_resolved_from_its_own_row(allows, parent, expected):
    assert allows(CREATE, "POST", {
        "instance_id": 10, "capability_id": CAPABILITY, "parent_plan_id": parent,
    }) is expected


@pytest.mark.parametrize("extra", [
    {"instance_id": 20}, {"source_instance_id": 20}, {"instance_ids": [10, 20]},
    {"parent_plan_id": FOREIGN}, {"parent_plan_id": GLOBAL}, {"parent_plan_id": MISSING},
    {"plan_id": FOREIGN}, {"plan_ids": [ALLOWED, FOREIGN]}, {"id": 10},
    {"unknown_id": 10}, {"unknown_ids": [10]}, {"run_id": 10},
    {"capability_ids": [CAPABILITY]}, {"other_capability_id": CAPABILITY},
])
def test_every_nested_foreign_parent_instance_and_unknown_id_fails_closed(allows, extra):
    payload = {"instance_id": 10, "capability_id": CAPABILITY,
               "steps": [{"capability_id": CAPABILITY, "params": {"deeper": [[extra]]}}]}
    assert not allows(CREATE, "POST", payload)
    assert not allows(EXECUTE.replace("{plan_id}", ALLOWED), "POST", payload)


@pytest.mark.parametrize("payload", [
    {}, {"capability_id": CAPABILITY}, {"instance_id": None},
    {"params": {"instance_id": 10}}, {"parent_plan_id": ALLOWED},
    [{"instance_id": 10}], {"instance_id": False}, {"instance_id": []},
])
def test_creation_requires_a_top_level_target_not_query_or_nested_hint(allows, payload):
    assert not allows(CREATE + "?instance_id=10", "POST", payload)


def test_foreign_creation_and_conflicting_query_targets_are_denied(allows):
    assert not allows(CREATE, "POST", {"instance_id": 20, "capability_id": CAPABILITY})
    assert not allows(CREATE + "?instance_id=20&instance_id=10", "POST", {"instance_id": 10})
    assert not allows(DETAIL.replace("{plan_id}", ALLOWED) + "?parent_plan_id=" + FOREIGN)


@pytest.mark.parametrize("auth,expected", [
    (None, True), ({"user_id": 7, "source": "identity", "role": "user"}, True),
    ({}, False), ({"user_id": 8, "source": "db", "role": "user"}, False),
    ({"user_id": 7, "source": "mes", "role": "user"}, False),
    ({"user_id": 7, "source": "db", "role": "admin"}, False),
])
def test_plan_collection_requires_service_filter_compatible_auth(allows, auth, expected):
    # This is the middleware contract, not evidence of the API's actual filtering.
    assert allows(CREATE, auth=auth) is expected
    assert not allows(CREATE + "?instance_id=20", auth=auth)


@pytest.mark.parametrize("template,path,method", [
    ("/assistant/capabilities/sync", "/assistant/capabilities/sync", "POST"),
    (DETAIL + "/cancel", DETAIL.replace("{plan_id}", ALLOWED) + "/cancel", "POST"),
    (DETAIL + "/approve", DETAIL.replace("{plan_id}", ALLOWED) + "/approve", "POST"),
    (EXECUTE + "/extra", EXECUTE.replace("{plan_id}", ALLOWED) + "/extra", "POST"),
    (EXECUTE, EXECUTE.replace("{plan_id}", ALLOWED), "GET"),
    (DETAIL, DETAIL.replace("{plan_id}", ALLOWED), "POST"),
    (CREATE, CREATE, "PUT"), ("/assistant/unknown", "/assistant/unknown", "POST"),
    ("/assistant/unknown", "/assistant/unknown", "GET"),
    ("/assistant/capabilities/{capability_id}", "/assistant/capabilities/" + CAPABILITY, "GET"),
])
def test_no_new_assistant_methods_or_paths_inherit_the_exception(allows, template, path, method):
    minimum = "read" if method == "GET" else "operate"
    assert not allows(path + "?instance_id=10", method, {"instance_id": 10, "capability_id": CAPABILITY},
                      routes=[route(template, method)], minimum=minimum)


def test_unregistered_shadowed_and_mounted_routes_do_not_get_read_exception(allows):
    for routes in (
        [], [route("/assistant/{unknown}", "POST"), route(CREATE, "POST")],
        [Mount(PREFIX, app=SimpleNamespace())],
        [route(CREATE, "GET")],
    ):
        assert not allows(CREATE, "POST", {"instance_id": 10}, routes=routes, minimum="operate")
    assert not allows(CREATE + "/extra", "POST", {"instance_id": 10}, minimum="operate")


def test_existing_non_assistant_post_still_requires_operate_and_never_exempts_capability_id(allows):
    template, path = "/operations/mysql/{instance_id}/actions", "/operations/mysql/10/actions"
    routes = [route(template, "POST")]
    assert not allows(path, "POST", {}, routes=routes, minimum="operate")
    assert allows(path, "POST", {}, user_id=8, routes=routes, minimum="operate")
    assert not allows(path, "POST", {"capability_id": CAPABILITY},
                      user_id=8, routes=routes, minimum="operate")
    # An all/read grant must not become an all/operate bypass for unknown POSTs.
    assert not allows("/assistant/unknown", "POST", {}, user_id=10,
                      routes=[route("/assistant/unknown", "POST")], minimum="operate")


@pytest.fixture
def workflow_db(db):
    import app.models  # noqa: F401
    from app.core.database import Base
    from app.models.access_control import UserAccessRole
    from app.models.db_instance import DBInstance
    from app.models.user import User
    from app.services import capability_registry as registry

    Base.metadata.create_all(db.get_bind())
    roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
    db.add_all([
        User(id=1, username="scope-admin", password_hash="unused", role="admin", status="active"),
        User(id=7, username="scope-reader", password_hash="unused", role="user", status="active"),
        UserAccessRole(user_id=7, role_id=roles["viewer"].id),
        DBInstance(id=10, instance_name="isolated-scope", db_type="mysql", host="127.0.0.1",
                   port=3306, username="unused", password_cipher="env://SYNTHETIC_SCOPE_PASSWORD", enabled=1),
    ])
    db.commit()
    registry.sync_definitions(db, actor={"user_id": 1})
    return db


def test_read_post_exception_is_pinned_to_the_registered_read_adapters():
    from app.services import capability_handler, capability_registry as registry, plan_service

    wired = [entry for entry in registry.validate_registry()
             if entry["execution"] == "server" and entry.get("handler") in capability_handler.HANDLERS]
    adapters = [entry for entry in wired if entry["kind"] in {"read", "query"}]
    adapter_ids = {entry["capability_id"] for entry in adapters}
    assert adapter_ids and all(entry["kind"] in {"read", "query"} for entry in adapters)
    # Explicitly typed contracts are a subset of the wired read adapters; unlisted adapters
    # fall back to untyped inputs rather than being silently treated as writes.
    assert set(plan_service.READ_CONTRACTS) <= adapter_ids | {"inspection.run.execute"}
    writers = [entry for entry in wired if entry["kind"] not in {"read", "query"}]
    writer_ids = {entry["capability_id"] for entry in writers}
    assert writer_ids and not (writer_ids & adapter_ids)
    assert all(entry["kind"] not in {"read", "query"} for entry in writers)
    # The read-POST exception never covers a writer: every writer needs a durable plan.
    assert "inspection.run.execute" in writer_ids


@pytest.mark.parametrize("capability", ["inspection.run.query", "operations.capability_matrix.read"])
def test_read_scope_boundary_and_real_t03_workflow_agree(workflow_db, allows, capability):
    from app.services import plan_service

    payload = {"instance_id": 10, "capability_id": capability, "definition_version": 1}
    assert allows(CREATE, "POST", payload)
    created = plan_service.create_plan(workflow_db, actor={"user_id": 7}, **payload)
    assert created["status"] == "validated"
    assert allows(EXECUTE.replace("{plan_id}", created["plan_id"]), "POST", {})
    executed = plan_service.execute_plan(workflow_db, created["plan_id"], actor={"user_id": 7})
    assert executed["outcome"] == "succeeded"
    assert executed["result_ref"] is not None


@pytest.mark.parametrize("approval", ["pending", "approved"])
def test_execution_post_is_not_read_exempt_and_never_trusts_a_stored_approval(
    workflow_db, allows, monkeypatch, approval,
):
    from app.services import capability_registry as registry, plan_service

    # inspection.run.execute is no longer a read-POST exception, so the request must produce a
    # plan. The plan is created in approval state "pending", and the stored flag alone must never
    # unlock execution. Params stay incomplete here (no target policy), so the plan settles as
    # needs_input instead of being dispatched.
    created = plan_service.create_plan(
        workflow_db, actor={"user_id": 1}, capability_id="inspection.run.execute",
        definition_version=2, instance_id=10,
    )
    assert created["outcome"] == "needs_input" and created["outcome_kind"] == "missing_params"
    plan = workflow_db.get(DBCapabilityPlan, created["plan_id"])
    assert plan.approval_state == "pending"
    plan.approval_state = approval
    workflow_db.commit()
    assert allows(EXECUTE.replace("{plan_id}", plan.plan_id), "POST", {})

    def forbidden(*args, **kwargs):
        pytest.fail("Side effects/approved plans must not reach any T03 executor")

    monkeypatch.setattr(registry, "execute_read", forbidden)
    result = plan_service.execute_plan(workflow_db, plan.plan_id, actor={"user_id": 1})
    assert result["outcome"] != "succeeded" and result["result_ref"] is None
    assert result["approval_state"] == approval
    assert all(step["status"] == "skipped" for step in result["steps"])


# --- C11 proposal consent is reachable for a scoped operator, and only within scope --------

def proposal_payload(**overrides):
    payload = {"text": "查看已保存巡检作业", "scope": {"mode": "explicit", "targets": [{"instance_id": 10}]},
               "provider_config_id": 1, "provider_revision": "a" * 64, "allow_model_context": True}
    payload.update(overrides)
    return payload


def test_a_scoped_operator_may_list_providers_and_propose_inside_scope(allows):
    assert allows(PROVIDERS)
    assert allows(PROPOSALS, "POST", proposal_payload())
    assert allows(PROPOSALS, "POST", proposal_payload(scope={"mode": "global"}))
    # A template and a provider are references, not asset grants: both must exist and be enabled.
    assert allows(PROPOSALS, "POST", proposal_payload(
        scope={"mode": "explicit", "targets": [{"instance_id": 10, "slots": {"policy_id": 1}}]},
        confirmed_policy_id=None))


def test_proposal_never_widens_the_asset_scope(allows):
    assert not allows(PROPOSALS, "POST", proposal_payload(scope={"mode": "explicit", "targets": [{"instance_id": 20}]}))
    assert not allows(PROPOSALS, "POST", proposal_payload(scope={"mode": "explicit", "targets": [{"instance_id": 10},
                                                                                                 {"instance_id": 20}]}))
    assert not allows(PROPOSALS, "POST", proposal_payload(scope={"mode": "explicit", "targets": [{"instance_id": 110}]}))


@pytest.mark.parametrize("overrides", [
    {"provider_config_id": 2},          # disabled provider
    {"provider_config_id": 9},          # missing provider
    {"provider_config_id": True},       # bool is not an ID
    {"provider_config_id": "abc"},      # non-numeric string is not an ID
    {"confirmed_policy_id": 2},         # disabled template
    {"confirmed_policy_id": 9},         # missing template
    {"confirmed_policy_id": True},
])
def test_proposal_references_must_exist_and_be_enabled(allows, overrides):
    assert not allows(PROPOSALS, "POST", proposal_payload(**overrides))


def test_proposal_body_is_read_class_but_still_a_bounded_scope_check(allows):
    # The route is read-class, so the resolved scope level is always "read"; a foreign target
    # still fails even though the caller's own access level is higher.
    assert allows(PROPOSALS, "POST", proposal_payload(), minimum="read")
    assert not allows(PROPOSALS, "POST", proposal_payload(scope={"mode": "explicit", "targets": [{"instance_id": 20}]}),
                      minimum="read")


def test_proposal_and_provider_reads_are_not_writable_and_unknown_verbs_are_denied(allows):
    assert not allows(PROPOSALS, "PUT", proposal_payload(), minimum="operate")
    assert not allows(PROVIDERS, "POST", {}, minimum="operate")
