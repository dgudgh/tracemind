"""C10 bindings through real isolated Sessions, revisions, approval and readers."""
from copy import deepcopy
import socket

import pytest
from sqlalchemy.orm import Session

from app.models.access_control import UserAssetScope
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_registry as registry, plan_governance as governance, plan_service as plans
from app.services import workflow_binding as bindings, workflow_library as library
from test_capability_plan import ADMIN, READER, db  # noqa: F401
from test_inspection_plan_adapter import env as inspection_env  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("C10 binding tests must not access live DB/network")
    monkeypatch.setattr(socket.socket, "connect", deny)


def candidate(name="inspection.jobs.list", *, slots=None, scope=None):
    return library.compile_workflow(name, 1, slots={} if slots is None else slots,
                                    scope={"mode": "global"} if scope is None else scope)


def inputs(value, index=0):
    return {**deepcopy(value["plan_inputs"][index]), "workflow_binding": bindings.make_binding(value, index)}


def save(db, value=None, **kwargs):
    return plans.create_plan(db, actor=READER, **inputs(candidate() if value is None else value), **kwargs)


def test_make_binding_is_canonical_detached_and_never_contains_sibling_scope():
    many = candidate(slots={"page_size": 7}, scope={"mode": "explicit", "targets": [
        {"instance_id": 10, "slots": {"page": 2}}, {"instance_id": 20, "slots": {"page": 3}},
    ]})
    first, second = bindings.make_binding(many, 0), bindings.make_binding(many, 1)
    assert set(first) == {"workflow_id", "workflow_version", "definition_hash", "recipe_snapshot", "input", "candidate_hash"}
    assert first["input"]["scope"] == {"mode": "explicit", "targets": [{"instance_id": 10}]}
    assert second["input"]["scope"] == {"mode": "explicit", "targets": [{"instance_id": 20}]}
    assert first["input"]["slots"]["page"] == 2 and second["input"]["slots"]["page"] == 3
    assert first["candidate_hash"] != many["candidate_hash"]
    equivalent = candidate(slots={"page": 2, "page_size": 7}, scope={"mode": "explicit", "targets": [{"instance_id": 10}]})
    assert bindings.make_binding(equivalent, 0) == first
    first["input"]["scope"]["targets"].clear()
    assert len(many["scope"]["targets"]) == 2 and bindings.make_binding(many, 0) != first


@pytest.mark.parametrize("index", [-1, True, 1.2, "0", 1])
def test_make_binding_rejects_invalid_indexes(index):
    with pytest.raises(bindings.WorkflowBindingError):
        bindings.make_binding(candidate(), index)


def test_make_binding_rejects_a_modified_or_incomplete_candidate():
    altered = candidate()
    altered["plan_inputs"][0]["steps"][0]["params"]["page"] = 99
    with pytest.raises(bindings.WorkflowBindingError):
        bindings.make_binding(altered, 0)
    incomplete = candidate("inspection.collect", scope={"mode": "explicit", "targets": [{"instance_id": 10}]})
    with pytest.raises(bindings.WorkflowBindingError):
        bindings.make_binding(incomplete, 0)


@pytest.mark.parametrize("name,scope", [
    ("inspection.jobs.list", {"mode": "global"}),
    ("inspection.templates.list", {"mode": "global"}),
    ("operations.capabilities.list", {"mode": "global"}),
    ("inspection.context.read", {"mode": "explicit", "targets": [{"instance_id": 10}]}),
])
def test_real_creation_revision_execution_and_fresh_session_readback(db, name, scope):
    value = candidate(name, scope=scope)
    saved = save(db, value)
    expected = bindings.make_binding(value, 0)
    assert saved["workflow_binding"] == expected and saved["outcome"] is None
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id, revision=1).one()
    assert revision.snapshot_json["workflow_binding"] == expected
    assert revision.binding_hash == plans._hash(row.capability_snapshot_json)
    completed = plans.execute_plan(db, row.plan_id, actor=READER)
    assert completed["outcome"] == "succeeded" and all(step["result_ref"] for step in completed["steps"])
    with Session(db.get_bind(), autoflush=False) as fresh:
        read = plans.read_plan(fresh, row.plan_id, actor=READER)
        assert read["workflow_binding"] == expected and read["result_ref"] == completed["result_ref"]
    saved["workflow_binding"]["input"]["slots"].clear()
    assert plans.read_plan(db, row.plan_id, actor=READER)["workflow_binding"] == expected


@pytest.mark.parametrize("field", ["steps", "target", "approval", "ttl", "capability", "version", "params"])
def test_pre_freeze_kwargs_cannot_escape_compiled_recipe(db, field):
    request = inputs(candidate())
    if field == "steps":
        request["steps"][0]["params"]["page"] = 2
    elif field == "target":
        request["instance_id"] = 10
    elif field == "approval":
        request["request_approval"] = True
    elif field == "ttl":
        request["valid_for_seconds"] = 61
    elif field == "capability":
        request["capability_id"] = "inspection.template.read"
    elif field == "version":
        request["definition_version"] = True
    else:
        request["params"] = {"page": 2}
    with pytest.raises((plans.PlanError, registry.CapabilityError)):
        plans.create_plan(db, actor=READER, **request)
    db.rollback()
    assert db.query(DBCapabilityPlan).count() == 0


def test_request_hash_distinguishes_workflow_from_legacy_without_changing_legacy(db):
    bound = save(db)
    legacy_request = inputs(candidate())
    legacy_request.pop("workflow_binding")
    legacy = plans.create_plan(db, actor=READER, **legacy_request)
    bound_row, old_row = (db.get(DBCapabilityPlan, item["plan_id"]) for item in (bound, legacy))
    assert bound_row.capability_snapshot_json["request_hash"] != old_row.capability_snapshot_json["request_hash"]
    assert "workflow_binding" not in legacy and "workflow_binding" not in old_row.capability_snapshot_json
    business = [{key: value for key, value in step.items() if key not in {"definition_version", "title"}}
                for step in legacy_request["steps"]]
    assert old_row.capability_snapshot_json["request_hash"] == plans._hash({
        "actor": READER["user_id"], "capability_id": legacy_request["capability_id"], "instance_id": None,
        "target": None, "steps": business, "valid_for_seconds": 3600,
    })


def test_generic_revision_cannot_drop_workflow_or_edit_fixed_dag(db):
    saved = save(db)
    legacy = inputs(candidate())
    legacy.pop("workflow_binding")
    with pytest.raises(plans.PlanError, match="workflow_revision_requires_recipe"):
        governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1, **legacy)
    db.rollback()
    altered = inputs(candidate())
    altered["steps"][0]["retries"] = 1
    with pytest.raises(plans.PlanError, match="workflow_input_mismatch"):
        governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1, **altered)
    db.rollback()
    assert plans.read_plan(db, saved["plan_id"], actor=READER)["revision"] == 1


def test_workflow_revise_then_execute_then_explicit_rerun_uses_same_business_identity(db):
    saved = save(db, idempotency_key="workflow-original")
    changed = candidate(slots={"page": 2})
    revised = governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1, **inputs(changed))
    before = deepcopy(db.get(DBCapabilityPlan, saved["plan_id"]).capability_snapshot_json)
    assert revised["revision"] == 2 and before["workflow_binding"] == bindings.make_binding(changed, 0)
    assert plans.execute_plan(db, saved["plan_id"], actor=READER)["outcome"] == "succeeded"
    old_result = deepcopy(plans.read_plan(db, saved["plan_id"], actor=READER)["result_ref"])
    rerun = save(db, changed, parent_plan_id=saved["plan_id"], idempotency_key="workflow-explicit-rerun")
    assert rerun["plan_id"] != saved["plan_id"] and rerun["parent_plan_id"] == saved["plan_id"]
    assert db.get(DBCapabilityPlan, rerun["plan_id"]).capability_snapshot_json["request_hash"] == before["request_hash"]
    assert rerun["result_ref"] is None and all(step["result_ref"] is None for step in rerun["steps"])
    assert plans.execute_plan(db, rerun["plan_id"], actor=READER)["outcome"] == "succeeded"
    assert plans.read_plan(db, saved["plan_id"], actor=READER)["result_ref"] == old_result
    assert save(db, changed, parent_plan_id=saved["plan_id"], idempotency_key="workflow-explicit-rerun")["reused"]


def test_binding_is_not_changed_by_invocation_admission_hash_or_fresh_expiry(db, monkeypatch):
    from datetime import timedelta
    first = save(db, idempotency_key="workflow-one", admission_hash="1" * 64)
    plans.execute_plan(db, first["plan_id"], actor=READER)
    old_now = plans._now
    monkeypatch.setattr(plans, "_now", lambda: old_now() + timedelta(seconds=3))
    second = save(db, idempotency_key="workflow-two", parent_plan_id=first["plan_id"], admission_hash="2" * 64)
    snapshots = [db.get(DBCapabilityPlan, item["plan_id"]).capability_snapshot_json for item in (first, second)]
    assert snapshots[0]["request_hash"] == snapshots[1]["request_hash"]
    assert snapshots[0]["validity"]["expires_at"] != snapshots[1]["validity"]["expires_at"]
    assert first["workflow_binding"] == second["workflow_binding"]


def test_old_workflow_remains_readable_without_current_catalog_but_execution_is_blocked(db, monkeypatch):
    saved = save(db)
    calls = []
    def retired(*args, **kwargs):
        calls.append(True)
        raise library.WorkflowError("workflow_not_found")
    monkeypatch.setattr(library, "get_workflow", retired)
    with Session(db.get_bind(), autoflush=False) as fresh:
        assert plans.read_plan(fresh, saved["plan_id"], actor=READER)["workflow_binding"] == saved["workflow_binding"]
        assert calls == []
        done = plans.execute_plan(fresh, saved["plan_id"], actor=READER)
        assert done["outcome"] != "succeeded" and done["outcome_kind"] == "workflow_not_found"
        assert all(step["result_ref"] is None for step in done["steps"])
        assert calls


def test_recipe_hash_drift_denies_new_execution_without_replacing_stored_binding(db, monkeypatch):
    saved = save(db)
    original = library.get_workflow
    def drift(*args, **kwargs):
        value = original(*args, **kwargs)
        value["title"] = "unpublished replacement"
        value["definition_hash"] = library._hash({key: item for key, item in value.items() if key != "definition_hash"})
        return value
    monkeypatch.setattr(library, "get_workflow", drift)
    result = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert result["outcome"] != "succeeded"
    assert plans.read_plan(db, saved["plan_id"], actor=READER)["workflow_binding"] == saved["workflow_binding"]


@pytest.mark.parametrize("tamper", ["recipe", "candidate", "scope", "step", "extra_binding", "extra_read_param"])
def test_self_consistent_revision_hash_does_not_hide_binding_tampering(db, tamper):
    saved = save(db)
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    snapshot = deepcopy(plan.capability_snapshot_json)
    binding = snapshot["workflow_binding"]
    if tamper == "recipe":
        binding["recipe_snapshot"]["title"] = "forged"
    elif tamper == "candidate":
        binding["candidate_hash"] = "0" * 64
    elif tamper == "scope":
        binding["input"]["scope"] = {"mode": "explicit", "targets": [{"instance_id": 10}, {"instance_id": 20}]}
    elif tamper == "extra_binding":
        binding["operation"] = "rerun"
    else:
        snapshot["steps"][0]["params"]["page" if tamper == "step" else "runtime_digest"] = 2 if tamper == "step" else "a" * 64
        step = db.query(DBCapabilityPlanStep).filter_by(plan_id=plan.plan_id).one()
        step.precondition_json = deepcopy(snapshot["steps"][0])
    plan.capability_snapshot_json = snapshot
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=plan.plan_id).one()
    revision.snapshot_json, revision.binding_hash = deepcopy(snapshot), plans._hash(snapshot)
    db.commit()
    with pytest.raises(plans.PlanError, match="plan_snapshot_invalid"):
        plans.read_plan(db, saved["plan_id"], actor=READER)


def test_current_scope_and_actual_target_are_rechecked_for_workflow(db):
    value = candidate(scope={"mode": "explicit", "targets": [{"instance_id": 10}]})
    saved = save(db, value)
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=20, access_level="read"))
    db.commit()
    with pytest.raises((plans.PlanError, registry.CapabilityError)):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    db.query(UserAssetScope).filter_by(user_id=2).delete()
    db.get(DBInstance, 10).host = "changed.invalid"
    db.commit()
    outcome = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert outcome["outcome"] != "succeeded" and outcome["outcome_kind"] == "target_changed"


def test_commit_false_remains_atomic_in_original_transaction(db):
    saved = save(db, commit=False)
    assert saved["workflow_binding"]
    with Session(db.get_bind(), autoflush=False) as other:
        assert other.query(DBCapabilityPlan).count() == 0
    db.rollback()
    assert db.query(DBCapabilityPlan).count() == 0 and db.query(DBCapabilityPlanRevision).count() == 0


@pytest.mark.parametrize("value", [None, {}, [], False, 0, "invalid"])
def test_current_binding_errors_use_the_plan_service_error_contract(value):
    with pytest.raises(plans.PlanError) as caught:
        bindings.check_current(value)
    assert caught.value.code == "workflow_binding_invalid"


def test_malformed_recipe_is_not_an_untyped_attribute_error(db):
    request = inputs(candidate())
    binding = request["workflow_binding"]
    binding["recipe_snapshot"]["steps"][0] = None
    digest = library._hash({key: item for key, item in binding["recipe_snapshot"].items() if key != "definition_hash"})
    binding["definition_hash"] = binding["recipe_snapshot"]["definition_hash"] = digest
    with pytest.raises(plans.PlanError, match="workflow_binding_invalid"):
        plans.create_plan(db, actor=READER, **request)
    db.rollback()
    assert db.query(DBCapabilityPlan).count() == 0


def test_inspection_binding_approval_revision_freezing_execution_and_rerun(inspection_env, monkeypatch):
    from app.services import plan_queue
    env = inspection_env
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    approver = User(username="binding-approver", password_hash="unused", role="admin", status="active")
    env.db.add(approver)
    env.db.commit()
    owner, human = {"user_id": env.actor.id}, {"user_id": approver.id}
    registry.sync_definitions(env.db, actor=human)
    scope = {"mode": "explicit", "targets": [{"instance_id": env.instance.id}]}
    first_input = candidate("inspection.collect", slots={"policy_id": env.policy.id}, scope=scope)
    saved = plans.create_plan(env.db, actor=owner, **inputs(first_input))
    assert saved["status"] == "validated" and saved["approval_state"] == "pending"
    assert set(saved["steps"][0]["params"]) - set(first_input["plan_inputs"][0]["steps"][0]["params"]) == bindings._FROZEN
    governance.request_approval(env.db, saved["plan_id"], actor=owner, expected_revision=1)
    with pytest.raises(plans.PlanError, match="self_approval_forbidden|permission_denied"):
        governance.approve_plan(env.db, saved["plan_id"], actor=owner, expected_revision=1,
                                expected_binding_hash=saved["binding_hash"])
    env.db.rollback()
    governance.approve_plan(env.db, saved["plan_id"], actor=human, expected_revision=1, expected_binding_hash=saved["binding_hash"])
    old_revision = deepcopy(env.db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"], revision=1).one().snapshot_json)
    revised_input = candidate("inspection.collect", slots={"policy_id": env.policy.id, "timeout_seconds": 60}, scope=scope)
    revised = governance.revise_plan(env.db, saved["plan_id"], actor=owner, expected_revision=1, **inputs(revised_input))
    assert revised["approved_by"] is None and revised["approval_state"] == "pending"
    assert revised["binding_hash"] != saved["binding_hash"]
    assert env.db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"], revision=1).one().snapshot_json == old_revision
    governance.request_approval(env.db, saved["plan_id"], actor=owner, expected_revision=2)
    with pytest.raises(plans.PlanError, match="approval_binding_invalid"):
        governance.approve_plan(env.db, saved["plan_id"], actor=human, expected_revision=2, expected_binding_hash=saved["binding_hash"])
    env.db.rollback()
    approved = governance.approve_plan(env.db, saved["plan_id"], actor=human, expected_revision=2, expected_binding_hash=revised["binding_hash"])
    plan = env.db.get(DBCapabilityPlan, saved["plan_id"])
    assert plan.approval_binding_hash == plans._hash(plan.capability_snapshot_json) == approved["binding_hash"]
    request_hash = plan.capability_snapshot_json["request_hash"]
    plan_queue.enqueue_batch(env.db, [saved["plan_id"]], actor=owner, idempotency_key="binding-worker")
    assert plan_queue.process_one(env.factory, worker_id="binding-existing-worker")["outcome"] == "succeeded"
    env.db.expire_all()
    assert plans.read_plan(env.db, saved["plan_id"], actor=owner)["outcome"] == "succeeded"
    rerun = plans.create_plan(env.db, actor=owner, **inputs(revised_input), parent_plan_id=saved["plan_id"],
                              idempotency_key="binding-explicit-rerun")
    assert rerun["plan_id"] != saved["plan_id"] and rerun["result_ref"] is None
    assert rerun["approval_state"] == "pending" and rerun["approved_by"] is None
    assert env.db.get(DBCapabilityPlan, rerun["plan_id"]).capability_snapshot_json["request_hash"] == request_hash
    assert env.collections == ["mysql"]
