"""Read-only U03 review; actual readers remain closed until execution rechecks."""
from copy import deepcopy
from datetime import datetime

import pytest
from sqlalchemy import event

from app.models.access_control import SecurityAuditEvent, UserAccessRole
from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.db_instance import DBInstance
from app.services import capability_registry as registry, plan_service as plans
from app.services.capability_handler import HANDLERS
from test_capability_plan import ADMIN, OTHER, READER, create, db  # noqa: F401


def forbid_execution(*args, **kwargs):
    pytest.fail("review or blocked execution must not call a business handler")


def test_review_is_read_only_with_no_handler_audit_or_row_mutation(db, monkeypatch):
    saved = create(db, instance_id=10)
    before = plans.read_plan(db, saved["plan_id"], actor=READER)
    audits = db.query(SecurityAuditEvent).count()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    statements = []
    def capture(_connection, _cursor, statement, *_rest):
        statements.append(statement.strip().split(None, 1)[0].upper())
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        report = plans.review_plan(db, saved["plan_id"], actor=READER)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert report == {"schema": "capability.plan_validation/v1", "plan_id": saved["plan_id"],
                      "revision": 1, "checked_at": report["checked_at"], "plan_status": "validated",
                      "can_execute": True, "issues": [], "approval_available": False, "requires_recheck": True}
    datetime.fromisoformat(report["checked_at"])
    assert set(statements) == {"SELECT"}
    assert plans.read_plan(db, saved["plan_id"], actor=READER) == before
    assert db.query(SecurityAuditEvent).count() == audits
    assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPlanRevision).count() == 1


@pytest.mark.parametrize("change,reason", [
    ("disabled", "capability_disabled"), ("target", "target_changed"),
    ("instance_disabled", "target_unavailable"), ("definition", "definition_out_of_sync"),
])
def test_review_shows_current_blocker_without_settling_the_plan(db, monkeypatch, change, reason):
    saved = create(db, instance_id=10)
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    if change in {"disabled", "definition"}:
        row = db.get(DBCapabilityDefinition, "inspection.run.query")
        if change == "disabled":
            row.enabled = 0
        else:
            row.definition_hash = "f" * 64
    else:
        row = db.get(DBInstance, 10)
        if change == "target":
            row.host = "127.0.0.2"
        else:
            row.enabled = 0
    db.commit()
    report = plans.review_plan(db, saved["plan_id"], actor=READER)
    assert report["can_execute"] is False and report["issues"] == [{"code": reason}]
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    assert row.status == "validated" and row.outcome is None
    assert not row.result_ref_json


def test_successful_review_cannot_be_used_after_revocation(db, monkeypatch):
    saved = create(db)
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["can_execute"]
    db.query(UserAccessRole).filter_by(user_id=2).delete()
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    with pytest.raises(plans.PlanError, match="permission_denied"):
        plans.execute_plan(db, saved["plan_id"], actor=READER)


def test_admin_review_does_not_delegate_execution_or_reveal_to_another_reader(db):
    saved = create(db)
    report = plans.review_plan(db, saved["plan_id"], actor=ADMIN)
    assert not report["can_execute"] and report["issues"] == [{"code": "plan_owner_required"}]
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        plans.review_plan(db, saved["plan_id"], actor=OTHER)


def test_review_of_completed_plan_is_not_a_new_execution_or_new_failure(db, monkeypatch):
    saved = create(db)
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    report = plans.review_plan(db, saved["plan_id"], actor=READER)
    assert not report["can_execute"] and report["issues"] == [{"code": "invalid_plan_state"}]
    assert plans.read_plan(db, saved["plan_id"], actor=READER) == done


@pytest.mark.parametrize("field,value", [
    ("approval_binding_hash", "a" * 64), ("approved_by", 1),
    ("approved_at", datetime(2026, 1, 1)), ("approval_expires_at", datetime(2026, 1, 1)),
    ("revoked_at", datetime(2026, 1, 1)), ("revoked_by", 1),
])
def test_read_plan_with_inconsistent_approval_metadata_is_blocked(db, monkeypatch, field, value):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    setattr(row, field, value)
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["issues"] == [{"code": "approval_metadata_invalid"}]
    result = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert result["outcome"] == "blocked" and result["outcome_kind"] == "approval_metadata_invalid"


def test_approval_requirement_is_derived_even_if_row_flag_is_weakened(db, monkeypatch):
    saved = create(db, "asset.discovery.execute", actor=ADMIN, instance_id=10)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    row.approval_state, row.approval_binding_hash = "not_required", None
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    report = plans.review_plan(db, saved["plan_id"], actor=ADMIN)
    assert not report["can_execute"]
    assert {"code": "approval_not_available_until_u03"} in report["issues"]
    done = plans.execute_plan(db, saved["plan_id"], actor=ADMIN)
    assert done["outcome_kind"] == "approval_not_available_until_u03"


@pytest.mark.parametrize("part", ["definitions_null", "steps_null", "definition_binding", "target_missing",
                                   "step_version", "step_effect", "revision_copy", "revision_actor", "handler_list"])
def test_corrupted_frozen_projection_is_an_explicit_conflict(db, part):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id).one()
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=row.plan_id).one()
    snapshot = deepcopy(row.capability_snapshot_json)
    if part == "definitions_null":
        snapshot["definitions"] = None
    elif part == "steps_null":
        snapshot["steps"] = None
    elif part == "definition_binding":
        snapshot["definitions"][0]["bindings"] = [7]
    elif part == "handler_list":
        snapshot["definitions"][0]["handler"] = []
    elif part == "target_missing":
        snapshot.pop("target_fingerprint")
    elif part == "step_version":
        snapshot["steps"][0]["definition_version"] = 2
        step.precondition_json = deepcopy(snapshot["steps"][0])
    elif part == "step_effect":
        step.side_effect = "write"
    # A matching hash alone is not proof that every stored projection is intact.
    row.capability_snapshot_json = snapshot
    revision.binding_hash = plans._hash(snapshot)
    revision.snapshot_json = deepcopy(snapshot)
    if part == "revision_copy":
        revision.snapshot_json = {}
    elif part == "revision_actor":
        revision.created_by = 3
    db.commit()
    for action in (plans.read_plan, plans.review_plan, plans.execute_plan):
        with pytest.raises(plans.PlanError, match="plan_snapshot_invalid"):
            action(db, saved["plan_id"], actor=READER)
        db.rollback()


def test_execution_revalidates_typed_inputs_even_after_a_rehashed_persistence_repair(db, monkeypatch):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id).one()
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=row.plan_id).one()
    snapshot = deepcopy(row.capability_snapshot_json)
    snapshot["steps"][0]["params"] = {"page_size": 0}
    row.capability_snapshot_json = snapshot
    revision.snapshot_json, revision.binding_hash = deepcopy(snapshot), plans._hash(snapshot)
    step.precondition_json = deepcopy(snapshot["steps"][0])
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["issues"] == [{"code": "invalid_params"}]
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["outcome"] == "blocked" and done["outcome_kind"] == "invalid_params"


def test_review_reports_missing_target_without_new_plan_or_default_target(db, monkeypatch):
    saved = create(db, "inspection.run.execute", actor=ADMIN)
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    report = plans.review_plan(db, saved["plan_id"], actor=ADMIN)
    assert not report["can_execute"] and {"code": "missing_instance"} in report["issues"]
    assert db.get(DBCapabilityPlan, saved["plan_id"]).instance_id is None
    assert db.query(DBCapabilityPlan).count() == 1


@pytest.mark.parametrize("params", [{"status": "NOT_A_STATUS"}, {"outcome": "NOT_AN_OUTCOME"},
                                     {"status": "queued,NOT_A_STATUS"}])
def test_invalid_literal_filters_in_later_steps_are_rejected_before_any_execution(db, monkeypatch, params):
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    steps = [{"seq": 1, "capability_id": "inspection.template.read", "definition_version": 1},
             {"seq": 2, "capability_id": "inspection.run.query", "definition_version": 1, "params": params}]
    with pytest.raises(plans.PlanError, match="invalid_params"):
        create(db, "inspection.template.read", steps=steps)
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("field,value", [("fanout", 999), ("retries", 999)])
def test_execution_revalidates_frozen_budget_before_any_handler(db, monkeypatch, field, value):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id).one()
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=row.plan_id).one()
    snapshot = deepcopy(row.capability_snapshot_json)
    snapshot["steps"][0][field] = value
    row.capability_snapshot_json = snapshot
    revision.snapshot_json, revision.binding_hash = deepcopy(snapshot), plans._hash(snapshot)
    step.precondition_json = deepcopy(snapshot["steps"][0])
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    reason = "plan_fanout_exceeded" if field == "fanout" else "plan_retry_exceeded"
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["issues"] == [{"code": reason}]
    assert plans.execute_plan(db, saved["plan_id"], actor=READER)["outcome_kind"] == reason


@pytest.mark.parametrize("change", ["cancelled", "result", "started", "finished", "outcome"])
def test_dirty_pending_projection_cannot_be_claimed_or_overwritten(db, monkeypatch, change):
    saved = create(db)
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=saved["plan_id"]).one()
    if change == "cancelled":
        step.status = "cancelled"
    elif change == "result":
        step.result_ref_json = {"kind": "bogus"}
    elif change == "outcome":
        step.outcome_kind = "cancelled"
    else:
        setattr(step, change + "_at", datetime(2026, 1, 1))
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["issues"] == [{"code": "plan_steps_not_pending"}]
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["outcome"] == "blocked" and done["outcome_kind"] == "plan_steps_not_pending"


def test_wrong_matrix_schema_is_unknown_and_not_relabelled_as_valid(db, monkeypatch):
    saved = create(db, "operations.capability_matrix.read")
    monkeypatch.setitem(HANDLERS, "operations.capability_matrix", lambda ctx: {
        "schema": "wrong/v99", "items": [], "instances": [], "engines": [], "summary": {},
    })
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["outcome"] == "unknown" and done["steps"][0]["result_ref"] is None


@pytest.mark.parametrize("reference", [{"kind": "bogus"}, {"kind": "capability_read", "inline": {}},
                                       {"kind": "capability_read", "schema": "inspection.run_list/v1",
                                        "capability_id": "inspection.run.query", "inline": {"items": []}}])
def test_fake_step_evidence_cannot_support_success_or_partial_settlement(db, reference):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    row.status = "running"
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=row.plan_id).one()
    step.status, step.result_ref_json = "succeeded", reference
    db.flush()
    with pytest.raises(plans.PlanError, match="invalid_evidence_outcome"):
        plans.settle(db, row, "succeeded")


def test_mismatched_matrix_engine_and_instance_are_rejected_before_any_step(db, monkeypatch):
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    steps = [{"seq": 1, "capability_id": "inspection.run.query", "definition_version": 1},
             {"seq": 2, "capability_id": "operations.capability_matrix.read", "definition_version": 1,
              "params": {"engine": "oracle"}}]
    with pytest.raises(plans.PlanError, match="invalid_params"):
        create(db, instance_id=10, steps=steps)
    assert db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("field,value", [("started_at", datetime(2026, 1, 1)),
    ("finished_at", datetime(2026, 1, 1)), ("result_ref_json", {"kind": "bogus"}),
    ("outcome_kind", "old_result"), ("outcome_reason", "old_result"), ("outcome_declared_by", "old_actor")])
def test_validated_plan_with_leftover_main_row_evidence_is_not_restarted(db, monkeypatch, field, value):
    saved = create(db)
    row = db.get(DBCapabilityPlan, saved["plan_id"])
    setattr(row, field, value)
    db.commit()
    monkeypatch.setattr(registry, "execute_read", forbid_execution)
    assert plans.review_plan(db, saved["plan_id"], actor=READER)["issues"] == [{"code": "plan_steps_not_pending"}]
    done = plans.execute_plan(db, saved["plan_id"], actor=READER)
    assert done["outcome_kind"] == "plan_steps_not_pending"
