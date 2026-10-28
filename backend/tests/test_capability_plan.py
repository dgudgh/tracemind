"""Persisted T03 workflows against isolated SQLite and the actual three read adapters."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import ast
import inspect
from threading import Event

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.access_control import UserAccessRole, UserAssetScope
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.db_instance import DBInstance
from app.models.inspection_policy import DBInspectionPolicy
from app.models.user import User
from app.services import access_control, capability_registry as registry, plan_service as plans
from app.services.capability_handler import HANDLERS


ADMIN, READER, OTHER = {"user_id": 1}, {"user_id": 2}, {"user_id": 3}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plans.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(session)}
        session.add_all([
            User(id=1, username="plan-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="plan-reader", password_hash="unused", role="user", status="active"),
            User(id=3, username="plan-other", password_hash="unused", role="user", status="active"),
            UserAccessRole(user_id=2, role_id=roles["viewer"].id),
            UserAccessRole(user_id=3, role_id=roles["viewer"].id),
        ])
        for identity in (10, 20):
            session.add(DBInstance(id=identity, instance_name=f"plan-{identity}", db_type="mysql", host="127.0.0.1",
                                   port=3306, username="unused", password_cipher="env://SYNTHETIC_PLAN_PASSWORD", enabled=1))
            session.add(AssetInstance(id=identity, native_instance_id=identity, stable_key=f"plan:{identity}",
                                      name=f"plan-{identity}", db_type="mysql", ip="127.0.0.1", port=3306,
                                      host_id=identity, region_id=1, site_id=1, zone_id=1, env_id=1, status="active"))
        session.commit()
        registry.sync_definitions(session, actor=ADMIN)
        yield session
    engine.dispose()


def create(db, capability="inspection.run.query", actor=READER, **kwargs):
    return plans.create_plan(db, actor=actor, capability_id=capability,
                             definition_version=kwargs.pop("definition_version", registry._entry(capability)["version"]), **kwargs)


def test_create_freezes_snapshot_steps_revision_without_placeholder_approval(db):
    result = create(db)
    row = db.get(DBCapabilityPlan, result["plan_id"])
    assert result["status"] == "validated" and result["outcome"] is None
    assert row.approval_state == "not_required" and row.approval_binding_hash is None
    assert row.requested_by == 2
    assert db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id).one().binding_hash == plans._hash(row.capability_snapshot_json)
    assert len(result["steps"]) == 1 and result["steps"][0]["status"] == "pending"
    assert db.query(DBInspectionPolicy).count() == 0


@pytest.mark.parametrize("capability", ["inspection.run.query", "inspection.template.read", "operations.capability_matrix.read"])
def test_three_real_read_handlers_persist_results_and_can_be_read_in_new_session(db, capability):
    result = create(db, capability)
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["status"] == "settled" and completed["outcome"] == "succeeded"
    assert completed["steps"][0]["status"] == "succeeded"
    with Session(db.get_bind(), autoflush=False) as fresh:
        read = plans.read_plan(fresh, result["plan_id"], actor=READER)
        assert read["result_ref"] == completed["result_ref"]
        assert read["steps"][0]["result_ref"]["kind"] == "capability_read"
    assert db.query(DBInspectionPolicy).count() == 0


def test_typed_dag_uses_real_prior_total_not_string_expression(db):
    db.add(DBInspectionPolicy(policy_key="one-local-policy", name="Local", db_types_json=["mysql"], enabled=1))
    db.commit()
    steps = [
        {"seq": 1, "capability_id": "inspection.template.read", "definition_version": 1},
        {"seq": 2, "capability_id": "inspection.run.query", "definition_version": 1, "depends_on": [1],
         "input_refs": {"page_size": {"seq": 1, "schema": "inspection.policy_list/v1", "field": "total", "type": "integer"}}},
    ]
    result = create(db, "inspection.template.read", steps=steps)
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["outcome"] == "succeeded"
    assert completed["steps"][1]["result_ref"]["inline"]["page_size"] == 1


def test_same_request_is_idempotent_explicit_key_conflicts_on_changed_params(db):
    first = create(db, idempotency_key="manual-key")
    again = create(db, idempotency_key="manual-key")
    assert again["plan_id"] == first["plan_id"] and again["reused"]
    with pytest.raises(plans.PlanError, match="idempotency_conflict"):
        create(db, idempotency_key="manual-key", params={"page": 2})
    db.rollback()
    assert db.query(DBCapabilityPlan).count() == 1


def test_deploying_new_definition_does_not_create_another_request(db, monkeypatch):
    first = create(db)
    definitions = deepcopy(registry.CAPABILITY_REGISTRY)
    next(row for row in definitions if row["capability_id"] == "inspection.run.query")["version"] = 2
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", definitions)
    registry.sync_definitions(db, actor=ADMIN)
    reused = create(db, definition_version=2)
    assert reused["plan_id"] == first["plan_id"] and reused["definition_version"] == 1
    blocked = plans.execute_plan(db, first["plan_id"], actor=READER)
    assert blocked["outcome"] == "blocked" and blocked["outcome_kind"] == "stale_definition_version"


def test_new_request_with_stale_version_is_rejected_before_plan_rows(db):
    with pytest.raises(plans.PlanError, match="stale_definition_version"):
        create(db, definition_version=999)
    assert db.query(DBCapabilityPlan).count() == 0


def test_new_budget_and_schema_cannot_block_replay_but_still_gate_new_plans(db, monkeypatch):
    steps = [{"seq": seq, "capability_id": "inspection.run.query", "definition_version": 1} for seq in (1, 2)]
    first = create(db, steps=steps)
    declarations = deepcopy(registry.CAPABILITY_REGISTRY)
    entry = next(row for row in declarations if row["capability_id"] == "inspection.run.query")
    entry["version"], entry["budget"]["max_steps"], entry["output_schema"] = 2, 1, "inspection.changed/v2"
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", declarations)
    registry.sync_definitions(db, actor=ADMIN)
    upgraded = [{**step, "definition_version": 2} for step in steps]
    replay = create(db, steps=upgraded, definition_version=2)
    assert replay["plan_id"] == first["plan_id"] and replay["reused"]
    assert replay["definition_version"] == 1
    upgraded[0]["params"] = {"page": 2}
    with pytest.raises(plans.PlanError, match="plan_too_many_steps"):
        create(db, steps=upgraded, definition_version=2)
    db.rollback()
    assert db.query(DBCapabilityPlan).count() == 1


def test_existing_explicit_key_is_not_reparented_by_a_later_automatic_plan(db):
    explicit = create(db, idempotency_key="first-explicit")
    automatic = create(db)
    assert explicit["plan_id"] != automatic["plan_id"]
    replay = create(db, idempotency_key="first-explicit")
    assert replay["plan_id"] == explicit["plan_id"] and replay["reused"]
    assert replay["parent_plan_id"] is None


def test_explicit_rerun_has_new_identity_and_no_old_result(db):
    first = create(db)
    plans.execute_plan(db, first["plan_id"], actor=READER)
    rerun = create(db, parent_plan_id=first["plan_id"], idempotency_key="new-attempt")
    assert rerun["plan_id"] != first["plan_id"] and rerun["attempt"] == 2
    assert rerun["parent_plan_id"] == first["plan_id"] and rerun["result_ref"] is None
    assert all(step["result_ref"] is None for step in rerun["steps"])
    assert create(db, parent_plan_id=first["plan_id"], idempotency_key="new-attempt")["reused"]
    with pytest.raises(plans.PlanError, match="parent_plan_already_retried"):
        create(db, parent_plan_id=first["plan_id"], idempotency_key="another-attempt")


def test_new_explicit_key_finds_matching_automatic_parent(db):
    first = create(db)
    plans.execute_plan(db, first["plan_id"], actor=READER)
    rerun = create(db, idempotency_key="explicit-rerun-header")
    assert rerun["attempt"] == 2 and rerun["parent_plan_id"] == first["plan_id"]
    assert rerun["result_ref"] is None
    assert create(db, idempotency_key="explicit-rerun-header")["plan_id"] == rerun["plan_id"]


def test_client_bound_and_termination_intents_do_not_become_executable(db, monkeypatch):
    calls = []
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", lambda ctx: calls.append(ctx))
    # platform.dashboard.read is wired as of C13 batch 2; use a still-client-bound read.
    for capability in ("performance.suggestion.refresh", "operations.termination.execute"):
        result = create(db, capability, actor=ADMIN)
        assert result["outcome"] == "unsupported"
        assert all(row["status"] == "skipped" for row in result["steps"])
        assert plans.execute_plan(db, result["plan_id"], actor=ADMIN)["outcome"] == "unsupported"
    assert calls == []


def test_missing_target_is_needs_input_not_an_executed_run(db):
    result = create(db, "inspection.run.execute", actor=ADMIN)
    assert result["status"] == "settled" and result["outcome"] == "needs_input"
    assert result["missing_params"] == ["instance_id", "policy_id"]
    assert result["steps"][0]["status"] == "skipped"


@pytest.mark.parametrize("approval", ["pending", "approved"])
def test_unwired_writer_never_trusts_a_database_approval_flag(db, monkeypatch, approval):
    result = create(db, "asset.discovery.execute", actor=ADMIN, instance_id=10)
    assert result["status"] == "validated" and result["approval_state"] == "pending"
    plan = db.get(DBCapabilityPlan, result["plan_id"])
    assert plan.approval_binding_hash
    plan.approval_state = approval
    db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("pending/unimplemented approval must never call an executor")
    monkeypatch.setattr(registry, "execute_read", forbidden)
    blocked = plans.execute_plan(db, result["plan_id"], actor=ADMIN)
    assert blocked["outcome"] == "blocked" and blocked["outcome_kind"] == "approval_not_available_until_u03"
    assert all(step["status"] == "skipped" for step in blocked["steps"])


@pytest.mark.parametrize("change,reason,outcome", [
    ("target", "target_changed", "blocked"),
    ("disabled", "capability_disabled", "unsupported"),
    ("instance_disabled", "target_unavailable", "blocked"),
])
def test_execution_rechecks_live_target_and_registry(db, change, reason, outcome):
    result = create(db, instance_id=10)
    if change == "disabled":
        registry.set_enabled(db, "inspection.run.query", enabled=False, actor=ADMIN)
    else:
        row = db.get(DBInstance, 10)
        if change == "target":
            row.host = "127.0.0.2"
        else:
            row.enabled = 0
        db.commit()
    blocked = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert blocked["outcome_kind"] == reason and blocked["outcome"] == outcome
    assert not blocked["result_ref"]


def test_scope_revocation_and_other_owner_cannot_read_or_execute(db):
    result = create(db, instance_id=10)
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        plans.read_plan(db, result["plan_id"], actor=OTHER)
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=20, access_level="read"))
    db.commit()
    with pytest.raises(plans.PlanError, match="scope_denied"):
        plans.read_plan(db, result["plan_id"], actor=READER)
    with pytest.raises(plans.PlanError, match="scope_denied"):
        plans.execute_plan(db, result["plan_id"], actor=READER)


def test_account_and_action_revocation_do_not_reuse_saved_authorization(db):
    result = create(db)
    db.query(UserAccessRole).filter_by(user_id=2).delete()
    db.commit()
    with pytest.raises(plans.PlanError, match="permission_denied"):
        plans.execute_plan(db, result["plan_id"], actor=READER)


def test_unauthorized_create_never_persists_intent_or_uses_actor_role(db):
    db.query(UserAccessRole).filter_by(user_id=3).delete()
    db.commit()
    with pytest.raises(plans.PlanError, match="permission_denied"):
        create(db, actor={"user_id": 3, "role": "admin"})
    assert db.query(DBCapabilityPlan).count() == 0


def test_frozen_snapshot_cannot_be_edited_behind_revision(db):
    result = create(db)
    row = db.get(DBCapabilityPlan, result["plan_id"])
    changed = deepcopy(row.capability_snapshot_json)
    changed["steps"][0]["params"]["page"] = 2
    row.capability_snapshot_json = changed
    db.commit()
    with pytest.raises(plans.PlanError, match="plan_snapshot_invalid"):
        plans.execute_plan(db, result["plan_id"], actor=READER)


def test_result_identity_cannot_be_retargeted_by_changing_row_instance(db):
    result = create(db, instance_id=10)
    plans.execute_plan(db, result["plan_id"], actor=READER)
    row = db.get(DBCapabilityPlan, result["plan_id"])
    row.instance_id = 20
    db.commit()
    with pytest.raises(plans.PlanError, match="plan_snapshot_invalid"):
        plans.read_plan(db, result["plan_id"], actor=READER)


def test_read_success_cannot_be_declared_and_missing_evidence_cannot_settle(db):
    result = create(db)
    plan = db.get(DBCapabilityPlan, result["plan_id"])
    assert not plans.STATUSES & plans.OUTCOMES
    with pytest.raises(plans.PlanError, match="declared_outcome_must_be_a_declaration"):
        plans.declare_outcome(db, plan, "succeeded", declared_by="test", reason="not evidence")
    with pytest.raises(plans.PlanError, match="plan_only_requires_plan_capability"):
        plans.declare_outcome(db, plan, "plan_only", declared_by="test", reason="not a plan handler")
    with pytest.raises(plans.PlanError, match="invalid_evidence_outcome"):
        plans.settle(db, plan, "succeeded")


def test_result_missing_schema_is_unknown_not_success(db, monkeypatch):
    result = create(db)
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", lambda ctx: {"items": []})
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["outcome"] == "unknown" and completed["outcome_kind"] == "result_unknown"
    assert completed["steps"][0]["result_ref"] is None


def test_late_valid_response_is_unknown_not_success(db, monkeypatch):
    result = create(db)
    clock = iter((0.0, 61.0))
    monkeypatch.setattr(plans, "monotonic", lambda: next(clock))
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["outcome"] == "unknown" and completed["outcome_kind"] == "timeout"
    assert completed["steps"][0]["result_ref"] is None


def test_permission_revoked_during_read_cannot_publish_or_return_payload(db, monkeypatch):
    result = create(db)
    original = HANDLERS["inspection.list_jobs"]
    def revoked(ctx):
        value = original(ctx)
        ctx.db.query(UserAccessRole).filter_by(user_id=2).delete()
        ctx.db.commit()
        return value
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", revoked)
    with pytest.raises(plans.PlanError, match="permission_denied"):
        plans.execute_plan(db, result["plan_id"], actor=READER)
    row = db.query(DBCapabilityPlanStep).filter_by(plan_id=result["plan_id"]).one()
    assert row.result_ref_json is None
    assert row.status == "failed" and row.outcome_kind == "permission_denied"


def test_actual_typed_reference_is_revalidated_against_target_parameter_bounds(db):
    steps = [
        {"seq": 1, "capability_id": "inspection.template.read", "definition_version": 1},
        {"seq": 2, "capability_id": "inspection.run.query", "definition_version": 1, "depends_on": [1],
         "input_refs": {"page_size": {"seq": 1, "schema": "inspection.policy_list/v1", "field": "total", "type": "integer"}}},
    ]
    result = create(db, "inspection.template.read", steps=steps)
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["outcome"] == "partial"
    assert completed["steps"][1]["outcome_kind"] == "invalid_params"
    assert completed["steps"][1]["result_ref"] is None


def test_second_failure_preserves_first_result_and_skips_later_steps(db, monkeypatch):
    steps = [{"seq": seq, "capability_id": "inspection.run.query", "definition_version": 1,
              "depends_on": [seq - 1] if seq > 1 else []} for seq in range(1, 4)]
    result = create(db, steps=steps)
    original = HANDLERS["inspection.list_jobs"]
    calls = []
    def fail_second(ctx):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("sensitive connection text must not be persisted")
        return original(ctx)
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", fail_second)
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert completed["outcome"] == "partial"
    assert [step["status"] for step in completed["steps"]] == ["succeeded", "failed", "skipped"]
    assert len(calls) == 2 and completed["result_ref"]["steps"] == [1]
    assert "sensitive connection" not in str(completed)


def test_fail_fast_distinguishes_unrelated_branch_from_failed_dependency(db, monkeypatch):
    steps = [{"seq": seq, "capability_id": "inspection.run.query", "definition_version": 1,
              "depends_on": [1] if seq == 3 else []} for seq in (1, 2, 3)]
    result = create(db, steps=steps)
    original = HANDLERS["inspection.list_jobs"]
    calls = []
    def second_fails(ctx):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("not persisted")
        return original(ctx)
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", second_fails)
    completed = plans.execute_plan(db, result["plan_id"], actor=READER)
    assert len(calls) == 2
    assert completed["steps"][2]["status"] == "skipped"
    assert completed["steps"][2]["outcome_kind"] == "stopped_after_failure"


def test_storage_failure_is_not_fabricated_as_empty_success(db, monkeypatch):
    result = create(db)
    def fail(ctx):
        raise OperationalError("test", {}, RuntimeError("unavailable"))
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", fail)
    with pytest.raises(plans.PlanError, match="plan_storage_unavailable"):
        plans.execute_plan(db, result["plan_id"], actor=READER)
    read = plans.read_plan(db, result["plan_id"], actor=READER)
    assert read["status"] == "running" and read["outcome"] is None
    assert not read["result_ref"]


def test_cancel_before_run_is_factual_and_never_calls_handler(db, monkeypatch):
    result = create(db)
    calls = []
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", lambda ctx: calls.append(ctx))
    cancelled = plans.cancel_plan(db, result["plan_id"], actor=READER)
    assert cancelled["outcome"] == "cancelled"
    assert plans.execute_plan(db, result["plan_id"], actor=READER)["outcome"] == "cancelled"
    assert calls == []


def test_concurrent_submission_has_one_plan(db):
    bind = db.get_bind()
    with ThreadPoolExecutor(max_workers=2) as pool:
        def submit():
            with Session(bind, autoflush=False) as session:
                return create(session, idempotency_key="same-concurrent-key")
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert len({row["plan_id"] for row in results}) == 1
    assert sorted(row["reused"] for row in results) == [False, True]
    assert db.query(DBCapabilityPlan).count() == 1


def test_concurrent_execute_and_running_cancel_do_not_duplicate_or_lie(db, monkeypatch):
    result = create(db)
    entered, release = Event(), Event()
    original = HANDLERS["inspection.list_jobs"]
    calls = []
    def waiting(ctx):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(ctx)
    monkeypatch.setitem(HANDLERS, "inspection.list_jobs", waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def execute():
            with Session(db.get_bind(), autoflush=False) as session:
                return plans.execute_plan(session, result["plan_id"], actor=READER)
        future = pool.submit(execute)
        try:
            assert entered.wait(10)
            duplicate = plans.execute_plan(db, result["plan_id"], actor=READER)
            assert duplicate["status"] == "running" and duplicate["reused"]
            with pytest.raises(plans.PlanError, match="running_cancellation_not_supported"):
                plans.cancel_plan(db, result["plan_id"], actor=READER)
            db.rollback()
        finally:
            release.set()
        assert future.result(timeout=15)["outcome"] == "succeeded"
    assert len(calls) == 1


@pytest.mark.parametrize("params,code", [
    ({"sql": "select 1"}, "plan_raw_payload_rejected"),
    ({"handler": "os.system"}, "request_supplied_handler_rejected"),
    ({"nested": {"password": "do-not-store"}}, "plan_sensitive_payload_rejected"),
    ({"page": True}, "invalid_params"),
])
def test_unsafe_or_invalid_inputs_are_rejected_before_persisting(db, params, code):
    with pytest.raises(plans.PlanError, match=code):
        create(db, params=params)
    assert db.query(DBCapabilityPlan).count() == 0


def test_only_two_named_writers_assign_plan_outcome():
    tree = ast.parse(inspect.getsource(plans))
    writers = set()
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Attribute) and node.attr == "outcome" and isinstance(node.ctx, ast.Store):
                    writers.add(function.name)
    assert writers == {"declare_outcome", "settle"}
