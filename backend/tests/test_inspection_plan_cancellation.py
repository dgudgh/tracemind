"""Confirmed domain cancellation through real plan/job queues and result readers."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanStep
from app.models.check_result import DBCheckResult
from app.models.inspection_job import DBInspectionJob
from app.models.slow_sql import DBSlowSQL
from app.models.user import User
from app.services import capability_registry as registry, plan_governance, plan_queue, plan_service as plans
from app.services.inspection import jobs, run_readback
from test_inspection_plan_adapter import env as adapter_env, KEYS  # noqa: F401
from test_inspection_plan_publication import accepted
from test_inspection_staged_publication import env  # noqa: F401


def request_domain_cancel(env, actor, *, now=None):
    with env.factory() as control:
        row = control.query(DBInspectionJob).filter_by(status="running").one()
        jobs.cancel(control, row, requested_by=actor["user_id"], now=now)


def cancel_before_claim(env, monkeypatch, actor):
    claim = jobs.claim

    def cancel_queued(db, job_id, token, **kwargs):
        with env.factory() as control:
            row = control.get(DBInspectionJob, job_id)
            assert row.status == "queued" and row.started_at is None
            jobs.cancel(control, row, requested_by=actor["user_id"])
        return claim(db, job_id, token, **kwargs)

    monkeypatch.setattr(jobs, "claim", cancel_queued)


def enqueue_same_target_read(env, actor):
    saved = plans.create_plan(env.db, actor=actor, capability_id="inspection.run.query", definition_version=1,
                              instance_id=env.instance.id, params={"page": 2})
    return plan_queue.enqueue_batch(env.db, [saved["plan_id"]], actor=actor,
                                    idempotency_key="read-after-domain-cancel")


@pytest.mark.parametrize("when", ["before_run", "during_run"])
def test_confirmed_domain_cancel_settles_and_archives_with_real_empty_readback(env, monkeypatch, when):
    actor, plan, receipt = accepted(env, monkeypatch)
    if when == "before_run":
        cancel_before_claim(env, monkeypatch, actor)
    else:
        env.action = lambda: request_domain_cancel(env, actor)
    finished = plan_queue.process_one(env.factory, worker_id="confirmed-cancel")
    assert finished["outcome"] == "cancelled" and finished["queue_state"] == "archived", finished
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        batch = plan_queue.read_batch(verify, receipt["batch_id"], actor=actor)
        step = saved["steps"][0]
        ref = step["result_ref"]["inline"]
        row = verify.get(DBInspectionJob, ref["job_id"])
        assert saved["outcome"] == step["status"] == ref["outcome"] == batch["outcome"] == "cancelled"
        assert row.cancel_requested == 1 and row.cancel_requested_at is not None
        assert row.owner_token is None and row.lease_until is None and row.finished_at is not None
        assert jobs.failure_code(row) == f"cancelled_{when}"
        assert step["outcome_kind"] == ref["error_code"] == f"cancelled_{when}"
        read = run_readback.build_run_read(verify, row.id, instance_id=env.instance.id)
        assert read["run"]["status"] == "cancelled" and read["results"]["total_count"] == 0
        assert read["report"] is None
        assert verify.query(DBSlowSQL).count() == verify.query(DBCheckResult).count() == 0
        assert plan_queue._evidence(verify, verify.get(DBCapabilityPlan, plan["plan_id"]))
    assert env.source_calls == ([] if when == "before_run" else [env.kind])
    assert plan_queue.process_one(env.factory, worker_id="never-replay-cancel") is None
    enqueue_same_target_read(env, actor)
    assert plan_queue.process_one(env.factory, worker_id="confirmed-target-released")["outcome"] == "succeeded"


def admit_steps(env, monkeypatch, engine, count, *, preserve_policy=False):
    env.instance.db_type = engine
    env.policy.db_types_json = [engine]
    if not preserve_policy:
        env.policy.item_config_json = [{"key": KEYS[engine], "enabled": True}]
    monkeypatch.setattr(registry.settings, "PLAN_INSPECTION_EXECUTION_ENABLED", True)
    approver = User(username="cancel-approver", password_hash="unused", role="admin", status="active")
    env.db.add(approver)
    env.db.commit()
    actor, reviewer = {"user_id": env.actor.id}, {"user_id": approver.id}
    registry.sync_definitions(env.db, actor=reviewer)
    plan = plans.create_plan(
        env.db, actor=actor, capability_id="inspection.run.execute", definition_version=2,
        instance_id=env.instance.id,
        steps=[{"seq": seq, "title": f"Inspection {seq}", "capability_id": "inspection.run.execute",
                "definition_version": 2, "params": {"policy_id": env.policy.id},
                "depends_on": [seq - 1] if seq > 1 else []} for seq in range(1, count + 1)],
    )
    plan_governance.request_approval(env.db, plan["plan_id"], actor=actor, expected_revision=1)
    plan_governance.approve_plan(env.db, plan["plan_id"], actor=reviewer, expected_revision=1,
                                 expected_binding_hash=plan["binding_hash"])
    receipt = plan_queue.enqueue_batch(env.db, [plan["plan_id"]], actor=actor, idempotency_key="cancel-steps")
    return actor, plan, receipt


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
@pytest.mark.parametrize("cancel_step", [1, 2])
def test_cancel_skips_remaining_steps_and_preserves_previously_published_business(adapter_env, monkeypatch,
                                                                              engine, cancel_step):
    env = adapter_env
    actor, plan, receipt = admit_steps(env, monkeypatch, engine, 3)
    env.action = lambda: request_domain_cancel(env, actor) if len(env.collections) == cancel_step else None
    finished = plan_queue.process_one(env.factory, worker_id="partial-then-cancel")
    expected = "partial" if cancel_step == 2 else "cancelled"
    assert finished["outcome"] == expected and finished["queue_state"] == "archived", finished
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert saved["outcome"] == plan_queue.read_batch(verify, receipt["batch_id"], actor=actor)["outcome"] == expected
        assert [step["status"] for step in saved["steps"]] == (
            ["succeeded", "cancelled", "skipped"] if cancel_step == 2 else ["cancelled", "skipped", "skipped"])
        cancel = saved["steps"][cancel_step - 1]
        cancelled_id = cancel["result_ref"]["inline"]["job_id"]
        cancelled = run_readback.build_run_read(verify, cancelled_id)
        assert cancelled["run"]["status"] == "cancelled" and cancelled["results"]["total_count"] == 0
        assert cancelled["report"] is None
        assert verify.query(DBInspectionJob).count() == cancel_step
        assert all(step["result_ref"] is None and step["started_at"] is None
                   for step in saved["steps"][cancel_step:])
        if cancel_step == 2:
            first_id = saved["steps"][0]["result_ref"]["inline"]["job_id"]
            first = run_readback.build_run_read(verify, first_id)
            assert first["run"]["status"] == "success" and first["results"]["total_count"] > 0
            assert first["report"] is not None and first["report"]["task"]["task_status"] == "success"
            assert verify.query(DBCheckResult).count() == first["results"]["total_count"]
            assert saved["result_ref"]["steps"] == [1, 2]
        else:
            assert verify.query(DBCheckResult).count() == 0
        assert plan_queue._evidence(verify, verify.get(DBCapabilityPlan, plan["plan_id"]))
    assert env.collections == [engine] * cancel_step


def test_successful_native_slow_sql_ids_and_report_survive_later_confirmed_cancel(env, monkeypatch):
    actor, plan, _receipt = admit_steps(env, monkeypatch, env.kind, 3, preserve_policy=True)
    claim, claims = jobs.claim, []

    def cancel_second_job(db, job_id, token, **kwargs):
        claims.append(job_id)
        if len(claims) == 2:
            with env.factory() as control:
                jobs.cancel(control, control.get(DBInspectionJob, job_id), requested_by=actor["user_id"])
        return claim(db, job_id, token, **kwargs)

    monkeypatch.setattr(jobs, "claim", cancel_second_job)
    finished = plan_queue.process_one(env.factory, worker_id="preserve-native-business")
    assert finished["outcome"] == "partial" and finished["queue_state"] == "archived", finished
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert [step["status"] for step in saved["steps"]] == ["succeeded", "cancelled", "skipped"]
        first = run_readback.build_run_read(verify, claims[0])
        persistence = next(item["raw_data"] for item in first["results"]["items"]
                           if item["metric_name"].endswith(".slow_sql.persistence"))
        actual = verify.query(DBSlowSQL).filter_by(instance_id=env.instance.id).all()
        assert persistence["count"] == len(actual) == 2
        assert set(persistence["record_ids"]) == {row.id for row in actual}
        assert first["report"]["task"]["task_status"] == first["run"]["status"] == "success"
        assert verify.query(DBCheckResult).count() == first["results"]["total_count"]
        second = run_readback.build_run_read(verify, claims[1])
        assert second["run"]["status"] == "cancelled" and second["results"]["total_count"] == 0
        assert second["report"] is None and verify.query(DBInspectionJob).count() == 2
        assert saved["result_ref"]["steps"] == [1, 2]
    assert env.source_calls == [env.kind]


@pytest.mark.parametrize("cancel_step", [1, 2])
@pytest.mark.parametrize("missing", ["reference", "reference_kind", "unfinished_remainder", "plan_reference"])
def test_settle_rechecks_confirmed_cancel_evidence_even_after_prior_success(adapter_env, monkeypatch,
                                                                          cancel_step, missing):
    env = adapter_env
    actor, plan, _receipt = admit_steps(env, monkeypatch, "mysql", 3)
    env.action = lambda: request_domain_cancel(env, actor) if len(env.collections) == cancel_step else None
    expected = "partial" if cancel_step == 2 else "cancelled"
    assert plan_queue.process_one(env.factory, worker_id="settle-proof")["outcome"] == expected
    with env.factory() as verify:
        saved = verify.get(DBCapabilityPlan, plan["plan_id"])
        steps = plans._steps(verify, saved.plan_id)
        if missing == "reference":
            steps[cancel_step - 1].result_ref_json = None
        elif missing == "reference_kind":
            reference = deepcopy(steps[cancel_step - 1].result_ref_json)
            reference["kind"] = "capability_read"
            steps[cancel_step - 1].result_ref_json = reference
        elif missing == "unfinished_remainder":
            steps[-1].status = "pending"
        else:
            saved.result_ref_json = None
        verify.flush()
        assert not plan_queue._evidence(verify, saved)
        if missing == "plan_reference":
            verify.rollback()
            return
        saved.status, saved.outcome, saved.finished_at, saved.result_ref_json = "running", None, None, None
        with pytest.raises(plans.PlanError, match="invalid_evidence_outcome"):
            plans.settle(verify, saved, expected)
        verify.rollback()
        assert verify.get(DBCapabilityPlan, plan["plan_id"]).outcome == expected
        if cancel_step == 2:
            assert verify.query(DBCheckResult).count() > 0


def test_batch_cancel_request_does_not_cancel_domain_or_erase_real_slow_sql(env, monkeypatch):
    actor, plan, receipt = accepted(env, monkeypatch)

    def cancel_delivery():
        with env.factory() as control:
            value = plan_queue.cancel_batch(control, receipt["batch_id"], actor=actor)
            assert value["cancel_requested"] and value["outcome"] is None

    env.action = cancel_delivery
    finished = plan_queue.process_one(env.factory, worker_id="delivery-request-is-not-domain-cancel")
    assert finished["outcome"] == "succeeded" and finished["queue_state"] == "archived"
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        row = verify.query(DBInspectionJob).one()
        assert row.status == "success" and row.cancel_requested == 0
        assert saved["steps"][0]["status"] == "succeeded"
        assert plan_queue.read_batch(verify, receipt["batch_id"], actor=actor)["cancel_requested"]
        read = run_readback.build_run_read(verify, row.id)
        assert verify.query(DBSlowSQL).count() == 2 and read["results"]["total_count"] > 0
        assert read["report"]["task"]["task_status"] == "success"


@pytest.mark.parametrize("termination", ["force_closed", "interrupted", "unacknowledged"])
def test_force_close_and_lost_child_owner_remain_unknown_and_never_replay(env, monkeypatch, termination):
    actor, plan, _receipt = accepted(env, monkeypatch)

    def uncertain_cancel():
        with env.factory() as control:
            row = control.query(DBInspectionJob).filter_by(status="running").one()
            requested_at = jobs.utcnow() - timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1)
            jobs.cancel(control, row, requested_by=actor["user_id"], now=requested_at)
            if termination == "force_closed":
                assert jobs.reap_overdue(control) == 1
            elif termination == "interrupted":
                assert jobs.reap_interrupted(control, now=row.lease_until + timedelta(seconds=1)) == 1
            else:
                row.owner_token = "different-unconfirmed-owner"
                control.commit()

    env.action = uncertain_cancel
    finished = plan_queue.process_one(env.factory, worker_id="uncertain-cancel")
    assert finished["outcome"] == "unknown" and finished["queue_state"] == "reconciliation_required", finished
    with env.factory() as verify:
        row = verify.query(DBInspectionJob).one()
        assert row.status == {"force_closed": "cancelled", "interrupted": "interrupted",
                              "unacknowledged": "running"}[termination]
        if termination != "unacknowledged":
            assert jobs.failure_code(row) == "cancelled_force_closed"
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert saved["steps"][0]["status"] == "unknown"
        if termination != "unacknowledged":
            reference = saved["steps"][0]["result_ref"]
            assert reference is not None
            assert reference["kind"] == "inspection_job" and reference["inline"]["job_id"] == row.id
            assert reference["inline"]["outcome"] == jobs.outcome_block(row)["outcome"]
            assert reference["inline"]["error_code"] == "cancelled_force_closed"
            assert saved["steps"][0]["outcome_kind"] == "cancelled_force_closed"
            assert saved["result_ref"]["steps"] == [1]
            persisted_step = verify.query(DBCapabilityPlanStep).filter_by(plan_id=plan["plan_id"]).one()
            assert plans.validate_effect_reference(verify, verify.get(DBCapabilityPlan, plan["plan_id"]), persisted_step)
            invalid = deepcopy(persisted_step.result_ref_json)
            invalid["inline"]["error_code"] = "cancelled_during_run"
            persisted_step.result_ref_json = invalid
            with pytest.raises(plans.PlanError, match="result_unknown"):
                plans.validate_effect_reference(verify, verify.get(DBCapabilityPlan, plan["plan_id"]), persisted_step)
            verify.rollback()
        assert verify.query(DBCheckResult).count() == verify.query(DBSlowSQL).count() == 0
        read = run_readback.build_run_read(verify, row.id)
        assert read["results"]["total_count"] == 0 and read["report"] is None
        assert not plan_queue._evidence(verify, verify.get(DBCapabilityPlan, plan["plan_id"]))
    assert plan_queue.process_one(env.factory, worker_id="uncertain-never-replayed") is None
    enqueue_same_target_read(env, actor)
    assert plan_queue.process_one(env.factory, worker_id="uncertain-target-still-held") is None


@pytest.mark.parametrize("engine", ["oracle", "mysql", "postgresql", "sqlserver"])
def test_force_close_after_success_keeps_both_job_links_but_never_releases_target(adapter_env, monkeypatch, engine):
    env = adapter_env
    actor, plan, _receipt = admit_steps(env, monkeypatch, engine, 3)

    def force_close_second():
        if len(env.collections) == 2:
            request_domain_cancel(env, actor, now=jobs.utcnow() - timedelta(seconds=jobs.CANCEL_GRACE_SECONDS + 1))
            with env.factory() as control:
                assert jobs.reap_overdue(control) == 1

    env.action = force_close_second
    finished = plan_queue.process_one(env.factory, worker_id="partial-but-unresolved")
    assert finished["outcome"] == "partial" and finished["queue_state"] == "reconciliation_required"
    assert finished["requires_attention"]
    with env.factory() as verify:
        saved = plans.read_plan(verify, plan["plan_id"], actor=actor)
        assert [step["status"] for step in saved["steps"]] == ["succeeded", "unknown", "skipped"]
        assert saved["result_ref"]["steps"] == [1, 2]
        first_id, second_id = [step["result_ref"]["inline"]["job_id"] for step in saved["steps"][:2]]
        first = run_readback.build_run_read(verify, first_id)
        second = run_readback.build_run_read(verify, second_id)
        assert first["run"]["status"] == "success" and first["report"] is not None
        assert first["results"]["total_count"] == verify.query(DBCheckResult).count() > 0
        assert second["results"]["total_count"] == 0 and second["report"] is None
        assert saved["steps"][1]["result_ref"]["inline"]["outcome"] == second["run"]["outcome"]
        assert saved["steps"][1]["outcome_kind"] == "cancelled_force_closed"
        assert verify.query(DBInspectionJob).count() == 2
        assert not plan_queue._evidence(verify, verify.get(DBCapabilityPlan, plan["plan_id"]))
    assert env.collections == [engine, engine]
    assert plan_queue.process_one(env.factory, worker_id="partial-unknown-not-replayed") is None


@pytest.mark.parametrize("missing", [
    "request", "request_time", "owner", "lease", "finished", "stage", "event", "event_stage",
    "force_closed", "published_event", "published_result", "envelope_code",
])
def test_cancel_reference_requires_all_authoritative_acknowledgement_evidence(env, monkeypatch, missing):
    actor, plan, _receipt = accepted(env, monkeypatch)
    env.action = lambda: request_domain_cancel(env, actor)
    finished = plan_queue.process_one(env.factory, worker_id="proof-before-corruption")
    assert finished["outcome"] == "cancelled"
    with env.factory() as verify:
        saved = verify.get(DBCapabilityPlan, plan["plan_id"])
        step = verify.query(DBCapabilityPlanStep).filter_by(plan_id=saved.plan_id).one()
        row = verify.query(DBInspectionJob).one()
        assert plans.validate_effect_reference(verify, saved, step)
        events = deepcopy(row.progress_json)
        if missing == "request":
            row.cancel_requested = 0
        elif missing == "request_time":
            row.cancel_requested_at = None
        elif missing == "owner":
            row.owner_token = "still-owned"
        elif missing == "lease":
            row.lease_until = jobs.utcnow() + timedelta(seconds=60)
        elif missing == "finished":
            row.finished_at = None
        elif missing == "stage":
            row.stage = "completed"
        elif missing == "event":
            row.progress_json = []
        elif missing == "event_stage":
            events[-1]["stage"] = "completed"
            row.progress_json = events
        elif missing == "force_closed":
            events[-1]["failure_code"] = "cancelled_force_closed"
            row.progress_json = events
        elif missing == "published_event":
            row.progress_json = [{"stage": "completed", "time": events[-1]["time"]}, *events]
        elif missing == "published_result":
            verify.add(DBCheckResult(task_id=row.id, instance_id=row.instance_id, db_type=row.db_type,
                                     metric_name="fixture.unexpected_publication", check_status="normal"))
        elif missing == "envelope_code":
            ref = deepcopy(step.result_ref_json)
            ref["inline"]["error_code"] = "cancelled_before_run"
            step.result_ref_json = ref
        verify.commit()
        with pytest.raises(plans.PlanError, match="result_unknown"):
            plans.validate_effect_reference(verify, saved, step)
        assert not plan_queue._evidence(verify, saved)


def test_confirmed_cancellation_survives_mysql_datetime_second_precision(env, monkeypatch):
    actor, plan, _receipt = accepted(env, monkeypatch)
    env.action = lambda: request_domain_cancel(env, actor)
    assert plan_queue.process_one(env.factory, worker_id="precision-proof")["outcome"] == "cancelled"
    with env.factory() as verify:
        saved = verify.get(DBCapabilityPlan, plan["plan_id"])
        step = verify.query(DBCapabilityPlanStep).filter_by(plan_id=saved.plan_id).one()
        row = verify.query(DBInspectionJob).one()
        row.finished_at = row.finished_at.replace(microsecond=0)
        row.cancel_requested_at = row.cancel_requested_at.replace(microsecond=0)
        verify.commit()
        assert plans.validate_effect_reference(verify, saved, step)
        assert plan_queue._evidence(verify, saved)
