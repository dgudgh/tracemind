"""Isolated C07 schema checks, deliberately outside the application's model imports."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


CASES = [
    "registered_contract", "vocabulary", "unique_request", "unique_version", "version_constraints",
    "applied_pause", "applied_resume", "applied_stop", "applied_probe", "received_not_stopped",
    "missing_applied_evidence", "typed_references", "frozen_intent", "held_target",
    "target_release", "legacy_target", "reconciliation_append_only", "reconciliation_chain",
    "reconciliation_release", "unknown_keeps_target", "review_required", "persistence",
    "epoch_cas", "dialect_ddl",
]


@pytest.mark.parametrize("case", CASES)
def test_control_schema_in_isolated_process(tmp_path, case):
    backend = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(backend), DATABASE_URL="sqlite://", DB_TYPE="sqlite",
               APP_MODE="quality_test", DEBUG="false", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys; runpy.run_path(sys.argv[1])['_run_case'](sys.argv[2],sys.argv[3])",
         str(Path(__file__).resolve()), case, str(tmp_path / "control.db")],
        env=env, capture_output=True, text=True, timeout=45, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_case(case, db_path):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta
    from threading import Barrier
    import socket

    from sqlalchemy import create_engine, select, text, update
    from sqlalchemy.dialects import mysql, postgresql, sqlite
    from sqlalchemy.exc import IntegrityError, StatementError
    from sqlalchemy.orm import Session
    from sqlalchemy.schema import CreateTable

    def deny_network(*args, **kwargs):
        raise AssertionError("Schema tests must not contact a real service")

    socket.socket.connect = deny_network
    if case == "registered_contract":
        import app.models
        from app.core.database import Base
        from app.migrations.runner import MIGRATIONS
        from app.migrations.versions import v20261020_05_capability_control

        assert "app.models.capability_control" in sys.modules
        for name in ("DBCapabilityControlCommand", "DBCapabilityTargetFence", "DBCapabilityReconciliation"):
            model = getattr(app.models, name)
            assert model.__table__ is Base.metadata.tables[model.__tablename__]
        assert {"db_capability_control_command", "db_capability_target_fence", "db_capability_reconciliation"} <= set(
            Base.metadata.tables)
        assert sum(item is v20261020_05_capability_control for item in MIGRATIONS) == 1
        assert v20261020_05_capability_control.REVISION == "20261020_05_capability_control"
        return

    from app.models.capability_control import (
        AckStage, CommandStatus, ControlAction, DBCapabilityControlCommand as Command,
        DBCapabilityReconciliation as Decision, DBCapabilityTargetFence as Fence,
        EffectOutcome, HoldState, ObservedState, Resolution, RuntimeObservation,
    )

    engine = create_engine("sqlite:///" + db_path, connect_args={"timeout": 10})
    models = (Command, Fence, Decision)
    for model in models:
        model.__table__.create(engine)
    now = datetime(2026, 10, 20, 10)
    evidence = {"kind": "control_receipt", "id": "receipt-1", "schema": "control.receipt/v1"}

    def command(**changes):
        values = dict(command_id="cmd-1", execution_id=1, plan_id="plan-1", plan_revision=1,
                      binding_hash="a" * 64, target_key="instance:1", action=ControlAction.STOP,
                      requested_by=1, reason="Explicit user stop", created_at=now,
                      deadline_at=now + timedelta(seconds=60), expected_control_version=0,
                      control_version=1, fencing_epoch=1, idempotency_key="stop-1", input_hash="b" * 64)
        values.update(changes)
        return Command(**values)

    def fence(**changes):
        values = dict(target_key="instance:1", epoch=1, row_version=0, hold_state=HoldState.HELD,
                      holder_execution_id=1, holder_plan_id="plan-1", binding_hash="a" * 64,
                      acquired_at=now, updated_at=now, hold_reason="Active execution")
        values.update(changes)
        return Fence(**values)

    def decision(**changes):
        values = dict(decision_id="decision-1", execution_id=1, plan_id="plan-1", plan_revision=1,
                      binding_hash="a" * 64, target_key="instance:1", observed_epoch=1,
                      control_version=1, sequence=1, resolution=Resolution.UNRESOLVED,
                      runtime_observation=RuntimeObservation.UNKNOWN, effect_outcome=EffectOutcome.UNKNOWN,
                      recorded_by=1, reason="Await authoritative evidence", observed_at=now, recorded_at=now)
        values.update(changes)
        return Decision(**values)

    def save(row):
        with Session(engine) as db:
            db.add(row)
            db.commit()

    def rejects(row, expected=IntegrityError):
        with Session(engine) as db:
            db.add(row)
            with pytest.raises(expected):
                db.commit()
            db.rollback()

    def released(**changes):
        values = dict(resolution=Resolution.CONFIRMED_STOPPED,
                      runtime_observation=RuntimeObservation.TERMINATED,
                      effect_outcome=EffectOutcome.CANCELLED, release_target=True,
                      reviewed_by=2, reviewed_at=now, source_verified_at=now,
                      runtime_evidence_ref=evidence, effect_evidence_ref=evidence,
                      barrier_evidence_ref=evidence)
        values.update(changes)
        return decision(**values)

    if case == "vocabulary":
        for field, value in (("action", "handler"), ("status", "success"),
                             ("ack_stage", "stopped"), ("observed_state", "success")):
            rejects(command(**{field: value}), StatementError)
        rejects(decision(effect_outcome="success"), StatementError)
        save(command())
        with engine.begin() as db:
            with pytest.raises(IntegrityError):
                db.execute(text("UPDATE db_capability_control_command SET action = 'exec_sql'"))
        assert {value.value for value in ControlAction} == {"pause", "resume", "stop", "probe"}
    elif case == "unique_request":
        save(command())
        rejects(command(command_id="cmd-2", execution_id=2))
        save(command(command_id="cmd-3", execution_id=2, requested_by=2))
    elif case == "unique_version":
        save(command())
        rejects(command(command_id="cmd-2", idempotency_key="another"))
    elif case == "version_constraints":
        for changes in ({"fencing_epoch": 0}, {"control_version": 2}, {"expected_control_version": -1},
                        {"execution_id": 0}, {"plan_revision": 0}, {"requested_by": 0},
                        {"deadline_at": now}, {"delivery_attempts": -1}):
            rejects(command(**changes))
    elif case.startswith("applied_"):
        action = case.removeprefix("applied_")
        stage, state = {"pause": (AckStage.QUIESCED, ObservedState.PAUSED),
                        "resume": (AckStage.RESUMED, ObservedState.RUNNING),
                        "stop": (AckStage.TERMINATED, ObservedState.STOPPED),
                        "probe": (AckStage.OBSERVED, ObservedState.UNKNOWN)}[action]
        save(command(action=action, status=CommandStatus.APPLIED, ack_stage=stage,
                     observed_state=state, acknowledged_at=now, applied_at=now, ack_evidence_ref=evidence))
        if action == "stop":
            save(command(command_id="cmd-2", execution_id=2, idempotency_key="stop-2", action=action,
                         status=CommandStatus.APPLIED, ack_stage=AckStage.NOT_STARTED,
                         observed_state=state, acknowledged_at=now, applied_at=now, ack_evidence_ref=evidence))
    elif case == "received_not_stopped":
        rejects(command(status=CommandStatus.APPLIED, ack_stage=AckStage.RECEIVED,
                        observed_state=ObservedState.STOPPED, acknowledged_at=now,
                        applied_at=now, ack_evidence_ref=evidence))
        save(command(status=CommandStatus.ACKNOWLEDGED, ack_stage=AckStage.RECEIVED, acknowledged_at=now))
        with Session(engine) as db:
            assert db.get(Command, "cmd-1").observed_state is None
    elif case == "missing_applied_evidence":
        for changes in ({"ack_evidence_ref": None}, {"ack_stage": None}, {"observed_state": None},
                        {"acknowledged_at": None}, {"applied_at": None}, {"ack_stage": AckStage.COMPLETED}):
            values = dict(status=CommandStatus.APPLIED, ack_stage=AckStage.TERMINATED,
                          observed_state=ObservedState.STOPPED, acknowledged_at=now,
                          applied_at=now, ack_evidence_ref=evidence)
            values.update(changes)
            rejects(command(**values))
    elif case == "typed_references":
        for ref in ({"kind": "handler", "id": "x", "schema": "x/v1"},
                    dict(evidence, inline={"verified": True}), dict(evidence, sha256="BAD"),
                    dict(evidence, id=""), {"kind": [], "id": "x", "schema": "x"}):
            with pytest.raises(ValueError):
                command(ack_evidence_ref=ref)
        with pytest.raises(ValueError):
            command(binding_hash="A" * 64)
        save(command(ack_evidence_ref=dict(evidence, sha256="c" * 64)))
    elif case == "frozen_intent":
        save(command())
        with Session(engine) as db:
            row = db.get(Command, "cmd-1")
            row.reason = "Changed request"
            with pytest.raises(ValueError, match="immutable"):
                db.commit()
            db.rollback()
            row = db.get(Command, "cmd-1")
            row.status, row.delivery_attempts = CommandStatus.SENT, 1
            db.commit()
            db.delete(row)
            with pytest.raises(ValueError, match="retained"):
                db.commit()
    elif case == "held_target":
        for state in (HoldState.HELD, HoldState.PAUSED, HoldState.STOP_PENDING, HoldState.RECONCILIATION_REQUIRED):
            rejects(fence(hold_state=state, holder_execution_id=None))
            save(fence(target_key="instance:" + state.value, hold_state=state))
        rejects(fence(epoch=0))
        rejects(fence(epoch=None))
    elif case == "target_release":
        values = dict(hold_state=HoldState.RELEASED, holder_execution_id=None,
                      holder_plan_id=None, binding_hash=None, released_at=now)
        rejects(fence(**values))
        save(fence(**values, release_evidence_ref={"kind": "reconciliation", "id": "decision-1",
                                                  "schema": "control.decision/v1"}))
        with Session(engine) as db:
            db.delete(db.get(Fence, "instance:1"))
            with pytest.raises(ValueError, match="retained"):
                db.commit()
    elif case == "legacy_target":
        save(fence(hold_state=HoldState.LEGACY_UNVERIFIED, epoch=None,
                   holder_execution_id=None, holder_plan_id=None, binding_hash=None, acquired_at=None))
        rejects(fence(target_key="instance:2", epoch=None, hold_state=HoldState.UNCLAIMED,
                      holder_execution_id=None, holder_plan_id=None, binding_hash=None, acquired_at=None))
    elif case == "reconciliation_append_only":
        save(decision())
        with Session(engine) as db:
            row = db.get(Decision, "decision-1")
            row.reason = "Overwrite history"
            with pytest.raises(ValueError, match="append-only"):
                db.commit()
            db.rollback()
            db.delete(db.get(Decision, "decision-1"))
            with pytest.raises(ValueError, match="append-only"):
                db.commit()
    elif case == "reconciliation_chain":
        save(decision())
        rejects(decision(decision_id="decision-2"))
        rejects(decision(decision_id="decision-2", sequence=2))
        save(decision(decision_id="decision-2", sequence=2, previous_decision_id="decision-1"))
        with Session(engine) as db:
            assert len(db.scalars(select(Decision)).all()) == 2
    elif case == "reconciliation_release":
        save(released())
        save(released(decision_id="decision-2", execution_id=2,
                      resolution=Resolution.CONFIRMED_NOT_STARTED,
                      runtime_observation=RuntimeObservation.NOT_STARTED, effect_outcome=EffectOutcome.NO_EFFECT))
        save(released(decision_id="decision-3", execution_id=3,
                      resolution=Resolution.CONFIRMED_COMPLETED,
                      runtime_observation=RuntimeObservation.COMPLETED, effect_outcome=EffectOutcome.PARTIAL))
    elif case == "unknown_keeps_target":
        rejects(released(effect_outcome=EffectOutcome.UNKNOWN))
        rejects(released(resolution=Resolution.STILL_RUNNING, runtime_observation=RuntimeObservation.RUNNING))
        rejects(released(resolution=Resolution.UNRESOLVED))
        rejects(released(resolution=Resolution.DISPUTED))
        rejects(released(observed_epoch=None))
        save(decision(resolution=Resolution.CONFIRMED_STOPPED,
                      runtime_observation=RuntimeObservation.TERMINATED, effect_outcome=EffectOutcome.UNKNOWN))
        with Session(engine) as db:
            row = db.get(Decision, "decision-1")
            assert row.effect_outcome is EffectOutcome.UNKNOWN and not row.release_target
    elif case == "review_required":
        for changes in ({"reviewed_by": None}, {"reviewed_by": 1}, {"reviewed_at": None},
                        {"source_verified_at": None}, {"runtime_evidence_ref": None},
                        {"effect_evidence_ref": None}, {"barrier_evidence_ref": None}):
            rejects(released(**changes))
    elif case == "persistence":
        save(command())
        save(fence(hold_state=HoldState.RECONCILIATION_REQUIRED))
        save(decision())
        engine.dispose()
        with Session(create_engine("sqlite:///" + db_path)) as db:
            assert db.get(Command, "cmd-1").status is CommandStatus.ACCEPTED
            assert db.get(Fence, "instance:1").hold_state is HoldState.RECONCILIATION_REQUIRED
            assert db.get(Decision, "decision-1").effect_outcome is EffectOutcome.UNKNOWN
    elif case == "epoch_cas":
        save(fence())
        barrier = Barrier(2)

        def compete():
            with engine.begin() as db:
                barrier.wait(timeout=10)
                return db.execute(update(Fence).where(Fence.target_key == "instance:1", Fence.epoch == 1,
                                                       Fence.row_version == 0).values(epoch=2, row_version=1)).rowcount

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: compete(), range(2)))
        assert sorted(results) == [0, 1]
        with Session(engine) as db:
            assert (db.get(Fence, "instance:1").epoch, db.get(Fence, "instance:1").row_version) == (2, 1)
    elif case == "dialect_ddl":
        for dialect in (mysql.dialect(), postgresql.dialect(), sqlite.dialect()):
            for model in models:
                ddl = str(CreateTable(model.__table__).compile(dialect=dialect))
                assert "CREATE TABLE" in ddl and "CHECK" in ddl
    else:
        raise AssertionError(case)
    engine.dispose()
