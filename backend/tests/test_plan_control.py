"""Actual local multi-step control races on isolated file-backed SQLite."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.models.capability_control import DBCapabilityControlCommand as Command, DBCapabilityTargetFence as Fence
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.access_control import RoleActionPermission, SecurityAuditEvent, UserAccessRole, UserAssetScope
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.models.user import User
from app.services import plan_control as control, plan_queue as queue, plan_service as plans
from test_capability_plan import ADMIN, READER, create, db  # noqa: F401
from test_inspection_plan_adapter import env as inspection_env  # noqa: F401
from test_inspection_plan_cancellation import admit_steps


@pytest.fixture
def factory(db):
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=True)


def admit(db, *, key="controlled", count=3):
    db.add(DBInspectionPolicy(policy_key=key, name=key, db_types_json=["mysql"], enabled=1))
    db.commit()
    definitions = ["inspection.template.read", "inspection.run.query", "operations.capability_matrix.read"]
    steps = [{"seq": seq, "capability_id": definitions[seq - 1], "definition_version": 1,
              "depends_on": [seq - 1] if seq > 1 else []} for seq in range(1, count + 1)]
    if count > 1:
        steps[1]["input_refs"] = {"page_size": {"seq": 1, "schema": "inspection.policy_list/v1",
                                                  "field": "total", "type": "integer"}}
    plan = create(db, "inspection.template.read", actor=ADMIN, instance_id=10, steps=steps, idempotency_key="plan-" + key)
    batch = queue.enqueue_batch(db, [plan["plan_id"]], actor=ADMIN, idempotency_key=key, schema_version=2)
    return plan, batch, batch["items"][0]["execution_id"]


def request(db, execution_id, action="pause", version=0, key=None, actor=ADMIN):
    return control.request_control(db, execution_id, actor=actor, action=action,
                                   expected_control_version=version, idempotency_key=key or f"{action}-{version}",
                                   reason="Explicit isolated control request")


def pause_after_first(db, factory, monkeypatch):
    plan, batch, execution_id = admit(db)
    original = plans.registry.execute_read
    calls = []

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[1])
        if len(calls) == 1:
            with factory() as controller:
                value = request(controller, execution_id)
                assert value["command"]["status"] == "accepted"
        return result

    monkeypatch.setattr(plans.registry, "execute_read", wrapped)
    paused = queue.process_one(factory, worker_id="first-owner")
    assert paused["queue_state"] == "paused", paused
    assert len(calls) == 1
    return plan, batch, execution_id, calls


def test_queued_pause_is_durable_and_resume_does_not_extend_ttl(db, factory):
    plan, batch, execution_id = admit(db)
    before = db.get(DBCapabilityBatch, batch["batch_id"]).deadline_at
    value = request(db, execution_id)
    assert value["queue_state"] == value["observed_state"] == "paused"
    assert value["command"]["status"] == "applied" and value["checkpoint_hash"]
    assert value["external_stop"] == value["external_fence"] == "unsupported"
    assert queue.process_one(factory, worker_id="cannot-start-paused") is None
    with factory() as fresh:
        assert control.read_control(fresh, execution_id, actor=ADMIN)["checkpoint_hash"] == value["checkpoint_hash"]
        assert fresh.get(DBCapabilityPlan, plan["plan_id"]).started_at is None
        resumed = request(fresh, execution_id, "resume", 1)
        assert resumed["queue_state"] == "queued" and resumed["command"]["status"] == "accepted"
    result = queue.process_one(factory, worker_id="resumed-owner")
    assert result["queue_state"] == "archived" and result["outcome"] == "succeeded", result
    with factory() as fresh:
        assert fresh.get(DBCapabilityBatch, batch["batch_id"]).deadline_at == before
        assert fresh.get(Command, resumed["command"]["command_id"]).status == "applied"


def test_running_resume_rebuilds_dag_outputs_and_never_replays_completed_step(db, factory, monkeypatch):
    plan, _batch, execution_id, calls = pause_after_first(db, factory, monkeypatch)
    with factory() as fresh:
        saved = fresh.get(DBCapabilityPlan, plan["plan_id"])
        assert saved.status == "running" and saved.outcome is None
        old_started = saved.started_at
        first_ref = deepcopy(plans._steps(fresh, saved.plan_id)[0].result_ref_json)
        member = fresh.get(DBCapabilityExecution, execution_id)
        old_epoch = member.fencing_epoch
        assert member.checkpoint_json["outputs"]["1"] == first_ref["inline"]
        assert [row.status for row in plans._steps(fresh, saved.plan_id)] == ["succeeded", "pending", "pending"]
        assert fresh.get(Fence, member.target_key).hold_state == "paused"
        request(fresh, execution_id, "resume", 1)
    result = queue.process_one(factory, worker_id="new-owner")
    assert result["queue_state"] == "archived" and result["outcome"] == "succeeded", result
    assert calls == ["inspection.template.read", "inspection.run.query", "operations.capability_matrix.read"]
    with factory() as fresh:
        saved = fresh.get(DBCapabilityPlan, plan["plan_id"])
        steps = plans._steps(fresh, saved.plan_id)
        assert saved.started_at == old_started and steps[0].result_ref_json == first_ref
        assert steps[1].result_ref_json["inline"]["page_size"] == first_ref["inline"]["total"]
        assert fresh.get(DBCapabilityExecution, execution_id).fencing_epoch > old_epoch


def test_inflight_pause_is_only_request_until_handler_returns(db, factory, monkeypatch):
    _plan, _batch, execution_id = admit(db)
    entered, release = Event(), Event()
    original = plans.registry.execute_read

    def block(*args, **kwargs):
        if args[1] == "inspection.template.read":
            entered.set()
            assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(plans.registry, "execute_read", block)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(queue.process_one, factory, worker_id="inflight")
        try:
            assert entered.wait(15)
            with factory() as fresh:
                before = fresh.get(DBCapabilityExecution, execution_id).owner_token
                value = request(fresh, execution_id)
                assert value["command"]["status"] == "accepted" and value["queue_state"] == "running"
                assert fresh.get(DBCapabilityExecution, execution_id).owner_token == before
        finally:
            release.set()
        assert future.result(timeout=20)["queue_state"] == "paused"


@pytest.mark.parametrize("started", [False, True])
def test_stop_is_local_checkpoint_not_fabricated_business_cancel(db, factory, monkeypatch, started):
    if started:
        plan, batch, execution_id, _calls = pause_after_first(db, factory, monkeypatch)
    else:
        plan, batch, execution_id = admit(db)
    value = request(db, execution_id, "stop", int(started))
    assert value["queue_state"] == "stopped" and value["command"]["status"] == "applied"
    assert value["scope"] == "local_step_boundary" and value["external_stop"] == "unsupported"
    assert queue.process_one(factory, worker_id="never-dispatch-stopped") is None
    with factory() as fresh:
        saved = fresh.get(DBCapabilityPlan, plan["plan_id"])
        assert saved.outcome is None and saved.status == ("running" if started else "validated")
        assert queue.read_batch(fresh, batch["batch_id"], actor=ADMIN, schema_version=2)["status"] == "stopped"
        with pytest.raises(control.ControlError, match="not_controllable"):
            request(fresh, execution_id, "resume", int(started) + 1)


def test_control_cas_and_idempotency_are_real_concurrent_transactions(db, factory):
    _plan, _batch, execution_id = admit(db)
    barrier = Barrier(2)

    def invoke(key):
        barrier.wait(timeout=10)
        with factory() as fresh:
            try:
                return request(fresh, execution_id, key=key)["command"]["command_id"]
            except control.ControlError as exc:
                return exc.code

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(invoke, ["same", "same"])
    assert first == second
    replay = request(db, execution_id, key="same")
    assert replay["reused"]
    with pytest.raises(control.ControlError, match="idempotency_conflict"):
        request(db, execution_id, "stop", 0, key="same")
    with pytest.raises(control.ControlError, match="version_conflict"):
        request(db, execution_id, "stop", 0, key="different")
    assert db.query(Command).count() == 1


def test_owner_permissions_and_current_user_are_not_body_hints(db, factory):
    _plan, _batch, execution_id = admit(db)
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        request(db, execution_id, actor=READER)
    reader_plan = create(db, actor=READER, instance_id=20)
    reader_batch = queue.enqueue_batch(db, [reader_plan["plan_id"]], actor=READER, idempotency_key="reader", schema_version=2)
    reader_execution = reader_batch["items"][0]["execution_id"]
    with pytest.raises(control.ControlError, match="permission_denied"):
        request(db, reader_execution, actor=READER)
    db.get(User, 1).status = "disabled"
    db.commit()
    with pytest.raises(Exception, match="actor_inactive"):
        request(db, execution_id, actor={"user_id": 1, "role": "admin"})
    assert db.query(Command).count() == 0


@pytest.mark.parametrize("corruption", ["checkpoint", "step", "ttl"])
def test_resume_refuses_changed_checkpoint_evidence_or_expired_ttl(db, factory, monkeypatch, corruption):
    plan, batch, execution_id, calls = pause_after_first(db, factory, monkeypatch)
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        if corruption == "checkpoint":
            value = deepcopy(member.checkpoint_json)
            value["outputs"] = {}
            member.checkpoint_json = value
        elif corruption == "step":
            plans._steps(fresh, plan["plan_id"])[0].result_ref_json = None
        else:
            fresh.get(DBCapabilityBatch, batch["batch_id"]).deadline_at = control._now() - timedelta(seconds=1)
        fresh.commit()
        with pytest.raises(control.ControlError):
            request(fresh, execution_id, "resume", 1)
    assert len(calls) == 1
    with factory() as fresh:
        assert fresh.get(DBCapabilityExecution, execution_id).state == "paused"


def test_v1_unchanged_and_controlled_batches_require_v2_before_writes(db, factory):
    _plan, batch, execution_id = admit(db)
    legacy = queue.read_batch(db, batch["batch_id"], actor=ADMIN)
    assert legacy["schema"] == "capability.batch_read/v1"
    assert "execution_id" not in legacy["items"][0] and "control_version" not in legacy["items"][0]
    request(db, execution_id)
    for function in (lambda: queue.read_batch(db, batch["batch_id"], actor=ADMIN),
                     lambda: queue.list_batches(db, actor=ADMIN, page=999),
                     lambda: queue.cancel_batch(db, batch["batch_id"], actor=ADMIN),
                     lambda: queue.enqueue_batch(db, [batch["items"][0]["plan_id"]], actor=ADMIN,
                                                  idempotency_key="controlled")):
        with pytest.raises(queue.QueueError, match="batch_control_requires_v2"):
            function()
        db.rollback()
    assert db.get(DBCapabilityBatch, batch["batch_id"]).cancel_requested_at is None
    assert queue.list_batches(db, actor=ADMIN, schema_version=2)["schema"] == "capability.batch_list/v2"


def test_fresh_process_resumes_only_pending_steps_from_checkpoint(db, factory, monkeypatch):
    plan, batch, execution_id, _calls = pause_after_first(db, factory, monkeypatch)
    request(db, execution_id, "resume", 1)
    url = str(db.get_bind().url)
    backend = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(backend), DATABASE_URL="sqlite://", DB_TYPE="sqlite",
               APP_MODE="quality_test", DEBUG="false", PYTHONDONTWRITEBYTECODE="1")
    code = """
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.services import plan_queue, plan_service
engine = create_engine(sys.argv[1])
factory = sessionmaker(bind=engine, autoflush=False)
original = plan_service.registry.execute_read
def no_replay(*args, **kwargs):
    assert args[1] != 'inspection.template.read', 'completed step replayed'
    return original(*args, **kwargs)
plan_service.registry.execute_read = no_replay
result = plan_queue.process_one(factory, worker_id='fresh-process')
assert result['outcome'] == 'succeeded', result
engine.dispose()
"""
    completed = subprocess.run([sys.executable, "-c", code, url], capture_output=True, text=True,
                               env=env, timeout=30, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    with factory() as fresh:
        assert fresh.get(DBCapabilityPlan, plan["plan_id"]).outcome == "succeeded"
        assert queue.read_batch(fresh, batch["batch_id"], actor=ADMIN, schema_version=2)["outcome"] == "succeeded"


@pytest.mark.parametrize("mode", ["none", "exception"])
def test_audit_failure_rolls_back_command_cas_checkpoint_and_target(db, factory, monkeypatch, mode):
    _plan, _batch, execution_id = admit(db)

    def fail(*args, **kwargs):
        if mode == "exception":
            raise RuntimeError("Synthetic audit store failure")
        return None

    monkeypatch.setattr(control.access_control, "append_security_audit", fail)
    with pytest.raises((control.ControlError, RuntimeError)):
        request(db, execution_id)
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        assert member.control_version == 0 and not member.control_enabled and member.checkpoint_json is None
        assert member.state == "queued" and fresh.query(Command).count() == fresh.query(Fence).count() == 0


def test_request_audit_contains_frozen_identity_and_idempotent_replay_adds_no_duplicate(db):
    plan, _batch, execution_id = admit(db)
    value = request(db, execution_id)
    request(db, execution_id)
    audit = db.query(SecurityAuditEvent).filter_by(category="capability_control").one()
    assert audit.actor_user_id == ADMIN["user_id"] and audit.target_id == str(execution_id)
    assert audit.details["plan_id"] == plan["plan_id"] and audit.details["revision"] == 1
    assert audit.details["command_id"] == value["command"]["command_id"]
    assert audit.details["binding_hash"] == db.get(DBCapabilityExecution, execution_id).binding_hash


def test_probe_does_not_replace_pending_pause_or_reenable_stop(db, factory, monkeypatch):
    _plan, _batch, execution_id = admit(db)
    original = plans.registry.execute_read

    def requests(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "inspection.template.read":
            with factory() as fresh:
                pause = request(fresh, execution_id)
                observed = request(fresh, execution_id, "probe", 1)
                assert observed["command"]["ack_stage"] == "observed"
                assert fresh.get(Command, pause["command"]["command_id"]).status == "accepted"
        return result

    monkeypatch.setattr(plans.registry, "execute_read", requests)
    assert queue.process_one(factory, worker_id="probe-during-pause")["queue_state"] == "paused"
    with factory() as fresh:
        assert fresh.query(Command).filter_by(action="pause").one().status == "applied"
        assert fresh.get(DBCapabilityExecution, execution_id).control_version == 2


def test_paused_target_stays_occupied_until_local_stop_and_unknown_cannot_resume(db, factory, monkeypatch):
    plan, _batch, execution_id, _calls = pause_after_first(db, factory, monkeypatch)
    another = create(db, actor=ADMIN, instance_id=10)
    queue.enqueue_batch(db, [another["plan_id"]], actor=ADMIN, idempotency_key="same-target")
    assert queue.process_one(factory, worker_id="blocked-by-paused") is None
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        member.state, member.observed_state = "reconciliation_required", "unknown"
        steps = plans._steps(fresh, plan["plan_id"])
        steps[1].status, steps[1].finished_at = "unknown", control._now()
        fresh.commit()
        for action in ("resume", "stop", "pause"):
            with pytest.raises(control.ControlError, match="requires_reconciliation"):
                request(fresh, execution_id, action, 1)
    assert queue.process_one(factory, worker_id="blocked-by-unknown") is None


def test_actual_worker_process_death_after_intent_never_replays_or_claims_stopped(db, factory):
    _plan, _batch, execution_id = admit(db)
    request(db, execution_id)
    request(db, execution_id, "resume", 1)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), DATABASE_URL="sqlite://",
               DB_TYPE="sqlite", APP_MODE="quality_test", DEBUG="false", PYTHONDONTWRITEBYTECODE="1")
    code = """
import os,sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.services import plan_queue,plan_service
engine=create_engine(sys.argv[1])
def die(*args, **kwargs):
    os._exit(23)
plan_service.registry.execute_read=die
plan_queue.process_one(sessionmaker(bind=engine,autoflush=False),worker_id='killed-after-intent')
"""
    child = subprocess.run([sys.executable, "-c", code, str(db.get_bind().url)], env=env,
                           capture_output=True, text=True, timeout=30, check=False)
    assert child.returncode == 23, child.stderr
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        assert member.state == "running"
        member.lease_expires_at = control._now() - timedelta(seconds=1)
        fresh.commit()
    assert queue.process_one(factory, worker_id="restarted-no-replay") is None
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        assert member.state == "reconciliation_required" and member.observed_state == "unknown"
        assert fresh.get(Fence, member.target_key).hold_state == "reconciliation_required"
        with pytest.raises(control.ControlError, match="requires_reconciliation"):
            request(fresh, execution_id, "resume", 2)


def test_lost_response_is_recovered_by_exact_owner_key_without_another_command(db, factory, monkeypatch):
    _plan, _batch, execution_id = admit(db)
    original = db.commit

    def lost_response():
        original()
        raise RuntimeError("Response lost after durable commit")

    monkeypatch.setattr(db, "commit", lost_response)
    with pytest.raises(RuntimeError, match="Response lost"):
        request(db, execution_id, key="lost-response")
    with factory() as fresh:
        before = fresh.connection().connection.driver_connection.total_changes
        recovered = control.read_control(fresh, execution_id, actor=ADMIN, idempotency_key="lost-response")
        assert recovered["command"]["action"] == "pause" and recovered["command"]["status"] == "applied"
        assert recovered["queue_state"] == "paused" and fresh.query(Command).count() == 1
        assert not fresh.new and not fresh.dirty and not fresh.deleted
        assert fresh.connection().connection.driver_connection.total_changes == before


def test_exact_command_query_keeps_original_superseded_command_and_current_projection(db):
    _plan, _batch, execution_id = admit(db)
    request(db, execution_id)
    resumed = request(db, execution_id, "resume", 1, key="resume-exact")
    request(db, execution_id, "stop", 2)
    value = control.read_control(db, execution_id, actor=ADMIN, idempotency_key="resume-exact")
    assert value["queue_state"] == "stopped" and value["control_version"] == 3
    assert value["command"]["command_id"] == resumed["command"]["command_id"]
    assert value["command"]["action"] == "resume" and value["command"]["status"] == "superseded"


def test_exact_query_never_matches_another_execution_or_actor_or_unknown_key(db, factory):
    _plan, _batch, execution_id = admit(db)
    request(db, execution_id, key="first-only")
    _other, _other_batch, other_id = admit(db, key="another")
    for identity, key in ((other_id, "first-only"), (execution_id, "not-present")):
        with pytest.raises(control.ControlError, match="control_command_not_found"):
            control.read_control(db, identity, actor=ADMIN, idempotency_key=key)
    with pytest.raises(plans.PlanError, match="plan_not_found"):
        control.read_control(db, execution_id, actor=READER, idempotency_key="first-only")
    db.get(User, 1).status = "disabled"
    db.commit()
    with factory() as fresh, pytest.raises(Exception, match="actor_inactive"):
        control.read_control(fresh, execution_id, actor=ADMIN, idempotency_key="first-only")


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
def test_real_inspection_v2_pauses_at_completed_job_and_resumes_without_duplicate_job(inspection_env, monkeypatch, engine):
    env = inspection_env
    actor, plan, batch = admit_steps(env, monkeypatch, engine, 3)
    role_id = env.db.query(UserAccessRole).filter_by(user_id=actor["user_id"]).one().role_id
    env.db.add_all([RoleActionPermission(role_id=role_id, action_key="control." + action)
                    for action in ("pause", "resume", "stop", "probe")])
    env.db.commit()
    execution_id = env.db.query(DBCapabilityExecution).filter_by(plan_id=plan["plan_id"]).one().id

    def pause():
        if len(env.collections) == 1:
            with env.factory() as controller:
                value = request(controller, execution_id, actor=actor)
                assert value["command"]["status"] == "accepted" and value["external_stop"] == "unsupported"

    env.action = pause
    result = queue.process_one(env.factory, worker_id="inspection-before-pause")
    assert result["queue_state"] == "paused", result
    with env.factory() as fresh:
        first = plans._steps(fresh, plan["plan_id"])[0].result_ref_json
        assert first["kind"] == "inspection_job" and first["inline"]["outcome"] == "success"
        assert fresh.query(DBInspectionJob).count() == 1
        request(fresh, execution_id, "resume", 1, actor=actor)
    env.action = None
    result = queue.process_one(env.factory, worker_id="inspection-after-resume")
    assert result["outcome"] == "succeeded" and result["queue_state"] == "archived", result
    with env.factory() as fresh:
        assert fresh.query(DBInspectionJob).count() == 3
        assert plans._steps(fresh, plan["plan_id"])[0].result_ref_json == first
        assert queue.read_batch(fresh, batch["batch_id"], actor=actor, schema_version=2)["outcome"] == "succeeded"


def test_asset_read_scope_does_not_grant_control_operate_even_with_control_actions(db):
    saved = create(db, actor=READER, instance_id=10)
    batch = queue.enqueue_batch(db, [saved["plan_id"]], actor=READER, idempotency_key="operate-scope", schema_version=2)
    role_id = db.query(UserAccessRole).filter_by(user_id=READER["user_id"]).one().role_id
    db.add_all([RoleActionPermission(role_id=role_id, action_key="control." + action) for action in ("pause", "probe")])
    db.add(UserAssetScope(user_id=READER["user_id"], scope_type="all", access_level="read"))
    db.commit()
    identity = batch["items"][0]["execution_id"]
    with pytest.raises(control.ControlError, match="scope_denied"):
        request(db, identity, actor=READER)
    with pytest.raises(control.ControlError, match="scope_denied"):
        control.read_control(db, identity, actor=READER)


def test_ci_lookup_cannot_replay_or_read_a_different_original_key(db):
    plan, batch, execution_id = admit(db, key="Mixed-Batch")
    request(db, execution_id, key="Mixed-Control")

    def simulate_ci_predicate(conn, cursor, statement, parameters, context, executemany):
        return statement.replace(".idempotency_key = ?", ".idempotency_key = ? COLLATE NOCASE"), parameters

    event.listen(db.get_bind(), "before_cursor_execute", simulate_ci_predicate, retval=True)
    try:
        with pytest.raises(control.ControlError, match="idempotency_conflict"):
            request(db, execution_id, key="mixed-control")
        with pytest.raises(control.ControlError, match="control_command_not_found"):
            control.read_control(db, execution_id, actor=ADMIN, idempotency_key="mixed-control")
        with pytest.raises(queue.QueueError, match="idempotency_conflict"):
            queue.enqueue_batch(db, [plan["plan_id"]], actor=ADMIN, idempotency_key="mixed-batch", schema_version=2)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", simulate_ci_predicate)
    assert db.query(Command).count() == db.query(DBCapabilityBatch).count() == 1


def test_pause_publication_supports_autoflush_sessions_without_erasing_staged_state(db, monkeypatch):
    factory = sessionmaker(bind=db.get_bind(), autoflush=True)
    _plan, _batch, execution_id, _calls = pause_after_first(db, factory, monkeypatch)
    with factory() as fresh:
        assert fresh.get(DBCapabilityExecution, execution_id).state == "paused"
        request(fresh, execution_id, "resume", 1)
    assert queue.process_one(factory, worker_id="autoflush-resume")["outcome"] == "succeeded"


def test_different_control_requests_same_version_have_one_cas_winner(db, factory):
    _plan, _batch, execution_id = admit(db)
    barrier = Barrier(2)

    def compete(action):
        with factory() as fresh:
            barrier.wait(timeout=10)
            try:
                return request(fresh, execution_id, action, key="race-" + action)["command"]["status"]
            except control.ControlError as exc:
                return exc.code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(compete, ["pause", "stop"])) == ["applied", "control_version_conflict"]
    assert db.query(Command).count() == 1


@pytest.mark.parametrize("failure", [False, True])
def test_pause_request_does_not_let_lost_owner_publish_success_or_failure(db, factory, monkeypatch, failure):
    plan, _batch, execution_id = admit(db)
    original = plans.registry.execute_read

    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        with factory() as fresh:
            request(fresh, execution_id)
            fresh.get(DBCapabilityExecution, execution_id).lease_expires_at = control._now() - timedelta(seconds=1)
            fresh.commit()
        if failure:
            raise RuntimeError("Synthetic failure after losing ownership")
        return result

    monkeypatch.setattr(plans.registry, "execute_read", lost)
    result = queue.process_one(factory, worker_id="lost-owner-with-control")
    assert result["queue_state"] == "reconciliation_required" and result["outcome"] is None
    with factory() as fresh:
        assert plans._steps(fresh, plan["plan_id"])[0].status == "running"
        assert fresh.get(Fence, "instance:10").hold_state == "reconciliation_required"
        assert fresh.query(Command).one().status == "accepted"


def test_completion_wins_over_pause_requested_during_final_handler(db, factory, monkeypatch):
    _plan, _batch, execution_id = admit(db, count=1)
    original = plans.registry.execute_read

    def pause_on_last(*args, **kwargs):
        with factory() as fresh:
            request(fresh, execution_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(plans.registry, "execute_read", pause_on_last)
    result = queue.process_one(factory, worker_id="completion-wins")
    assert result["queue_state"] == "archived" and result["outcome"] == "succeeded"
    with factory() as fresh:
        value = control.read_control(fresh, execution_id, actor=ADMIN, idempotency_key="pause-0")
        assert value["observed_state"] == "completed" and value["command"]["status"] == "superseded"
        assert fresh.get(Fence, "instance:10").hold_state == "released"


def test_controlled_claim_death_before_plan_intent_is_safely_recovered(db, factory):
    plan, _batch, execution_id = admit(db)
    request(db, execution_id)
    request(db, execution_id, "resume", 1)
    claim = queue._claim(factory, "never-started-owner")
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        member.lease_expires_at = control._now() - timedelta(seconds=1)
        assert fresh.get(DBCapabilityPlan, plan["plan_id"]).started_at is None
        fresh.commit()
    result = queue.process_one(factory, worker_id="safe-recovered-owner")
    assert result["outcome"] == "succeeded" and result["queue_state"] == "archived", result
    with factory() as fresh:
        member = fresh.get(DBCapabilityExecution, execution_id)
        assert member.attempt == 2 and member.owner_token is None
        assert member.fencing_epoch > 2
    assert claim["id"] == execution_id


def test_cancel_recovered_never_started_control_releases_only_local_reservation(db, factory):
    from app.core.transaction_lock import lock_transaction

    plan, batch, execution_id = admit(db)
    request(db, execution_id)
    request(db, execution_id, "resume", 1)
    claim = queue._claim(factory, "cancelled-stale-owner")
    with factory() as fresh:
        fresh.get(DBCapabilityExecution, execution_id).lease_expires_at = control._now() - timedelta(seconds=1)
        fresh.commit()
        lock_transaction(fresh, queue.LOCK_KEY)
        queue._recover(fresh, queue._now())
        fresh.commit()
    value = queue.cancel_batch(db, batch["batch_id"], actor=ADMIN, schema_version=2)
    assert value["items"][0]["queue_state"] == "cancelled"
    with factory() as stale:
        guard = queue.ExecutionGuard(claim, stale)
        try:
            saved = stale.get(DBCapabilityPlan, plan["plan_id"])
            saved.status, saved.started_at = "running", control._now()
            with pytest.raises(plans.PlanOwnershipLost):
                stale.commit()
            stale.rollback()
        finally:
            guard.close()
    with factory() as fresh:
        assert fresh.get(Fence, "instance:10").hold_state == "released"
        assert fresh.get(DBCapabilityPlan, plan["plan_id"]).started_at is None


def test_local_target_generation_change_fences_actual_controlled_publication(db, factory, monkeypatch):
    plan, _batch, execution_id = admit(db)
    request(db, execution_id)
    request(db, execution_id, "resume", 1)
    original = plans.registry.execute_read

    def lose_epoch(*args, **kwargs):
        result = original(*args, **kwargs)
        with factory() as fresh:
            fresh.get(Fence, "instance:10").epoch += 1
            fresh.commit()
        return result

    monkeypatch.setattr(plans.registry, "execute_read", lose_epoch)
    result = queue.process_one(factory, worker_id="lost-local-epoch")
    assert result["queue_state"] == "reconciliation_required" and result["outcome"] is None
    with factory() as fresh:
        assert plans._steps(fresh, plan["plan_id"])[0].result_ref_json is None
        assert fresh.get(Fence, "instance:10").hold_state == "reconciliation_required"


@pytest.mark.parametrize("change", ["batch_owner", "revision", "target", "hash"])
def test_control_rechecks_frozen_execution_membership_and_batch_owner(db, change):
    _plan, batch, execution_id = admit(db)
    member = db.get(DBCapabilityExecution, execution_id)
    if change == "batch_owner":
        db.get(DBCapabilityBatch, batch["batch_id"]).requested_by = READER["user_id"]
    elif change == "revision":
        member.plan_revision += 1
    elif change == "target":
        member.target_key = "instance:20"
    else:
        member.binding_hash = "0" * 64
    db.commit()
    with pytest.raises(control.ControlError, match="execution_binding_changed"):
        request(db, execution_id)
    with pytest.raises(control.ControlError, match="execution_binding_changed"):
        control.read_control(db, execution_id, actor=ADMIN)
    assert db.query(Command).count() == 0
