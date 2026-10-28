"""C06 on isolated file-backed SQLite; no platform or external database is started."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import create_engine, inspect as inspect_sql, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from test_capability_plan import ADMIN, OTHER, READER, create, db  # noqa: F401
from test_inspection_plan_adapter import env as inspection_env  # noqa: F401
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanStep
from app.models.user import User
from app.services import plan_queue as queue, plan_service as plans
from app.services import plan_governance, plan_validation
from app.tasks import plan_worker


@pytest.fixture
def factory(db):
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=True)


def enqueue(db, ids=None, *, key="batch-key", actor=READER, **kwargs):
    ids = ids if ids is not None else [create(db)["plan_id"]]
    return queue.enqueue_batch(db, ids, actor=actor, idempotency_key=key, **kwargs)


def member(db, batch):
    return db.query(DBCapabilityExecution).filter_by(batch_id=batch["batch_id"]).populate_existing().one()


def expire_claim(factory, claim):
    with factory() as fresh:
        fresh.query(DBCapabilityExecution).filter_by(id=claim["id"]).update(
            {"lease_expires_at": queue._now() - timedelta(seconds=1)})
        fresh.commit()


def mark_running(factory, plan_id):
    with factory() as fresh:
        plan = fresh.get(DBCapabilityPlan, plan_id)
        plan.status, plan.started_at = "running", queue._now()
        row = fresh.query(DBCapabilityPlanStep).filter_by(plan_id=plan_id).one()
        row.status, row.started_at = "running", queue._now()
        fresh.commit()


def test_durable_acceptance_replay_and_no_ack_success(db, factory):
    batch = enqueue(db)
    assert batch["accepted"] and batch["status"] == "queued" and batch["outcome"] is None
    with factory() as fresh:
        assert queue.read_batch(fresh, batch["batch_id"], actor=READER)["items"] == batch["items"]
        replay = enqueue(fresh, [batch["items"][0]["plan_id"]])
        assert replay["reused"] and replay["batch_id"] == batch["batch_id"]
    assert db.query(DBCapabilityBatch).count() == 1


def test_changed_input_and_duplicate_delivery_are_rejected(db):
    first, second = create(db)["plan_id"], create(db, params={"page": 2})["plan_id"]
    enqueue(db, [first, second])
    for ids, kwargs in (([second, first], {}), ([first, second], {"deadline_seconds": 5})):
        with pytest.raises(queue.QueueError, match="idempotency_conflict"):
            enqueue(db, ids, **kwargs)
    with pytest.raises(queue.QueueError, match="plan_already_enqueued"):
        enqueue(db, [first], key="different")


@pytest.mark.parametrize("ids", [[], ["a"] * 101, ["a", "a"], [None], "not-a-list"])
def test_explicit_targets_required(db, ids):
    with pytest.raises(queue.QueueError, match="invalid_plan_ids"):
        enqueue(db, ids)
    assert db.query(DBCapabilityBatch).count() == 0


def test_clean_session_and_commit_failure_are_not_acceptance(db, monkeypatch):
    plan = create(db)["plan_id"]
    user = db.get(User, 2)
    user.username = "dirty"
    with pytest.raises(queue.QueueError, match="queue_requires_clean_session"):
        enqueue(db, [plan])
    db.rollback()
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(OperationalError("synthetic", {}, None)))
    with pytest.raises(OperationalError):
        enqueue(db, [plan])
    assert db.query(DBCapabilityBatch).count() == 0


def test_owner_and_permission_revocation_filter_read_list_and_replay(db):
    batch = enqueue(db)
    with pytest.raises(queue.QueueError, match="batch_not_found"):
        queue.read_batch(db, batch["batch_id"], actor=OTHER)
    assert queue.list_batches(db, actor=OTHER)["total"] == 0
    db.get(User, 2).status = "disabled"
    db.commit()
    with pytest.raises(Exception, match="actor_inactive"):
        enqueue(db, [batch["items"][0]["plan_id"]])


def test_enqueue_cannot_take_another_owners_plan_even_for_admin(db):
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        enqueue(db, [create(db)["plan_id"]], actor=ADMIN)


def test_real_concurrent_enqueue_is_idempotent_and_hard_cap_is_serialized(db, factory, monkeypatch):
    first = create(db)["plan_id"]
    db.rollback()
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait()
        with factory() as fresh:
            return enqueue(fresh, [first])["batch_id"]
    with ThreadPoolExecutor(2) as pool:
        assert len(set(pool.map(lambda _: submit(), range(2)))) == 1
    monkeypatch.setattr(queue, "MAX_PENDING", 2)
    second, third = create(db, params={"page": 2})["plan_id"], create(db, params={"page": 3})["plan_id"]
    db.rollback()
    barrier = threading.Barrier(2)
    def bounded(plan_id):
        barrier.wait()
        with factory() as fresh:
            try:
                return enqueue(fresh, [plan_id], key=plan_id)["status"]
            except queue.QueueError as exc:
                return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(bounded, [second, third])) == ["queue_full", "queued"]


def test_actual_executor_settles_and_new_session_reads_real_result(db, factory):
    batch = enqueue(db)
    db.rollback()
    result = queue.process_one(factory, worker_id="worker-a")
    assert result["queue_state"] == "archived" and result["outcome"] == "succeeded"
    with factory() as fresh:
        view = queue.read_batch(fresh, batch["batch_id"], actor=READER)
        assert view["outcome"] == "succeeded" and view["settled_plans"] == 1
        assert view["items"][0]["result_ref"]["kind"] == "capability_plan_steps"
    assert queue.process_one(factory, worker_id="worker-b") is None


def test_two_real_workers_only_one_executes(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    calls = []
    original = plans.registry.execute_read
    def count(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(plans.registry, "execute_read", count)
    barrier = threading.Barrier(2)
    def consume(worker):
        barrier.wait()
        return queue.process_one(factory, worker_id=worker)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(consume, ("one", "two")))
    assert len(calls) == 1 and sum(result is not None for result in results) == 1
    assert queue.read_batch(db, batch["batch_id"], actor=READER)["outcome"] == "succeeded"


def test_restart_recovers_claimed_but_never_started(db, factory):
    batch = enqueue(db)
    db.rollback()
    old = queue._claim(factory, "dead-before-start")
    expire_claim(factory, old)
    url = str(db.get_bind().url)
    restarted_engine = create_engine(url, connect_args={"timeout": 15, "check_same_thread": False})
    restarted = sessionmaker(bind=restarted_engine, autoflush=False)
    try:
        completed = queue.process_one(restarted, worker_id="after-restart")
        assert completed["outcome"] == "succeeded"
        with restarted() as fresh:
            assert member(fresh, batch).attempt == 2
            guard = queue.ExecutionGuard(old, fresh)
            with pytest.raises(queue.ExecutionLeaseLost):
                guard()
            fresh.rollback()
    finally:
        restarted_engine.dispose()


def test_running_dead_worker_never_replays_and_blocks_same_target(db, factory, monkeypatch):
    first = enqueue(db)
    second_plan = create(db, params={"page": 2})["plan_id"]
    enqueue(db, [second_plan], key="next-on-same-target")
    db.rollback()
    claim = queue._claim(factory, "dead-after-start")
    mark_running(factory, claim["plan_id"])
    expire_claim(factory, claim)
    def forbidden(*args, execution_guard=None, **kwargs):
        pytest.fail("an uncertain started plan must not be executed again")
    monkeypatch.setattr(plans, "execute_plan", forbidden)
    assert queue.process_one(factory, worker_id="replacement") is None
    view = queue.read_batch(db, first["batch_id"], actor=READER)
    assert view["status"] == "reconciliation_required" and view["outcome"] is None
    assert db.get(DBCapabilityPlan, claim["plan_id"]).status == "running"


def test_settled_persisted_evidence_recovers_after_crash_before_ack(db, factory):
    batch = enqueue(db)
    db.rollback()
    claim = queue._claim(factory, "crash-after-result")
    with factory() as fresh:
        plans.execute_plan(fresh, claim["plan_id"], actor=READER)
    expire_claim(factory, claim)
    assert queue.process_one(factory, worker_id="reaper") is None
    view = queue.read_batch(db, batch["batch_id"], actor=READER)
    assert view["outcome"] == "succeeded" and view["queue_counts"] == {"archived": 1}


def test_uncertain_executor_exception_is_reconciliation_not_failure_or_replay(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    def interrupted(session, plan_id, *, actor, execution_guard):
        execution_guard()
        plan = session.get(DBCapabilityPlan, plan_id)
        plan.status, plan.started_at = "running", queue._now()
        session.commit()
        raise RuntimeError("synthetic sensitive payload must not be persisted")
    monkeypatch.setattr(plans, "execute_plan", interrupted)
    result = queue.process_one(factory, worker_id="interrupted")
    assert result["queue_state"] == "reconciliation_required"
    assert result["outcome"] is None and result["reason"] == "executor_interrupted"
    assert queue.process_one(factory, worker_id="must-not-retry") is None
    assert member(db, batch).attempt == 1


def test_deadline_expires_waiting_but_running_requires_reconciliation(db, factory):
    first = enqueue(db)
    db.rollback()
    with factory() as fresh:
        fresh.get(DBCapabilityBatch, first["batch_id"]).deadline_at = queue._now() - timedelta(seconds=1)
        fresh.commit()
    assert queue.process_one(factory, worker_id="expired-queued") is None
    assert queue.read_batch(db, first["batch_id"], actor=READER)["outcome"] == "expired"
    second = enqueue(db, [create(db, params={"page": 2})["plan_id"]], key="running-deadline")
    db.rollback()
    claim = queue._claim(factory, "old")
    mark_running(factory, claim["plan_id"])
    with factory() as fresh:
        fresh.get(DBCapabilityBatch, second["batch_id"]).deadline_at = queue._now() - timedelta(seconds=1)
        fresh.commit()
    assert queue.process_one(factory, worker_id="expired-running") is None
    assert queue.read_batch(db, second["batch_id"], actor=READER)["status"] == "reconciliation_required"


def test_cancel_queued_is_delivery_cancel_and_running_is_only_request(db, factory):
    batch = enqueue(db)
    result = queue.cancel_batch(db, batch["batch_id"], actor=READER)
    assert result["outcome"] == "cancelled" and result["items"][0]["status"] == "validated"
    assert queue.process_one(factory, worker_id="none") is None
    second = enqueue(db, [create(db, params={"page": 2})["plan_id"]], key="cancel-running")
    db.rollback()
    claim = queue._claim(factory, "in-flight")
    mark_running(factory, claim["plan_id"])
    result = queue.cancel_batch(db, second["batch_id"], actor=READER)
    assert result["cancel_requested"] and result["status"] == "running" and result["outcome"] is None


def test_editability_freezes_active_binding_and_allows_only_cancelled_unstarted(db, factory):
    plan_id = create(db)["plan_id"]
    queue.assert_plan_editable(db, plan_id)
    db.rollback()
    batch = enqueue(db, [plan_id])
    for state in ("queued", "blocked", "reconciliation_required", "archived", "expired"):
        member(db, batch).state = state
        db.commit()
        with pytest.raises(plans.PlanError, match="plan_batch_binding_active"):
            queue.assert_plan_editable(db, plan_id)
        db.rollback()
    row = member(db, batch)
    row.state = "queued"
    db.commit()
    queue.cancel_batch(db, batch["batch_id"], actor=READER)
    queue.assert_plan_editable(db, plan_id)
    db.rollback()
    mark_running(factory, plan_id)
    with pytest.raises(plans.PlanError, match="executed_plan_requires_rerun"):
        queue.assert_plan_editable(db, plan_id)
    db.rollback()


def test_revocation_at_claim_does_not_execute(db, factory):
    batch = enqueue(db)
    db.get(User, 2).status = "disabled"
    db.commit()
    assert queue.process_one(factory, worker_id="revoked") is None
    assert member(db, batch).state == "blocked"
    assert db.get(DBCapabilityPlan, batch["items"][0]["plan_id"]).started_at is None


def test_guard_checks_live_token_lease_and_owner(db, factory):
    enqueue(db)
    db.rollback()
    claim = queue._claim(factory, "owner")
    with factory() as fresh:
        queue.ExecutionGuard(claim, fresh)()
        fresh.commit()
    with factory() as fresh:
        fresh.query(DBCapabilityExecution).filter_by(id=claim["id"]).update({"owner_token": "new-owner"})
        fresh.commit()
    with factory() as fresh, pytest.raises(queue.ExecutionLeaseLost):
        queue.ExecutionGuard(claim, fresh)()


def test_independent_heartbeat_renews_and_refuses_expired_lease(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    claim = queue._claim(factory, "heartbeat")
    with factory() as fresh:
        before = member(fresh, batch).heartbeat_at
    monkeypatch.setattr(queue, "HEARTBEAT_SECONDS", 0.01)
    stop = threading.Event()
    thread = threading.Thread(target=queue._heartbeat, args=(factory, claim, stop))
    thread.start()
    try:
        limit = time.monotonic() + 3
        while time.monotonic() < limit:
            with factory() as fresh:
                if member(fresh, batch).heartbeat_at > before:
                    break
        else:
            pytest.fail("independent heartbeat did not renew")
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    expire_claim(factory, claim)
    with pytest.raises(queue.ExecutionLeaseLost):
        queue._renew(factory, claim)


def test_guard_integration_is_required_before_any_claim(db, factory, monkeypatch):
    batch = enqueue(db)
    def old_executor(db, plan_id, *, actor):
        pytest.fail("unfenced executor must not be called")
    monkeypatch.setattr(plans, "execute_plan", old_executor)
    with pytest.raises(queue.QueueError, match="execution_guard_not_integrated"):
        queue.process_one(factory, worker_id="refuse")
    assert member(db, batch).attempt == 0


def test_real_handler_has_no_queue_write_lock_and_other_batch_can_be_accepted(db, factory, monkeypatch):
    enqueue(db)
    second = create(db, params={"page": 2})["plan_id"]
    db.rollback()
    accepted = []
    original = plans.registry.execute_read
    def independent_acceptance(*args, **kwargs):
        with factory() as fresh:
            accepted.append(enqueue(fresh, [second], key="during-handler")["batch_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(plans.registry, "execute_read", independent_acceptance)
    result = queue.process_one(factory, worker_id="does-not-hold-lock")
    assert accepted and result["outcome"] == "succeeded"


@pytest.mark.parametrize("handler_failure", [False, True])
def test_actual_executor_cannot_publish_success_or_failure_after_lease_loss(db, factory, monkeypatch, handler_failure):
    batch = enqueue(db)
    plan_id = batch["items"][0]["plan_id"]
    db.rollback()
    original = plans.registry.execute_read
    def lose_lease(*args, **kwargs):
        with factory() as fresh:
            row = member(fresh, batch)
            row.lease_expires_at = queue._now() - timedelta(seconds=1)
            fresh.commit()
        if handler_failure:
            raise RuntimeError("synthetic handler failure after owner loss")
        return original(*args, **kwargs)
    monkeypatch.setattr(plans.registry, "execute_read", lose_lease)
    result = queue.process_one(factory, worker_id="stale")
    assert result["queue_state"] == "reconciliation_required" and result["outcome"] is None
    with factory() as fresh:
        plan = fresh.get(DBCapabilityPlan, plan_id)
        step = fresh.query(DBCapabilityPlanStep).filter_by(plan_id=plan_id).one()
        assert plan.status == "running" and plan.outcome is None and plan.result_ref_json is None
        assert step.status == "running" and step.result_ref_json is None
    assert queue.process_one(factory, worker_id="not-replayed") is None


def test_before_commit_rechecks_approval_without_clobbering_staged_outcome(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    revoked = False
    original_read, original_approval = plans.registry.execute_read, plan_validation.approval_block_reason
    def after_external_read(*args, **kwargs):
        nonlocal revoked
        result = original_read(*args, **kwargs)
        revoked = True
        return result
    def approve_projection(plan, db=None):
        if revoked and type(plan).__name__ == "SimpleNamespace":
            return "approval_revoked"
        return original_approval(plan, db=db)
    monkeypatch.setattr(plans.registry, "execute_read", after_external_read)
    monkeypatch.setattr(plan_validation, "approval_block_reason", approve_projection)
    result = queue.process_one(factory, worker_id="approval-race")
    assert result["queue_state"] == "reconciliation_required" and result["outcome"] is None
    with factory() as fresh:
        step = fresh.query(DBCapabilityPlanStep).filter_by(plan_id=batch["items"][0]["plan_id"]).one()
        assert step.status == "running" and step.result_ref_json is None


def test_cancel_then_revise_and_reenqueue_preserves_original_binding(db, factory):
    batch = enqueue(db)
    plan_id = batch["items"][0]["plan_id"]
    original_hash = batch["items"][0]["binding_hash"]
    with pytest.raises(plans.PlanError, match="plan_batch_binding_active"):
        plan_governance.revise_plan(db, plan_id, actor=READER, expected_revision=1,
                                   capability_id="inspection.run.query", definition_version=1, params={"page": 2})
    db.rollback()
    queue.cancel_batch(db, batch["batch_id"], actor=READER)
    revised = plan_governance.revise_plan(db, plan_id, actor=READER, expected_revision=1,
                                         capability_id="inspection.run.query", definition_version=1, params={"page": 2})
    assert revised["status"] == "validated"
    historical = queue.read_batch(db, batch["batch_id"], actor=READER)
    assert historical["items"][0]["bound_revision"] == 1
    assert historical["items"][0]["binding_hash"] == original_hash
    assert not historical["items"][0]["binding_current"] and historical["outcome"] == "cancelled"
    next_batch = enqueue(db, [plan_id], key="revised-batch")
    assert next_batch["items"][0]["bound_revision"] == 2
    assert next_batch["items"][0]["binding_hash"] != original_hash
    db.rollback()
    assert queue.process_one(factory, worker_id="revision-two")["outcome"] == "succeeded"


def test_real_enqueue_and_revision_share_lock_order_without_deadlock(db, factory):
    plan_id = create(db)["plan_id"]
    db.rollback()
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait()
        with factory() as fresh:
            return enqueue(fresh, [plan_id])["batch_id"]
    def revise():
        barrier.wait()
        with factory() as fresh:
            try:
                plan_governance.revise_plan(fresh, plan_id, actor=READER, expected_revision=1,
                                           capability_id="inspection.run.query", definition_version=1, params={"page": 2})
                return "revised"
            except plans.PlanError as exc:
                fresh.rollback()
                return exc.code
    with ThreadPoolExecutor(2) as pool:
        enqueued, revision = pool.submit(submit), pool.submit(revise)
        batch_id, revised = enqueued.result(timeout=20), revision.result(timeout=20)
    assert revised in {"revised", "plan_batch_binding_active"}
    with factory() as fresh:
        view = queue.read_batch(fresh, batch_id, actor=READER)
        assert view["items"][0]["binding_current"]


def test_global_active_limit_and_target_exclusion_are_enforced(db, factory, monkeypatch):
    first = create(db, instance_id=10)["plan_id"]
    second = create(db, instance_id=20)["plan_id"]
    third = create(db, instance_id=10, params={"page": 2})["plan_id"]
    enqueue(db, [first, second, third])
    db.rollback()
    monkeypatch.setattr(queue, "MAX_ACTIVE", 1)
    one = queue._claim(factory, "one")
    assert one["plan_id"] == first
    assert queue._claim(factory, "full") is None
    monkeypatch.setattr(queue, "MAX_ACTIVE", 8)
    two = queue._claim(factory, "different-target")
    assert two["plan_id"] == second
    assert queue._claim(factory, "same-target") is None


def test_scope_recheck_at_claim_filters_list_and_never_executes(db, factory):
    from app.models.access_control import UserAssetScope
    batch = enqueue(db, [create(db, instance_id=10)["plan_id"]])
    db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=20, access_level="read"))
    db.commit()
    assert queue.list_batches(db, actor=READER)["total"] == 0
    assert queue.process_one(factory, worker_id="scope-revoked") is None
    assert member(db, batch).state == "blocked"


def test_returned_success_without_persisted_evidence_never_becomes_batch_success(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    def ack_only(session, plan_id, *, actor, execution_guard):
        return {"status": "settled", "outcome": "succeeded"}
    monkeypatch.setattr(plans, "execute_plan", ack_only)
    result = queue.process_one(factory, worker_id="only-ack")
    assert result["queue_state"] == "blocked" and result["outcome"] is None
    assert queue.read_batch(db, batch["batch_id"], actor=READER)["outcome"] is None


def test_persisted_timeout_unknown_stays_unresolved_and_never_replays(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    def timeout(*args, **kwargs):
        raise TimeoutError("synthetic uncertain completion")
    monkeypatch.setattr(plans.registry, "execute_read", timeout)
    result = queue.process_one(factory, worker_id="timed-out")
    assert result["status"] == "settled" and result["outcome"] == "unknown"
    assert result["queue_state"] == "reconciliation_required" and result["requires_attention"]
    assert queue.process_one(factory, worker_id="must-not-replay") is None
    view = queue.read_batch(db, batch["batch_id"], actor=READER)
    assert view["status"] == "reconciliation_required" and view["outcome"] is None
    assert view["outcomes"] == {"unknown": 1}


def effect_evidence_fixture(db, monkeypatch, outcome="partial"):
    import socket
    from app.models.inspection_job import DBInspectionJob
    from app.models.inspection_policy import DBInspectionPolicy
    from app.services.inspection import jobs, plan_adapter

    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("No field I/O allowed"))
    monkeypatch.setattr(plans.registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    policy = DBInspectionPolicy(policy_key="queue-effect-fixture", name="Queue fixture", enabled=1,
                                db_types_json=["mysql"], item_config_json=[
                                    {"key": "mysql.connection.used_pct", "enabled": True}])
    db.add(policy)
    db.commit()
    version = plans.registry._entry("inspection.run.execute")["version"]
    created = create(db, "inspection.run.execute", actor=ADMIN, instance_id=10,
                     definition_version=version, params={"policy_id": policy.id})
    plan = db.get(DBCapabilityPlan, created["plan_id"])
    step = plans._steps(db, plan.plan_id)[0]
    now = queue._now()
    snapshot = jobs.freeze_policy(policy)
    job = DBInspectionJob(id="a" * 30, instance_id=10, db_type="mysql", requested_by=1,
                          trigger_type=plan_adapter.TRIGGER, policy_id=policy.id, policy_snapshot_json=snapshot,
                          policy_revision=jobs._digest(snapshot), target_fingerprint=step.precondition_json["params"]["target_fingerprint"],
                          idempotency_key=jobs._digest([1, plan_adapter.TRIGGER, f"{plan.plan_id}:1:1"]),
                          status={"succeeded": "success", "unknown": "timed_out"}.get(outcome, outcome), stage="completed",
                          created_at=now, started_at=now, finished_at=now)
    db.add(job)
    db.flush()
    domain_outcome = jobs.outcome_block(job)["outcome"]
    assert domain_outcome == ("success" if outcome == "succeeded" else outcome)
    value = {"schema": "inspection.plan_result/v1", "job_id": job.id, "instance_id": 10,
             "status": job.status, "outcome": domain_outcome, "report": {}, "run": {}}
    step.status, step.started_at, step.finished_at = outcome, now, now
    step.result_ref_json = plans._result_ref(plan.capability_snapshot_json["definitions"][0], value)
    plan.status, plan.outcome, plan.started_at, plan.finished_at = "settled", outcome, now, now
    plan.result_ref_json = {"kind": "capability_plan_steps", "plan_id": plan.plan_id,
                            "schema": "capability.plan_results/v1", "steps": [1]}
    db.commit()
    return plan, step, job


@pytest.mark.parametrize("outcome", ["succeeded", "partial", "failed", "expired"])
def test_real_domain_evidence_including_single_partial_step_can_archive(db, monkeypatch, outcome):
    plan, step, _ = effect_evidence_fixture(db, monkeypatch, outcome)
    original_reference = json.dumps(step.result_ref_json, sort_keys=True)
    assert queue._evidence(db, plan)
    result = enqueue(db, [plan.plan_id], actor=ADMIN)
    assert result["outcome"] == outcome and result["queue_counts"] == {"archived": 1}
    assert json.dumps(step.result_ref_json, sort_keys=True) == original_reference
    assert step.result_ref_json["inline"]["outcome"] == ("success" if outcome == "succeeded" else outcome)


@pytest.mark.parametrize("change", ["missing", "owner", "target", "nonterminal", "outcome", "execution_id", "policy", "finished"])
def test_inspection_reference_needs_real_matching_terminal_domain_run(db, monkeypatch, change):
    plan, _, job = effect_evidence_fixture(db, monkeypatch)
    if change == "missing":
        db.delete(job)
    elif change == "owner":
        job.requested_by = 3
    elif change == "target":
        job.instance_id = 20
    elif change == "nonterminal":
        job.status = "queued"
    elif change == "outcome":
        job.status = "success"
    elif change == "execution_id":
        job.idempotency_key = "different-execution"
    elif change == "policy":
        job.policy_snapshot_json = {"wrong": "policy"}
    else:
        job.finished_at = None
    db.commit()
    assert not queue._evidence(db, plan)


@pytest.mark.parametrize("outcome,wrong_step", [
    ("succeeded", "partial"), ("partial", "succeeded"), ("failed", "succeeded"),
    ("expired", "succeeded"), ("unknown", "succeeded"),
])
def test_domain_outcome_must_also_match_step_verdict(db, monkeypatch, outcome, wrong_step):
    plan, step, _ = effect_evidence_fixture(db, monkeypatch, outcome)
    step.status = wrong_step
    plan.outcome = wrong_step
    db.commit()
    assert not queue._evidence(db, plan)


@pytest.mark.parametrize("alias", ["succeeded", "SUCCESS", "Success", "ok"])
def test_inspection_domain_result_rejects_plan_success_alias(db, monkeypatch, alias):
    plan, step, job = effect_evidence_fixture(db, monkeypatch, "succeeded")
    assert job.status == "success" and step.result_ref_json["inline"]["outcome"] == "success"
    wrong = {**step.result_ref_json["inline"], "outcome": alias}
    with pytest.raises(plans.PlanError, match="result_unknown"):
        plans._result_ref(plan.capability_snapshot_json["definitions"][0], wrong)
    step.result_ref_json = {**step.result_ref_json, "inline": wrong}
    db.commit()
    assert not queue._evidence(db, plan)


def test_real_inspection_success_archives_the_batch_not_just_the_plan(inspection_env, monkeypatch):
    from test_inspection_plan_workflow import admit, prepare

    requester, approver, saved = prepare(inspection_env, monkeypatch)
    receipt = admit(inspection_env, requester, approver, saved)
    finished = queue.process_one(inspection_env.factory, worker_id="verify-successful-batch")
    assert finished["outcome"] == "succeeded" and finished["queue_state"] == "archived", finished
    inspection_env.db.expire_all()
    batch = queue.read_batch(inspection_env.db, receipt["batch_id"], actor=requester)
    assert batch["status"] == "settled" and batch["outcome"] == "succeeded"
    assert batch["queue_counts"] == {"archived": 1} and not batch["requires_attention"]


def test_failed_effect_reference_is_also_checked_against_domain_owner(db, monkeypatch):
    plan, _, job = effect_evidence_fixture(db, monkeypatch, "failed")
    job.requested_by = 2
    db.commit()
    assert not queue._evidence(db, plan)


def test_unknown_effect_reference_is_validated_but_never_releases_target(db, monkeypatch):
    plan, step, _ = effect_evidence_fixture(db, monkeypatch, "unknown")
    calls, original = [], plans.validate_effect_reference
    def validate(session, stored_plan, stored_step):
        calls.append(stored_step.seq)
        return original(session, stored_plan, stored_step)
    monkeypatch.setattr(plans, "validate_effect_reference", validate)
    assert not queue._evidence(db, plan)
    assert calls == [step.seq]


def test_unknown_then_cancel_still_blocks_same_target_but_not_other_targets(db, factory, monkeypatch):
    first = enqueue(db, [create(db, instance_id=10)["plan_id"]])
    db.rollback()
    original = plans.registry.execute_read
    def timeout(*args, **kwargs):
        raise TimeoutError("synthetic unresolved execution")
    monkeypatch.setattr(plans.registry, "execute_read", timeout)
    result = queue.process_one(factory, worker_id="unknown-before-cancel")
    assert result["queue_state"] == "reconciliation_required" and result["outcome"] == "unknown"
    monkeypatch.setattr(plans.registry, "execute_read", original)
    cancelled = queue.cancel_batch(db, first["batch_id"], actor=READER)
    assert cancelled["cancel_requested"] and cancelled["queue_counts"] == {"reconciliation_required": 1}
    assert cancelled["outcome"] is None
    waiting = create(db, instance_id=10, params={"page": 2})["plan_id"]
    enqueue(db, [waiting], key="blocked-by-unknown")
    db.rollback()
    assert queue.process_one(factory, worker_id="cannot-reuse-target") is None
    available = create(db, instance_id=20)["plan_id"]
    enqueue(db, [available], key="other-target")
    db.rollback()
    assert queue.process_one(factory, worker_id="other-target-can-run")["outcome"] == "succeeded"
    with factory() as fresh:
        plan = fresh.get(DBCapabilityPlan, waiting)
        assert plan.status == "validated" and plan.started_at is None
        assert member(fresh, first).state == "reconciliation_required" and member(fresh, first).attempt == 1


def test_cancel_during_handler_keeps_request_and_publishes_truthful_completion(db, factory, monkeypatch):
    batch = enqueue(db)
    db.rollback()
    original = plans.registry.execute_read
    def request_cancel(*args, **kwargs):
        with factory() as control:
            response = queue.cancel_batch(control, batch["batch_id"], actor=READER)
            assert response["cancel_requested"] and response["status"] == "running"
            assert response["outcome"] is None and response["queue_counts"] == {"running": 1}
        return original(*args, **kwargs)
    monkeypatch.setattr(plans.registry, "execute_read", request_cancel)
    finished = queue.process_one(factory, worker_id="cancel-is-not-stop")
    assert finished["queue_state"] == "archived" and finished["outcome"] == "succeeded"
    with factory() as fresh:
        response = queue.read_batch(fresh, batch["batch_id"], actor=READER)
        assert response["cancel_requested"] and response["outcome"] == "succeeded"
        assert response["queue_counts"] == {"archived": 1}


def test_cancel_recovered_unstarted_claim_fences_old_worker_commit(db, factory):
    from app.core.transaction_lock import lock_transaction

    batch = enqueue(db)
    db.rollback()
    claim = queue._claim(factory, "old-owner-before-start")
    expire_claim(factory, claim)
    with factory() as fresh:
        lock_transaction(fresh, queue.LOCK_KEY)
        queue._recover(fresh, queue._now())
        fresh.commit()
    assert queue.cancel_batch(db, batch["batch_id"], actor=READER)["outcome"] == "cancelled"
    db.rollback()
    with factory() as stale:
        guard = queue.ExecutionGuard(claim, stale)
        try:
            plan = stale.get(DBCapabilityPlan, claim["plan_id"])
            step = plans._steps(stale, claim["plan_id"])[0]
            plan.status, plan.started_at = "running", queue._now()
            step.status, step.finished_at = "failed", queue._now()
            with pytest.raises(plans.PlanOwnershipLost, match="execution_ownership_lost"):
                stale.commit()
            stale.rollback()
        finally:
            guard.close()
    with factory() as fresh:
        plan = fresh.get(DBCapabilityPlan, claim["plan_id"])
        step = plans._steps(fresh, claim["plan_id"])[0]
        assert plan.status == "validated" and plan.started_at is None and plan.outcome is None
        assert step.status == "pending" and step.result_ref_json is None


def test_unstarted_ownership_loss_is_safe_to_recover(db, factory):
    enqueue(db)
    db.rollback()
    claim = queue._claim(factory, "never-started")
    expire_claim(factory, claim)
    result = queue._complete(factory, claim, "execution_lease_lost")
    assert result["queue_state"] == "queued" and result["outcome"] is None
    assert queue.process_one(factory, worker_id="safe-replacement")["outcome"] == "succeeded"


@pytest.mark.parametrize("started", [False, True])
def test_real_process_death_and_cli_restart_use_durable_evidence(db, factory, started):
    batch = enqueue(db)
    db.rollback()
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    env = dict(os.environ, DATABASE_URL=str(db.get_bind().url), DB_TYPE="sqlite",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root / "backend"))
    child = """
import os
from app.core.database import SessionLocal
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanStep
from app.services import plan_queue as q
claim = q._claim(SessionLocal, 'crashed-child')
if os.environ['SYNTHETIC_STARTED'] == '1':
    with SessionLocal() as session:
        plan = session.get(DBCapabilityPlan, claim['plan_id'])
        plan.status, plan.started_at = 'running', q._now()
        step = session.query(DBCapabilityPlanStep).filter_by(plan_id=claim['plan_id']).one()
        step.status, step.started_at = 'running', q._now()
        session.commit()
os._exit(17)
"""
    crashed = subprocess.run([sys.executable, "-c", child], cwd=root,
                             env=dict(env, SYNTHETIC_STARTED="1" if started else "0"),
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 17, crashed.stderr
    with factory() as fresh:
        row = member(fresh, batch)
        assert row.state == "running" and row.worker_id == "crashed-child" and row.attempt == 1
        row.lease_expires_at = queue._now() - timedelta(seconds=1)
        fresh.commit()
    restarted = subprocess.run([sys.executable, "scripts/capability_worker.py", "--once"], cwd=root,
                               env=env, capture_output=True, text=True, timeout=30)
    assert restarted.returncode == 0, restarted.stderr
    with factory() as fresh:
        result = queue.read_batch(fresh, batch["batch_id"], actor=READER)
        if started:
            assert result["status"] == "reconciliation_required" and result["outcome"] is None
            assert member(fresh, batch).attempt == 1
        else:
            assert result["outcome"] == "succeeded" and member(fresh, batch).attempt == 2


def test_migration_idempotent_and_nonempty_downgrade_is_atomic(tmp_path):
    migration = importlib.import_module("app.migrations.versions.v20261020_04_capability_execution")
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            migration.upgrade(connection)
            migration.upgrade(connection)
            connection.execute(DBCapabilityBatch.__table__.insert().values(
                batch_id="fixture", requested_by=2, idempotency_key="one", input_hash="a" * 64,
                deadline_seconds=30, created_at=queue._now(), deadline_at=queue._now()))
            with pytest.raises(RuntimeError, match="retained"):
                migration.downgrade(connection)
            assert inspect_sql(connection).has_table("db_capability_execution")
            assert connection.execute(select(DBCapabilityBatch.batch_id)).scalar() == "fixture"
            connection.execute(DBCapabilityBatch.__table__.delete())
            migration.downgrade(connection)
            migration.downgrade(connection)
    finally:
        engine.dispose()


def test_worker_once_backoff_and_stop(monkeypatch):
    calls = []
    def consume(*args, **kwargs):
        calls.append(kwargs)
        return None
    monkeypatch.setattr(plan_worker, "process_one", consume)
    assert plan_worker.run_worker(None, worker_id="once", once=True) == 0
    assert len(calls) == 1
    class Stop:
        def __init__(self):
            self.delays = []
        def is_set(self):
            return len(self.delays) == 4
        def wait(self, value):
            self.delays.append(value)
    stop = Stop()
    assert plan_worker.run_worker(None, worker_id="idle", stop=stop, idle_seconds=0.1, max_backoff=0.3) == 0
    assert stop.delays == [0.1, 0.2, 0.3, 0.3]


def test_real_cli_once_and_graceful_sigterm_without_platform(db, tmp_path):
    batch = enqueue(db)
    db.rollback()
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    env = dict(os.environ, DATABASE_URL=str(db.get_bind().url), DB_TYPE="sqlite",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root / "backend"))
    cwd = str(root)
    completed = subprocess.run([sys.executable, "scripts/capability_worker.py", "--once"],
                               cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in completed.stderr.splitlines() if line.startswith("{")]
    assert any(record.get("execution", {}).get("queue_state") == "archived" for record in records)
    assert queue.read_batch(db, batch["batch_id"], actor=READER)["outcome"] == "succeeded"
    db.rollback()
    process = subprocess.Popen([sys.executable, "scripts/capability_worker.py", "--idle-seconds", "0.1"],
                               cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(2)
        assert process.poll() is None
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
