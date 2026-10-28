"""Exercise the actual registry/adapters against isolated storage, including denial paths."""
from copy import deepcopy
from datetime import UTC, datetime
import ast
import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import AccessRole, RoleActionPermission, SecurityAuditEvent, UserAccessRole, UserAssetScope
from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_plan import DBCapabilityPlan
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.user import User
from app.services import access_control, capability_registry as registry, operations_capabilities


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="registry-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="registry-reader", password_hash="unused", role="user", status="active"),
            User(id=3, username="registry-no-role", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"].id),
        ])
        for identity in (10, 20):
            session.add(DBInstance(id=identity, instance_name=f"instance-{identity}", db_type="mysql",
                                   host="127.0.0.1", port=3306, username="unused", enabled=1,
                                   password_cipher="env://SYNTHETIC_CAPABILITY_TEST_PASSWORD"))
            session.add(AssetInstance(id=identity, native_instance_id=identity,
                                      stable_key=f"asset:{identity}", name=f"asset-{identity}",
                                      db_type="mysql", ip="127.0.0.1", port=3306,
                                      host_id=identity, region_id=1, site_id=1,
                                      zone_id=1, env_id=1, status="active"))
        session.commit()
        yield session
    engine.dispose()


ADMIN = {"user_id": 1}
READER = {"user_id": 2}
NO_ROLE = {"user_id": 3}


def _sync(db):
    return registry.sync_definitions(db, actor=ADMIN)


def _change_declaration(monkeypatch, capability_id, **values):
    entries = deepcopy(registry.CAPABILITY_REGISTRY)
    target = next(row for row in entries if row["capability_id"] == capability_id)
    target.update(values)
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", entries)


def test_reads_never_sync_or_create_plans(db):
    view = registry.list_capabilities(db, actor=ADMIN)
    assert view["total"] == len(registry.CAPABILITY_REGISTRY)
    assert not any(item["plan_executable"] for item in view["items"])
    assert {item["unavailable_reason"] for item in view["items"]} == {"definition_not_synced"}
    assert db.query(DBCapabilityDefinition).count() == 0
    assert db.query(DBCapabilityPlan).count() == 0


def test_sync_preserves_disable_and_records_only_code_owned_definitions(db):
    first = _sync(db)
    assert first["created"] == len(registry.CAPABILITY_REGISTRY)
    registry.set_enabled(db, "inspection.run.query", enabled=False, actor=ADMIN)
    _sync(db)
    assert not registry.effective_capability(db, "inspection.run.query")["enabled"]
    with pytest.raises(registry.CapabilityError, match="capability_disabled"):
        registry.execute_read(db, "inspection.run.query", 1, actor=ADMIN)


def test_database_row_cannot_invent_an_executable_handler(db):
    _sync(db)
    db.add(DBCapabilityDefinition(capability_id="rogue.subject.execute", definition_version=1,
                                  definition_hash="0" * 64, domain="rogue", kind="execute",
                                  execution="server", enabled=1, synced_at=datetime.now(UTC).replace(tzinfo=None)))
    db.commit()
    with pytest.raises(registry.CapabilityError, match="unregistered_capability"):
        registry.execute_read(db, "rogue.subject.execute", 1, actor=ADMIN)
    assert any(row["conflict_kind"] == "db_row_without_declaration"
               for row in registry.detect_conflicts(db))


def test_version_and_hash_drift_are_blocked_before_handler(db, monkeypatch):
    _sync(db)
    with pytest.raises(registry.CapabilityError, match="stale_definition_version"):
        registry.execute_read(db, "inspection.run.query", 2, actor=ADMIN)
    _change_declaration(monkeypatch, "inspection.run.query", title="Changed without a version")
    with pytest.raises(registry.CapabilityError, match="definition_out_of_sync"):
        registry.execute_read(db, "inspection.run.query", 1, actor=ADMIN)
    with pytest.raises(registry.CapabilityError, match="definition_version_not_incremented"):
        _sync(db)


def test_new_version_requires_explicit_sync_and_downgrade_is_refused(db, monkeypatch):
    _sync(db)
    original = registry.CAPABILITY_REGISTRY
    _change_declaration(monkeypatch, "inspection.run.query", version=2)
    with pytest.raises(registry.CapabilityError, match="definition_out_of_sync"):
        registry.execute_read(db, "inspection.run.query", 2, actor=ADMIN)
    _sync(db)
    assert registry.execute_read(db, "inspection.run.query", 2, actor=ADMIN)["total"] == 0
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", original)
    with pytest.raises(registry.CapabilityError, match="definition_version_regression"):
        _sync(db)


def test_derived_maturity_wins_over_database_claims(db):
    _sync(db)
    row = db.get(DBCapabilityDefinition, "protection.backup.execute")
    row.declared_maturity_json = {"sqlserver": "ready"}
    db.commit()
    actual = registry.effective_capability(db, row.capability_id)
    assert actual["effective_maturity"] == operations_capabilities.protection_maturity("backup")
    assert actual["effective_maturity"]["sqlserver"] != "ready"
    assert any(item.get("engine") == "sqlserver" and item["conflict_kind"] == "maturity_conflict"
               for item in registry.detect_conflicts(db))
    assert registry.effective_maturity("protection.backup.plan")["sqlserver"] != "blocked"


def test_derived_maturity_cannot_be_redeclared_on_sync(db, monkeypatch):
    _change_declaration(monkeypatch, "protection.backup.execute", declared_maturity={"sqlserver": "ready"})
    assert registry.effective_maturity("protection.backup.execute")["sqlserver"] != "ready"
    with pytest.raises(registry.CapabilityError, match="derived_maturity_is_not_declarable"):
        _sync(db)
    assert db.query(DBCapabilityDefinition).count() == 0


def test_conflict_log_keeps_safe_fields_in_plain_formatter(db, caplog):
    _sync(db)
    row = db.get(DBCapabilityDefinition, "protection.backup.execute")
    row.declared_maturity_json = {"sqlserver": "ready", "untrusted\nengine": "secret raw payload"}
    db.commit()
    registry.detect_conflicts(db)
    assert '"capability_id":"protection.backup.execute"' in caplog.text
    assert '"engine":"sqlserver"' in caplog.text
    assert '"declared":"ready"' in caplog.text
    assert '"derived":"partial"' in caplog.text
    assert "untrusted" not in caplog.text
    assert "secret raw payload" not in caplog.text
    row.declared_maturity_json = ["not an engine map"]
    db.commit()
    assert any(item["conflict_kind"] == "invalid_maturity_claim" for item in registry.detect_conflicts(db))


def test_session_termination_and_client_handlers_never_execute(db):
    _sync(db)
    assert set(registry.effective_maturity("operations.termination.execute").values()) == {"unsupported"}
    # platform.dashboard.read is wired now (C13 batch 2); the client-bound example must be one
    # that still has no server handler at all.
    for capability in ("operations.termination.execute", "protection.restore.execute"):
        with pytest.raises(registry.CapabilityError, match="handler_not_wired"):
            registry.execute_read(db, capability, 1, actor=ADMIN)


def test_permission_filtering_and_actor_role_cannot_be_supplied_by_caller(db):
    _sync(db)
    assert registry.list_capabilities(db, actor=NO_ROLE)["items"] == []
    with pytest.raises(registry.CapabilityError, match="permission_denied"):
        registry.execute_read(db, "inspection.run.query", 1, actor=NO_ROLE)
    with pytest.raises(registry.CapabilityError, match="permission_denied"):
        registry.sync_definitions(db, actor={"user_id": 3, "role": "admin"})
    with pytest.raises(registry.CapabilityError, match="permission_denied"):
        registry.set_enabled(db, "inspection.run.query", enabled=False, actor=READER)
    assert "platform.user.read" not in {row["capability_id"] for row in registry.list_capabilities(db, actor=READER)["items"]}


def test_permission_and_account_revocation_rechecked(db):
    _sync(db)
    assert registry.execute_read(db, "inspection.run.query", 1, actor=READER)["total"] == 0
    db.query(UserAccessRole).filter_by(user_id=2).delete()
    db.commit()
    with pytest.raises(registry.CapabilityError, match="permission_denied"):
        registry.execute_read(db, "inspection.run.query", 1, actor=READER)
    user = db.get(User, 1)
    user.status = "disabled"
    db.commit()
    with pytest.raises(registry.CapabilityError, match="actor_inactive"):
        registry.execute_read(db, "inspection.run.query", 1, actor=ADMIN)


def _grant_only(db, *actions):
    db.query(UserAccessRole).filter_by(user_id=2).delete()
    role = AccessRole(role_key="registry-test-only", name="Registry test", status="active")
    db.add(role)
    db.flush()
    db.add(UserAccessRole(user_id=2, role_id=role.id))
    db.add_all([RoleActionPermission(role_id=role.id, action_key=action) for action in actions])
    db.commit()


def test_engine_specific_permissions_do_not_union_other_engine_side_effects(db):
    _sync(db)
    _grant_only(db, "operations.read")
    data = registry.list_capabilities(db, actor=READER, engine="mysql")
    objects = next(row for row in data["items"] if row["capability_id"] == "operations.object.read")
    assert objects["required_actions"] == ("operations.read",)
    assert objects["authorized_engines"] == ["mysql"]
    oracle = registry.list_capabilities(db, actor=READER, engine="oracle")
    # Object read now uses a shared GET action, so Oracle is authorized too (engine support still declared).
    assert "operations.object.read" in {row["capability_id"] for row in oracle["items"]}
    unfiltered = registry.list_capabilities(db, actor=READER)
    objects = next(row for row in unfiltered["items"] if row["capability_id"] == "operations.object.read")
    assert "mysql" in objects["authorized_engines"]
    assert set(objects["authorized_engines"]) == {"oracle", "mysql", "postgresql", "sqlserver"}


def test_composite_binding_requires_every_endpoint_permission(db):
    _sync(db)
    _grant_only(db, "platform.security")
    entry = next(row for row in registry.validate_registry() if row["capability_id"] == "platform.user.read")
    actions = registry.binding_actions(entry, engine="mysql")
    assert len(actions) > 1
    assert not registry._authorized(entry, {actions[0]}, engine="mysql")
    assert registry._authorized(entry, set(actions), engine="mysql")


def test_scoped_settings_user_cannot_mutate_global_registry(db):
    _sync(db)
    _grant_only(db, "platform.settings")
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"))
    db.commit()
    before = db.query(SecurityAuditEvent).count()
    with pytest.raises(registry.CapabilityError, match="scope_denied"):
        registry.sync_definitions(db, actor=READER)
    with pytest.raises(registry.CapabilityError, match="scope_denied"):
        registry.set_enabled(db, "inspection.run.query", enabled=False, actor=READER)
    assert db.get(DBCapabilityDefinition, "inspection.run.query").enabled == 1
    assert db.query(SecurityAuditEvent).count() == before


def test_matrix_cannot_present_termination_as_ready_even_for_admin(db):
    _sync(db)
    asset = db.get(AssetInstance, 10)
    asset.attributes = {"connection_test_status": "success"}
    db.commit()
    matrix = registry.execute_read(db, "operations.capability_matrix.read", 1, actor=ADMIN, instance_id=10)
    assert matrix["selected_instance"]["connection_status"] == "success"
    terminate = next(item for item in matrix["items"] if item["key"] == "runtime.terminate_session")
    assert set(registry.effective_maturity("operations.termination.execute").values()) == {"unsupported"}
    for cell in terminate["engines"].values():
        assert cell["mode"] == "unsupported"
        assert cell["availability"] == "unsupported"
        assert cell["ready_instances"] == 0


def test_rejected_read_security_event_is_bounded_and_has_no_raw_payload(db, caplog):
    _sync(db)
    before = db.query(DBCapabilityPlan).count()
    raw = "sensitive arbitrary payload not for logs"
    with pytest.raises(registry.CapabilityError, match="request_supplied_handler_rejected"):
        registry.execute_read(db, "inspection.run.query", 1, actor=ADMIN,
                              instance_id=10, params={"handler": raw})
    db.rollback()
    events = [record.security_event for record in caplog.records if hasattr(record, "security_event")]
    assert events == [{"actor_user_id": 1, "capability_id": "inspection.run.query",
                       "definition_version": 1, "instance_id": 10,
                       "reason": "request_supplied_handler_rejected"}]
    assert raw not in str(events) + caplog.text
    assert db.query(DBCapabilityPlan).count() == before


def test_scoped_reads_use_real_asset_scope_not_caller_target_hint(db):
    _sync(db)
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"))
    db.commit()
    for instance_id in (None, 20):
        with pytest.raises(registry.CapabilityError, match="scope_denied"):
            registry.execute_read(db, "inspection.run.query", 1, actor=READER, instance_id=instance_id)
    for identity in (10, 20):
        db.add(DBInspectionJob(id=f"failed-{identity}", instance_id=identity, db_type="mysql",
                               trigger_type="manual", policy_id=1, policy_snapshot_json={},
                               policy_revision="0" * 64, target_fingerprint="0" * 64,
                               status="failed", stage="failed", timeout_seconds=900,
                               attempt=1, cancel_requested=0, created_at=datetime.now(UTC).replace(tzinfo=None)))
    db.commit()
    data = registry.execute_read(db, "inspection.run.query", 1, actor=READER, instance_id=10)
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == "failed-10"
    assert data["items"][0]["score"] is None
    matrix = registry.execute_read(db, "operations.capability_matrix.read", 1, actor=READER, instance_id=10)
    assert [item["id"] for item in matrix["instances"]] == [10]
    assert matrix["summary"]["execution_permission"] is False
    with pytest.raises(registry.CapabilityError, match="scope_denied"):
        registry.execute_read(db, "inspection.template.read", 1, actor=READER, instance_id=10)


def test_template_read_does_not_seed_defaults_or_plan_rows(db):
    _sync(db)
    assert registry.execute_read(db, "inspection.template.read", 1, actor=READER)["total"] == 0
    assert db.query(DBInspectionPolicy).count() == 0
    db.add(DBInspectionPolicy(policy_key="local-test", name="Existing template", db_types_json=["mysql"], enabled=1))
    db.commit()
    data = registry.execute_read(db, "inspection.template.read", 1, actor=READER, params={"db_type": "mysql"})
    assert data["total"] == 1
    assert data["items"][0]["key"] == "local-test"
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("params,code", [
    ({"page": 0}, "invalid_params"), ({"page_size": 201}, "invalid_params"),
    ({"page": True}, "invalid_params"), ({"page": "1"}, "invalid_params"),
    ({"bogus": "x"}, "invalid_params"), ({"page": float("nan")}, "invalid_params"),
    ({"handler": "os.system"}, "request_supplied_handler_rejected"),
    ({"nested": [{"command": "do not execute"}]}, "plan_raw_payload_rejected"),
    ({"SQL": "select 1"}, "plan_raw_payload_rejected"),
])
def test_raw_handlers_scripts_and_unbounded_inputs_rejected(db, params, code):
    _sync(db)
    with pytest.raises(registry.CapabilityError, match=code):
        registry.execute_read(db, "inspection.run.query", 1, actor=ADMIN, params=params)


def test_registry_has_no_dynamic_import_or_eval_path():
    tree = ast.parse(inspect.getsource(registry))
    forbidden = {"eval", "exec", "__import__", "import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            assert name not in forbidden
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "importlib" not in ast.unparse(node)
