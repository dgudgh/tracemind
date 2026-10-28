"""Atomic workflow receipts and real plans on isolated file-backed SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models.access_control import AccessRole, RoleActionPermission, SecurityAuditEvent, UserAccessRole, UserAssetScope
from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision, DBCapabilityPlanStep
from app.models.capability_preparation import DBCapabilityPreparation
from app.models.inspection_job import DBInspectionJob
from app.models.user import User
from app.services import access_control, capability_registry as registry, plan_admission, plan_governance
from app.services import plan_queue, plan_service as plans, workflow_service as workflows
from app.services.plan_admission import SUMMARY_FIELDS
from test_capability_plan import ADMIN, OTHER, READER, create, db  # noqa: F401
from test_inspection_plan_adapter import env as inspection_env  # noqa: F401


GLOBAL = {"mode": "global"}
ONE = {"mode": "explicit", "targets": [{"instance_id": 10}]}
TWO = {"mode": "explicit", "targets": [{"instance_id": 10}, {"instance_id": 20}]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("workflow service must not contact a target/model/Agent")
    monkeypatch.setattr(socket.socket, "connect", denied)


def prepare(db, name="inspection.jobs.list", *, actor=READER, key="workflow-request", slots=None,
            scope=None, **kwargs):
    return workflows.prepare_workflow(db, name, 1, actor=actor, idempotency_key=key,
                                      slots={} if slots is None else slots,
                                      scope=TWO if scope is None else scope, **kwargs)


def rejected(code, call):
    with pytest.raises(plans.PlanError) as caught:
        call()
    assert caught.value.code == code


def restrict_actions(db, actions):
    db.query(UserAccessRole).filter_by(user_id=3).delete(synchronize_session=False)
    role = AccessRole(role_key="workflow-restricted", name="Workflow tests", status="active")
    db.add(role)
    db.flush()
    db.add(UserAccessRole(user_id=3, role_id=role.id))
    db.add_all([RoleActionPermission(role_id=role.id, action_key=action) for action in actions])
    db.commit()
    return role


def completed(db, *, scope=ONE, name="inspection.jobs.list", key="source", slots=None):
    saved = prepare(db, name, key=key, scope=scope, slots=slots)
    plan_id = saved["items"][0]["plan_id"]
    assert plans.execute_plan(db, plan_id, actor=READER)["outcome"] == "succeeded"
    return saved, plan_id


def test_authorized_catalog_requires_every_step_action_and_match_cannot_fallback(db):
    restrict_actions(db, {"inspection.read"})
    catalog = workflows.list_workflows(db, actor={"user_id": 3, "role": "admin"})
    assert {item["workflow_id"] for item in catalog["items"]} == {
        "inspection.jobs.list", "inspection.templates.list"}
    rejected("permission_denied", lambda: workflows.get_workflow(db, "inspection.context.read", 1, actor=OTHER))
    rejected("permission_denied", lambda: workflows.match_workflow(db, "查看巡检上下文", actor=OTHER))
    rejected("permission_denied", lambda: workflows.match_workflow(db, "运行巡检", actor=OTHER))
    assert workflows.match_workflow(db, "打开巡检页面", actor=OTHER)["operation"] == "navigate"
    assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


def test_catalog_detail_match_and_receipt_get_are_read_only_without_seed_or_sync(db, monkeypatch):
    saved = prepare(db)
    mutations = []

    def observe(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            mutations.append(statement)

    def forbidden(*args, **kwargs):
        raise AssertionError("GET cannot sync, seed, audit or execute")

    monkeypatch.setattr(registry, "sync_definitions", forbidden)
    monkeypatch.setattr(access_control, "ensure_builtin_roles", forbidden)
    monkeypatch.setattr(access_control, "append_security_audit", forbidden)
    monkeypatch.setattr(plans, "create_plan", forbidden)
    monkeypatch.setattr(plans, "execute_plan", forbidden)
    event.listen(db.get_bind(), "before_cursor_execute", observe)
    try:
        assert len(workflows.list_workflows(db, actor=READER)["items"]) == 4
        assert workflows.get_workflow(db, "inspection.jobs.list", 1, actor=READER)["workflow_version"] == 1
        assert workflows.match_workflow(db, "查看已有报告", actor=READER)["operation"] == "view_existing"
        read = workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request")
        assert read["preparation_id"] == saved["preparation_id"]
        assert all(item["reused"] for item in read["items"])
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", observe)
    assert mutations == []


def test_disabled_or_unreleased_recipes_are_not_selectable_and_checks_write_nothing(db, monkeypatch):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", False)
    db.get(DBCapabilityDefinition, "inspection.run.query").enabled = 0
    db.commit()
    statements = []
    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(db.get_bind(), "before_cursor_execute", observe)
    try:
        catalog = workflows.list_workflows(db, actor=ADMIN)
        assert {item["workflow_id"] for item in catalog["items"]} == {
            "inspection.templates.list", "operations.capabilities.list"}
        for definition in catalog["items"]:
            assert definition["runtime_checks_required"] is True
            assert not {"ready", "available", "availability", "plan_executable"}.intersection(definition)
        rejected("capability_not_released", lambda: workflows.get_workflow(db, "inspection.collect", 1, actor=ADMIN))
        rejected("capability_disabled", lambda: workflows.get_workflow(db, "inspection.jobs.list", 1, actor=ADMIN))
        rejected("capability_not_released", lambda: workflows.match_workflow(db, "运行巡检", actor=ADMIN))
        rejected("capability_disabled", lambda: workflows.match_workflow(db, "查看巡检任务", actor=ADMIN))
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", observe)
    assert not {"INSERT", "UPDATE", "DELETE", "REPLACE"}.intersection(statements)
    rejected("capability_disabled", lambda: prepare(db, actor=ADMIN))
    assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


@pytest.mark.parametrize("change,code", [("unsynced", "definition_not_synced"), ("drift", "definition_out_of_sync")])
def test_unsynced_or_drifted_registry_never_appears_selectable_and_does_not_seed(db, change, code):
    if change == "unsynced":
        db.query(DBCapabilityDefinition).delete(synchronize_session=False)
    else:
        for row in db.query(DBCapabilityDefinition).all():
            row.definition_hash = "0" * 64
    db.commit()
    before = db.query(DBCapabilityDefinition).count()
    assert workflows.list_workflows(db, actor=ADMIN)["items"] == []
    rejected(code, lambda: workflows.get_workflow(db, "inspection.jobs.list", 1, actor=ADMIN))
    rejected(code, lambda: workflows.match_workflow(db, "查看巡检任务", actor=ADMIN))
    assert db.query(DBCapabilityDefinition).count() == before


def test_retired_registry_blocks_new_selection_but_not_original_receipt_history(db):
    saved = prepare(db)
    db.get(DBCapabilityDefinition, "inspection.run.query").enabled = 0
    db.commit()
    rejected("capability_disabled", lambda: workflows.get_workflow(db, "inspection.jobs.list", 1, actor=READER))
    read = workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request")
    assert read["preparation_id"] == saved["preparation_id"]


def test_prepare_persists_every_plan_revision_binding_and_audit_once_but_never_enqueues(db):
    value = prepare(db)
    assert value["schema"] == "capability.workflow_preparation/v1"
    assert value["accepted"] is False and value["status"] == "prepared"
    assert value["total"] == 2 and value["operation"] == "prepare" and value["source_plan_id"] is None
    assert all(set(item) == set(SUMMARY_FIELDS) and not item["reused"] for item in value["items"])
    assert db.query(DBCapabilityBatch).count() == db.query(DBCapabilityExecution).count() == 0
    assert db.query(DBInspectionJob).count() == 0
    receipt = db.query(DBCapabilityPreparation).one()
    assert receipt.request_key_hash == plans._hash({"request_key": "workflow-request"})
    audit = db.query(SecurityAuditEvent).filter_by(category="capability_workflow").one()
    assert audit.actor_user_id == 2 and audit.target_id == receipt.preparation_id
    for index, item in enumerate(value["items"]):
        plan = db.get(DBCapabilityPlan, item["plan_id"])
        snapshot = plan.capability_snapshot_json
        assert snapshot["admission_hash"] == receipt.input_hash
        assert plan.idempotency_key == plans._hash({"actor": 2, "client_key": plans._hash({
            "batch_key": "workflow-request", "position": index})})
        assert snapshot["workflow_binding"]["input"]["scope"] == {
            "mode": "explicit", "targets": [{"instance_id": item["instance_id"]}]}
        revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=plan.plan_id, revision=1).one()
        assert revision.snapshot_json == snapshot and revision.binding_hash == item["binding_hash"]


def test_global_preparation_uses_null_target_without_relaxing_original_batch_v1(db):
    value = prepare(db, scope=GLOBAL)
    assert value["total"] == 1 and value["items"][0]["instance_id"] is None
    assert workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request")["total"] == 1
    rejected("preparation_binding_changed", lambda: plan_admission.read_preparation(
        db, actor=READER, idempotency_key="workflow-request"))


def test_three_read_composite_has_real_dag_and_single_target_binding_without_siblings(db):
    value = prepare(db, "inspection.context.read", slots={"jobs": {"page_size": 7}})
    for item in value["items"]:
        row = db.get(DBCapabilityPlan, item["plan_id"])
        binding = row.capability_snapshot_json["workflow_binding"]
        assert [step["depends_on"] for step in row.capability_snapshot_json["steps"]] == [[], [1], [2]]
        assert binding["input"]["scope"]["targets"] == [{"instance_id": row.instance_id}]
        assert len(binding["input"]["scope"]["targets"]) == 1


def test_request_replay_uses_effective_defaults_and_does_not_add_plans_or_audits(db):
    saved = prepare(db)
    replay = prepare(db, slots={"page": 1, "page_size": 50})
    assert replay["preparation_id"] == saved["preparation_id"]
    assert [item["plan_id"] for item in replay["items"]] == [item["plan_id"] for item in saved["items"]]
    assert all(item["reused"] for item in replay["items"])
    assert db.query(DBCapabilityPlan).count() == 2
    assert db.query(SecurityAuditEvent).filter_by(category="capability_workflow").count() == 1
    rejected("idempotency_conflict", lambda: prepare(db, slots={"page_size": 9}))
    rejected("idempotency_conflict", lambda: prepare(db, scope={"mode": "explicit", "targets": list(reversed(TWO["targets"]))}))
    rejected("idempotency_conflict", lambda: prepare(db, "inspection.templates.list"))


def test_request_families_cannot_alias_existing_legacy_preparation(db):
    plan_admission.prepare_batch(db, actor=READER, capability_id="inspection.run.query", definition_version=1,
                                 targets=TWO["targets"], idempotency_key="workflow-request")
    rejected("idempotency_conflict", lambda: prepare(db))
    rejected("workflow_binding_invalid", lambda: workflows.read_preparation(
        db, actor=READER, idempotency_key="workflow-request"))
    assert db.query(DBCapabilityPlan).count() == 2


def test_case_distinct_keys_never_cross_bind_and_exact_bytes_are_checked_after_lookup(db):
    first = prepare(db, key="Request.A")
    second = prepare(db, key="request.a")
    assert first["preparation_id"] != second["preparation_id"]
    row = db.query(DBCapabilityPreparation).filter_by(idempotency_key="Request.A").one()
    row.idempotency_key = "REQUEST.A"
    db.commit()
    rejected("idempotency_conflict", lambda: workflows.read_preparation(db, actor=READER, idempotency_key="Request.A"))
    rejected("idempotency_conflict", lambda: prepare(db, key="Request.A"))


def test_owner_and_current_role_scope_are_real_on_read_and_prepare(db):
    saved = prepare(db)
    rejected("preparation_not_found", lambda: workflows.read_preparation(db, actor=OTHER, idempotency_key="workflow-request"))
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"))
    db.commit()
    rejected("scope_denied", lambda: workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request"))
    rejected("scope_denied", lambda: prepare(db, key="restricted"))
    rejected("scope_denied", lambda: prepare(db, key="global-scope", scope=GLOBAL))
    db.query(UserAccessRole).filter_by(user_id=2).delete(synchronize_session=False)
    db.commit()
    rejected("permission_denied", lambda: workflows.read_preparation(
        db, actor={"user_id": 2, "role": "admin"}, idempotency_key="workflow-request"))
    assert db.query(DBCapabilityPlan).count() == saved["total"]


@pytest.mark.parametrize("mutation", ["position", "revision", "admission", "binding", "input_hash", "owner"])
def test_receipt_checks_original_positions_revision_hash_and_owner(db, mutation):
    saved = prepare(db)
    receipt = db.query(DBCapabilityPreparation).one()
    plan = db.get(DBCapabilityPlan, saved["items"][0]["plan_id"])
    if mutation == "position":
        receipt.plan_bindings_json = list(reversed(receipt.plan_bindings_json))
    elif mutation == "revision":
        changed = deepcopy(receipt.plan_bindings_json)
        changed[0]["revision"] = 2
        receipt.plan_bindings_json = changed
    elif mutation == "binding":
        changed = deepcopy(receipt.plan_bindings_json)
        changed[0]["binding_hash"] = "0" * 64
        receipt.plan_bindings_json = changed
    elif mutation == "input_hash":
        receipt.input_hash = "0" * 64
    elif mutation == "owner":
        plan.requested_by = 3
    else:
        snapshot = deepcopy(plan.capability_snapshot_json)
        snapshot["admission_hash"] = "0" * 64
        plan.capability_snapshot_json = snapshot
    db.commit()
    with pytest.raises(plans.PlanError):
        workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request")


def test_historical_receipt_read_never_recompiles_todays_recipe(db, monkeypatch):
    saved = prepare(db)
    def changed(*args, **kwargs):
        raise AssertionError("historical receipt must not consult today's recipe")
    monkeypatch.setattr(workflows.library, "get_workflow", changed)
    monkeypatch.setattr(workflows.library, "compile_workflow", changed)
    read = workflows.read_preparation(db, actor=READER, idempotency_key="workflow-request")
    assert read["workflow"] == saved["workflow"]


def test_all_targets_are_preflighted_before_first_create_and_no_partial_rows_remain(db, monkeypatch):
    original = plans.create_plan
    calls = []
    def tracked(*args, **kwargs):
        calls.append(kwargs["instance_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(plans, "create_plan", tracked)
    rejected("target_unavailable", lambda: prepare(db, scope={"mode": "explicit", "targets": [
        {"instance_id": 10}, {"instance_id": 999}]}))
    assert calls == [] and db.query(DBCapabilityPreparation).count() == db.query(DBCapabilityPlan).count() == 0


@pytest.mark.parametrize("change", ["permission", "definition"])
def test_revocation_after_preflight_is_rechecked_by_core_and_rolls_back_everything(db, monkeypatch, change):
    original = plans.create_plan
    calls = [0]
    def revoke_late(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            if change == "permission":
                db.query(UserAccessRole).filter_by(user_id=2).delete(synchronize_session=False)
            else:
                db.get(DBCapabilityDefinition, "inspection.run.query").enabled = 0
                db.flush()
        return original(*args, **kwargs)
    monkeypatch.setattr(plans, "create_plan", revoke_late)
    rejected("permission_denied" if change == "permission" else "capability_disabled", lambda: prepare(db))
    assert calls[0] == 2
    assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


@pytest.mark.parametrize("condition", ["disabled", "unsynced", "engine", "target_disabled"])
def test_unconfigured_disabled_or_wrong_target_workflow_fails_before_acceptance(db, condition):
    slots = {}
    if condition == "disabled":
        db.get(DBCapabilityDefinition, "inspection.run.query").enabled = 0
    elif condition == "unsynced":
        db.query(DBCapabilityDefinition).delete(synchronize_session=False)
    elif condition == "target_disabled":
        from app.models.db_instance import DBInstance
        db.get(DBInstance, 10).enabled = 0
    else:
        slots = {"matrix": {"engine": "oracle"}}
    db.commit()
    name = "inspection.context.read" if condition == "engine" else "inspection.jobs.list"
    with pytest.raises(plans.PlanError):
        prepare(db, name, slots=slots)
    assert db.query(DBCapabilityPlan).count() == db.query(DBCapabilityPreparation).count() == 0


@pytest.mark.parametrize("stage", ["second_plan", "audit_exception", "audit_none", "commit"])
def test_every_failure_rolls_back_plans_receipt_revisions_and_audit(db, monkeypatch, stage):
    original = plans.create_plan
    count = [0]
    if stage == "second_plan":
        def fail_second(*args, **kwargs):
            count[0] += 1
            if count[0] == 2:
                raise plans.PlanError("target_unavailable")
            return original(*args, **kwargs)
        monkeypatch.setattr(plans, "create_plan", fail_second)
    elif stage == "audit_none":
        monkeypatch.setattr(access_control, "append_security_audit", lambda *args, **kwargs: None)
    elif stage == "audit_exception":
        def fail_audit(*args, **kwargs):
            raise RuntimeError("isolated audit failure")
        monkeypatch.setattr(access_control, "append_security_audit", fail_audit)
    else:
        def fail_commit():
            raise OperationalError("isolated", {}, RuntimeError("commit unavailable"))
        monkeypatch.setattr(db, "commit", fail_commit)
    expected = "target_unavailable" if stage == "second_plan" else (
        "workflow_storage_unavailable" if stage == "commit" else "workflow_audit_unavailable")
    rejected(expected, lambda: prepare(db))
    with Session(db.get_bind()) as fresh:
        assert fresh.query(DBCapabilityPlan).count() == fresh.query(DBCapabilityPlanRevision).count() == 0
        assert fresh.query(DBCapabilityPreparation).count() == fresh.query(DBCapabilityPlanStep).count() == 0
        assert fresh.query(SecurityAuditEvent).filter_by(category="capability_workflow").count() == 0


def test_prepare_missing_scope_or_policy_does_not_save_declared_fake_plans(db, monkeypatch):
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    rejected("workflow_missing_input", lambda: prepare(db, scope={}))
    rejected("workflow_missing_input", lambda: prepare(db, "inspection.collect", actor=ADMIN, scope=ONE))
    assert db.query(DBCapabilityPreparation).count() == db.query(DBCapabilityPlan).count() == 0


def test_real_rerun_uses_new_parent_child_ids_and_replay_finds_original_even_after_source_has_child(db):
    original, parent = completed(db)
    again = prepare(db, key="rerun", scope=ONE, operation="rerun", source_plan_id=parent)
    child = again["items"][0]["plan_id"]
    assert child != parent and again["source_plan_id"] == parent and again["operation"] == "rerun"
    assert db.get(DBCapabilityPlan, child).parent_plan_id == parent
    replay = prepare(db, key="rerun", scope=ONE, operation="rerun", source_plan_id=parent)
    assert replay["preparation_id"] == again["preparation_id"]
    assert workflows.read_preparation(db, actor=READER, idempotency_key="rerun")["operation"] == "rerun"
    rejected("parent_plan_already_retried", lambda: prepare(db, key="other-rerun", scope=ONE,
              operation="rerun", source_plan_id=parent))
    plans.execute_plan(db, child, actor=READER)
    third = prepare(db, key="third-run", scope=ONE, operation="rerun", source_plan_id=child)
    assert db.get(DBCapabilityPlan, third["items"][0]["plan_id"]).attempt == 3
    assert workflows.read_preparation(db, actor=READER, idempotency_key="source")["operation"] == "prepare"
    assert original["preparation_id"] != third["preparation_id"]


def test_rerun_rejects_wrong_business_legacy_source_owner_and_multiple_targets(db):
    saved, parent = completed(db)
    rejected("rerun_request_mismatch", lambda: prepare(db, key="changed", scope=ONE,
              slots={"page_size": 7}, operation="rerun", source_plan_id=parent))
    rejected("workflow_input_mismatch", lambda: prepare(db, key="multiple", operation="rerun", source_plan_id=parent))
    rejected("workflow_source_not_found", lambda: prepare(db, actor=OTHER, key="other-owner", scope=ONE,
              operation="rerun", source_plan_id=parent))
    legacy = create(db, instance_id=10)
    plans.execute_plan(db, legacy["plan_id"], actor=READER)
    rejected("rerun_request_mismatch", lambda: prepare(db, key="legacy", scope=ONE,
              operation="rerun", source_plan_id=legacy["plan_id"]))
    assert db.query(DBCapabilityPreparation).count() == 1


def test_unsettled_source_and_persisted_unknown_are_never_replayed(db):
    saved = prepare(db, scope=ONE)
    parent = saved["items"][0]["plan_id"]
    rejected("workflow_rerun_source_unsettled", lambda: prepare(db, key="running-source", scope=ONE,
              operation="rerun", source_plan_id=parent))
    plans.execute_plan(db, parent, actor=READER)
    plan = db.get(DBCapabilityPlan, parent)
    plan.outcome = "unknown"
    step = db.query(DBCapabilityPlanStep).filter_by(plan_id=parent).one()
    step.status = "unknown"
    db.commit()
    rejected("workflow_rerun_source_unsettled", lambda: prepare(db, key="unknown-source", scope=ONE,
              operation="rerun", source_plan_id=parent))
    assert db.query(DBCapabilityPlan).count() == 1


def test_revision_infers_fixed_recipe_changes_cas_and_invalidates_old_receipt(db):
    saved = prepare(db, scope=ONE)
    plan_id = saved["items"][0]["plan_id"]
    result = workflows.revise_workflow(db, plan_id, actor=READER, expected_revision=1,
                                      slots={"page_size": 7}, scope=ONE)
    assert result["revision"] == 2 and result["binding_hash"] != saved["items"][0]["binding_hash"]
    assert result["workflow_binding"]["input"]["slots"]["page_size"] == 7
    assert db.query(SecurityAuditEvent).filter_by(action="revise_plan", target_id=plan_id).count() == 1
    rejected("plan_revision_conflict", lambda: workflows.revise_workflow(db, plan_id, actor=READER,
              expected_revision=1, slots={"page_size": 8}, scope=ONE))
    rejected("preparation_binding_changed", lambda: workflows.read_preparation(
        db, actor=READER, idempotency_key="workflow-request"))


def test_revision_respects_real_approval_reset_and_same_transaction_audit_failure(inspection_env, monkeypatch):
    env = inspection_env
    db = env.db
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    approver = User(username="workflow-approver", password_hash="unused", role="admin", status="active")
    db.add(approver)
    db.commit()
    actor, reviewer = {"user_id": env.actor.id}, {"user_id": approver.id}
    registry.sync_definitions(db, actor=reviewer)
    target = {"mode": "explicit", "targets": [{"instance_id": env.instance.id}]}
    saved = prepare(db, "inspection.collect", actor=actor, scope=target, slots={"policy_id": env.policy.id})
    plan_id = saved["items"][0]["plan_id"]
    assert env.collections == env.probes == env.seed_calls == []
    assert db.query(DBInspectionJob).count() == 0
    plan_governance.request_approval(db, plan_id, actor=actor, expected_revision=1)
    plan_governance.approve_plan(db, plan_id, actor=reviewer, expected_revision=1,
                                 expected_binding_hash=saved["items"][0]["binding_hash"])
    def denied(*args, **kwargs):
        raise RuntimeError("isolated revision audit failure")
    with monkeypatch.context() as scope_patch:
        scope_patch.setattr(access_control, "append_security_audit", denied)
        rejected("workflow_storage_unavailable", lambda: workflows.revise_workflow(db, plan_id, actor=actor,
                  expected_revision=1, slots={"policy_id": env.policy.id, "timeout_seconds": 901}, scope=target))
    db.expire_all()
    assert db.get(DBCapabilityPlan, plan_id).approval_state == "approved"
    assert db.query(DBCapabilityPlanRevision).filter_by(plan_id=plan_id).count() == 1
    revised = workflows.revise_workflow(db, plan_id, actor=actor, expected_revision=1,
                                       slots={"policy_id": env.policy.id, "timeout_seconds": 901}, scope=target)
    assert revised["revision"] == 2 and revised["approved_by"] is None
    assert revised["approval_state"] == "pending"


def test_valid_collection_recipe_with_release_gate_closed_never_creates_a_plan_or_job(inspection_env, monkeypatch):
    env = inspection_env
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", False)
    admin = User(username="workflow-release-admin", password_hash="unused", role="admin", status="active")
    env.db.add(admin)
    env.db.commit()
    registry.sync_definitions(env.db, actor={"user_id": admin.id})
    rejected("capability_not_released", lambda: prepare(env.db, "inspection.collect",
        actor={"user_id": env.actor.id}, slots={"policy_id": env.policy.id},
        scope={"mode": "explicit", "targets": [{"instance_id": env.instance.id}]}))
    assert env.db.query(DBCapabilityPlan).count() == env.db.query(DBInspectionJob).count() == 0
    assert env.db.query(DBCapabilityPreparation).count() == 0
    assert env.collections == env.probes == env.seed_calls == []


@pytest.mark.parametrize("action,state", [("pause", "paused"), ("stop", "stopped")])
def test_actual_local_control_checkpoint_cannot_be_replaced_by_rerun(db, action, state):
    from app.services import plan_control

    saved = prepare(db, actor=ADMIN, scope=ONE)
    plan_id = saved["items"][0]["plan_id"]
    batch = plan_queue.enqueue_batch(db, [plan_id], actor=ADMIN, idempotency_key="control-batch")
    execution = db.query(DBCapabilityExecution).filter_by(batch_id=batch["batch_id"]).one()
    control = plan_control.request_control(db, execution.id, actor=ADMIN, action=action,
        expected_control_version=0, idempotency_key="control-request", reason="isolated workflow boundary")
    db.expire_all()
    assert db.get(DBCapabilityExecution, execution.id).state == state
    rejected("workflow_rerun_source_unsettled", lambda: prepare(db, actor=ADMIN, key="not-resume", scope=ONE,
              operation="rerun", source_plan_id=plan_id))
    assert db.query(DBCapabilityPlan).count() == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_partial_requires_actual_terminal_evidence_and_unknown_is_not_cleared(db, monkeypatch, unknown):
    from app.services.capability_handler import HANDLERS

    def failed_policy_read(ctx):
        raise ValueError("isolated read failure")
    saved = prepare(db, "inspection.context.read", scope=ONE)
    plan_id = saved["items"][0]["plan_id"]
    with monkeypatch.context() as fixture:
        fixture.setitem(HANDLERS, "inspection.list_policies", failed_policy_read)
        result = plans.execute_plan(db, plan_id, actor=READER)
    assert result["outcome"] == "partial"
    if unknown:
        row = db.query(DBCapabilityPlanStep).filter_by(plan_id=plan_id, seq=2).one()
        row.status = "unknown"
        db.commit()
        rejected("workflow_rerun_source_unsettled", lambda: prepare(db, "inspection.context.read", key="partial-run",
                  scope=ONE, operation="rerun", source_plan_id=plan_id))
        assert row.status == "unknown"
    else:
        child = prepare(db, "inspection.context.read", key="partial-run", scope=ONE,
                        operation="rerun", source_plan_id=plan_id)
        assert child["source_plan_id"] == plan_id


def test_concurrent_same_key_is_one_atomic_preparation_and_one_plan_set(db):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    db.rollback()
    def once(_):
        with factory() as fresh:
            return prepare(fresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, range(2)))
    assert results[0]["preparation_id"] == results[1]["preparation_id"]
    assert db.query(DBCapabilityPlan).count() == 2
    assert db.query(DBCapabilityPreparation).count() == 1


def test_new_interpreter_recovers_lost_preparation_response_from_exact_original_key(db):
    saved = prepare(db, key="response-lost")
    root = Path(__file__).resolve().parents[2]
    script = """
import json, socket, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
def denied(*args, **kwargs):
    raise AssertionError('no live targets')
socket.socket.connect = denied
from app.services.workflow_service import read_preparation
engine = create_engine(sys.argv[1])
with Session(engine, autoflush=False) as db:
    value = read_preparation(db, actor={'user_id': 2}, idempotency_key='response-lost')
    sys.stdout.write(json.dumps(value))
engine.dispose()
"""
    completed_process = subprocess.run([sys.executable, "-c", script, str(db.get_bind().url)], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "backend"), "DATABASE_URL": "sqlite://", "DB_TYPE": "sqlite",
             "APP_MODE": "quality_test", "DEBUG": "false", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30, check=True)
    read = json.loads(completed_process.stdout)
    assert read["preparation_id"] == saved["preparation_id"]
    assert [item["plan_id"] for item in read["items"]] == [item["plan_id"] for item in saved["items"]]
    assert all(item["reused"] for item in read["items"])
