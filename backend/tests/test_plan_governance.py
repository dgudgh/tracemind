"""Human approval uses persisted revisions and current identities, never caller roles."""
from copy import deepcopy
from datetime import timedelta
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.access_control import SecurityAuditEvent, UserAccessRole
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.user import User
from app.services import access_control, capability_registry as registry, plan_governance as governance, plan_service as plans
from test_capability_plan import ADMIN, READER, OTHER, create, db  # noqa: F401


def pending(db, actor=READER, **kwargs):
    saved = create(db, actor=actor, request_approval=True, **kwargs)
    governance.request_approval(db, saved["plan_id"], actor=actor, expected_revision=1)
    return saved


def approve(db, saved, actor=ADMIN, **kwargs):
    return governance.approve_plan(db, saved["plan_id"], actor=actor,
                                   expected_revision=saved.get("revision", 1),
                                   expected_binding_hash=saved["binding_hash"], **kwargs)


def test_approval_binding_survives_whole_second_datetime_storage(db, monkeypatch):
    instant = datetime(2026, 10, 20, 10, 0, 0, 987654)
    monkeypatch.setattr(plans, "_now", lambda: instant)
    saved = pending(db, valid_for_seconds=60)
    approve(db, saved)
    db.expire_all()
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    assert plan.approved_at.microsecond == plan.approval_expires_at.microsecond == 0
    assert plan.approval_expires_at <= instant + timedelta(seconds=60)
    # This is precision normalization in SQLite, not a claim of a live MySQL round trip.
    plan.approved_at = plan.approved_at.replace(microsecond=0)
    plan.approval_expires_at = plan.approval_expires_at.replace(microsecond=0)
    db.commit()
    with Session(db.get_bind(), autoflush=False) as verify:
        stored = verify.get(DBCapabilityPlan, saved["plan_id"])
        assert governance.approval_problem(verify, stored) is None
        finished = plans.execute_plan(verify, saved["plan_id"], actor=READER)
        assert finished["outcome"] == "succeeded"


def test_read_can_require_independent_human_approval_without_enabling_a_writer(db):
    saved = pending(db)
    with pytest.raises(plans.PlanError, match="approval_required"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    assert db.get(DBCapabilityPlan, saved["plan_id"]).status == "awaiting_approval"
    approved = approve(db, saved)
    assert approved["status"] == "approved" and approved["approved_by"] == 1
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["can_execute"]
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["outcome"] == "succeeded"
    assert db.query(SecurityAuditEvent).filter_by(action="approve_plan", target_id=saved["plan_id"]).count() == 1


def test_self_approval_and_role_hint_are_not_authority(db):
    own = pending(db, actor=ADMIN)
    with pytest.raises(plans.PlanError, match="self_approval_forbidden"):
        approve(db, own)
    db.rollback()
    other = pending(db)
    with pytest.raises(plans.PlanError, match="permission_denied"):
        approve(db, other, actor={"user_id": 3, "role": "admin"})
    db.rollback()
    assert db.get(DBCapabilityPlan, other["plan_id"]).approval_state == "pending"


def test_binding_revision_and_duplicate_decision_are_guarded(db):
    saved = pending(db)
    with pytest.raises(plans.PlanError, match="plan_revision_conflict"):
        governance.approve_plan(db, saved["plan_id"], actor=ADMIN, expected_revision=2,
                                expected_binding_hash=saved["binding_hash"])
    db.rollback()
    with pytest.raises(plans.PlanError, match="approval_binding_invalid"):
        governance.approve_plan(db, saved["plan_id"], actor=ADMIN, expected_revision=1,
                                expected_binding_hash="0" * 64)
    db.rollback()
    approve(db, saved)
    with pytest.raises(plans.PlanError, match="invalid_approval_state"):
        approve(db, saved)


def test_a_database_approved_flag_without_an_actual_decision_is_not_a_grant(db):
    saved = pending(db)
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    plan.status = plan.approval_state = "approved"
    plan.approved_by, plan.approved_at = 1, plans._now()
    plan.approval_expires_at = plans._now() + timedelta(minutes=10)
    db.commit()
    assert plans.review_plan(db, plan.plan_id, actor=READER)["issues"] == [{"code": "approval_binding_invalid"}]
    with pytest.raises(plans.PlanError, match="approval_binding_invalid"):
        plans.execute_plan(db, plan.plan_id, actor=READER)


def test_approval_window_expires_and_is_capped_by_frozen_plan_ttl(db, monkeypatch):
    saved = pending(db, valid_for_seconds=60)
    approved = approve(db, saved, valid_for_seconds=3600)
    deadline = datetime.fromisoformat(saved["expires_at"])
    expiry = datetime.fromisoformat(approved["approval_expires_at"])
    assert expiry == deadline.replace(microsecond=0)
    assert 0 <= (deadline - expiry).total_seconds() < 1
    now = plans._now()
    monkeypatch.setattr(plans, "_now", lambda: now + timedelta(seconds=61))
    review = plans.review_plan(db, saved["plan_id"], actor=READER)
    assert not review["can_execute"]
    assert {"code": "plan_expired"} in review["issues"]
    assert {"code": "approval_expired"} in review["issues"]


def test_revoke_is_durable_and_cannot_be_undone_without_new_revision(db):
    saved = pending(db)
    approve(db, saved)
    revoked = governance.revoke_approval(db, saved["plan_id"], actor=READER, expected_revision=1)
    assert revoked["approval_state"] == "revoked" and revoked["status"] == "validated"
    assert governance.revoke_approval(db, saved["plan_id"], actor=READER, expected_revision=1)["reused"]
    with pytest.raises(plans.PlanError, match="approval_revoked"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    with pytest.raises(plans.PlanError, match="approval_revoked_requires_revision"):
        governance.request_approval(db, saved["plan_id"], actor=READER, expected_revision=1)


def test_new_revision_keeps_old_snapshot_and_invalidates_approval_atomically(db):
    saved = pending(db)
    approve(db, saved)
    old = deepcopy(db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"], revision=1).one().snapshot_json)
    revised = governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1,
                                     capability_id="inspection.run.query", definition_version=1,
                                     instance_id=20, params={"page_size": 11}, request_approval=True)
    assert revised["revision"] == 2 and revised["instance_id"] == 20
    assert revised["approval_state"] == "pending" and revised["approved_by"] is None
    assert revised["binding_hash"] != saved["binding_hash"]
    assert db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"], revision=1).one().snapshot_json == old
    assert db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"]).count() == 2
    assert plans.read_plan(db, saved["plan_id"], actor=READER)["steps"][0]["params"] == {"page_size": 11}
    with pytest.raises(plans.PlanError, match="plan_revision_conflict"):
        governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1,
                               capability_id="inspection.run.query", definition_version=1)


def test_completed_evidence_cannot_be_erased_by_revision(db):
    saved = create(db)
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    with pytest.raises(plans.PlanError, match="executed_plan_requires_rerun"):
        governance.revise_plan(db, saved["plan_id"], actor=READER, expected_revision=1,
                               capability_id="inspection.run.query", definition_version=1)
    db.rollback()
    assert plans.read_plan(db, saved["plan_id"], actor=READER) == done


def test_current_approver_grants_are_checked_again_at_execution(db):
    roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
    db.add(UserAccessRole(user_id=3, role_id=roles["approver"].id))
    db.commit()
    saved = pending(db)
    approve(db, saved, actor=OTHER)
    db.query(UserAccessRole).filter_by(user_id=3).delete()
    db.commit()
    assert {"code": "approval_authority_revoked"} in plans.review_plan(db, saved["plan_id"], actor=READER)["issues"]


def test_high_risk_requires_two_distinct_current_approvers(db, monkeypatch):
    definitions = deepcopy(registry.CAPABILITY_REGISTRY)
    definition = next(item for item in definitions if item["capability_id"] == "inspection.run.query")
    definition["risk"], definition["version"] = "high", 2
    monkeypatch.setattr(registry, "CAPABILITY_REGISTRY", definitions)
    registry.sync_definitions(db, actor=ADMIN)
    roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
    db.add(UserAccessRole(user_id=3, role_id=roles["approver"].id))
    db.commit()
    saved = pending(db, definition_version=2)
    first = approve(db, saved)
    assert first["approval_state"] == "pending"
    with pytest.raises(plans.PlanError, match="duplicate_approval"):
        approve(db, saved)
    db.rollback()
    approved = approve(db, saved, actor=OTHER)
    assert approved["approval_state"] == "approved"
    assert governance.read_approval(db, saved["plan_id"], actor=OTHER)["required_approvals"] == 2


def test_audit_storage_failure_does_not_commit_approval(db, monkeypatch):
    saved = pending(db)
    def fail(*args, **kwargs):
        raise OperationalError("audit unavailable", {}, None)
    monkeypatch.setattr(access_control, "append_security_audit", fail)
    with pytest.raises(OperationalError):
        approve(db, saved)
    db.rollback()
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    assert plan.approval_state == "pending" and plan.approved_by is None


def test_revocation_from_another_session_during_handler_never_publishes_result(db, monkeypatch):
    saved = pending(db)
    approve(db, saved)
    original = registry.execute_read
    def revoke_while_reading(*args, **kwargs):
        result = original(*args, **kwargs)
        with Session(db.get_bind(), autoflush=False) as other:
            governance.revoke_approval(other, saved["plan_id"], actor=ADMIN, expected_revision=1)
        return result
    monkeypatch.setattr(registry, "execute_read", revoke_while_reading)
    with pytest.raises(plans.PlanOwnershipLost, match="approval_revoked"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=saved["plan_id"]).one()
    assert step.result_ref_json is None
    assert db.get(DBCapabilityPlan, saved["plan_id"]).outcome is None


def test_worker_claim_loss_is_not_published_as_a_failure_by_the_old_worker(db):
    saved = create(db)
    calls = []
    def guard():
        calls.append(None)
        if len(calls) == 4:
            raise ValueError("lost lease")
    with pytest.raises(plans.PlanOwnershipLost, match="execution_ownership_lost"):
        plans.execute_plan(db, saved["plan_id"], actor=READER, execution_guard=guard)
    db.rollback()
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=saved["plan_id"]).one()
    assert step.status == "running" and step.result_ref_json is None
    assert db.get(DBCapabilityPlan, saved["plan_id"]).outcome is None


def test_revoke_after_the_last_explicit_check_still_fences_the_actual_commit(db, monkeypatch):
    saved = pending(db)
    approve(db, saved)
    original = plans._result_ref
    revoked = []
    def after_last_check(entry, result):
        reference = original(entry, result)
        if not revoked:
            with Session(db.get_bind(), autoflush=False) as other:
                governance.revoke_approval(other, saved["plan_id"], actor=ADMIN, expected_revision=1)
            revoked.append(True)
        return reference
    monkeypatch.setattr(plans, "_result_ref", after_last_check)
    with pytest.raises(plans.PlanOwnershipLost, match="approval_revoked"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    assert revoked
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    assert row.approval_state == "revoked" and row.outcome is None
    assert all(step.result_ref_json is None for step in plans._steps(db, row.plan_id))


def test_expiry_after_the_last_explicit_check_is_rechecked_at_commit(db, monkeypatch):
    saved = pending(db, valid_for_seconds=60)
    approve(db, saved)
    original, now = plans._result_ref, plans._now()
    def expire_before_commit(entry, result):
        reference = original(entry, result)
        monkeypatch.setattr(plans, "_now", lambda: now + timedelta(seconds=61))
        return reference
    monkeypatch.setattr(plans, "_result_ref", expire_before_commit)
    with pytest.raises(plans.PlanOwnershipLost, match="plan_expired"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    assert db.get(DBCapabilityPlan, saved["plan_id"]).outcome is None
    assert all(step.result_ref_json is None for step in plans._steps(db, saved["plan_id"]))


def test_approval_authority_never_grants_other_peoples_business_result_payloads(db):
    roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
    db.query(UserAccessRole).filter_by(user_id=3).delete()
    db.add(UserAccessRole(user_id=3, role_id=roles["approver"].id))
    db.commit()
    saved = pending(db, capability="operations.capability_matrix.read")
    approve(db, saved, actor=OTHER)
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["steps"][0]["result_ref"]["inline"]["instances"]
    context = governance.read_approval(db, saved["plan_id"], actor=OTHER)
    assert context["plan"]["result_ref"] is None
    assert all(step["result_ref"] is None for step in context["plan"]["steps"])
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        plans.read_plan(db, saved["plan_id"], actor=OTHER)
    ungoverned = create(db, "operations.capability_matrix.read")
    plans.execute_plan(db, ungoverned["plan_id"], actor=READER)
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        governance.read_approval(db, ungoverned["plan_id"], actor=OTHER)


def test_revoke_between_last_step_and_terminal_flush_never_settles_success(db, monkeypatch):
    saved = pending(db)
    approve(db, saved)
    original = plans.settle
    def revoke_then_settle(session, plan, outcome, **kwargs):
        with Session(db.get_bind(), autoflush=False) as other:
            governance.revoke_approval(other, saved["plan_id"], actor=ADMIN, expected_revision=1)
        return original(session, plan, outcome, **kwargs)
    monkeypatch.setattr(plans, "settle", revoke_then_settle)
    with pytest.raises(plans.PlanOwnershipLost, match="approval_revoked"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    assert plan.status == "running" and plan.outcome is None and plan.result_ref_json is None
    assert plan.approval_state == "revoked"


def test_an_explicit_terminal_flush_does_not_clear_the_commit_time_ttl_fence(db, monkeypatch):
    saved = pending(db, valid_for_seconds=60)
    approve(db, saved)
    original, now = plans.settle, plans._now()
    def flush_then_expire(session, plan, outcome, **kwargs):
        result = original(session, plan, outcome, **kwargs)
        assert plan not in session.dirty  # settle flushed, but has not committed.
        monkeypatch.setattr(plans, "_now", lambda: now + timedelta(seconds=61))
        return result
    monkeypatch.setattr(plans, "settle", flush_then_expire)
    with pytest.raises(plans.PlanOwnershipLost, match="plan_expired"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    plan = db.get(DBCapabilityPlan, saved["plan_id"])
    assert plan.status == "running" and plan.outcome is None and plan.result_ref_json is None


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_savepoint_end_never_clears_the_outer_publication_fence(db, monkeypatch, end):
    saved = pending(db, valid_for_seconds=60)
    approve(db, saved)
    original, now = plans.settle, plans._now()

    def savepoint_then_expire(session, plan, outcome, **kwargs):
        result = original(session, plan, outcome, **kwargs)
        nested = session.begin_nested()
        session.execute(text("SELECT 1"))
        getattr(nested, end)()
        assert session.in_transaction() and plan not in session.dirty
        monkeypatch.setattr(plans, "_now", lambda: now + timedelta(seconds=61))
        return result

    monkeypatch.setattr(plans, "settle", savepoint_then_expire)
    with pytest.raises(plans.PlanOwnershipLost, match="plan_expired"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)
    db.rollback()
    with Session(db.get_bind()) as verify:
        plan = verify.get(DBCapabilityPlan, saved["plan_id"])
        assert plan.status == "running" and plan.outcome is None and plan.result_ref_json is None
        assert plans._steps(verify, saved["plan_id"])[0].result_ref_json is not None
