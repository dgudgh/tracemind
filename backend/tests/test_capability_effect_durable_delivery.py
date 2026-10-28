"""Every advertised v3 effect uses the same strict HTTP -> queue -> worker transport."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.api.v1 import assistant
from app.core.database import Base, get_db
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_effect_receipt import DBCapabilityEffectReceipt
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_registry as registry
from app.services import plan_governance as governance, plan_queue, plan_service as plans
from app.services.capability_effect_contract import (
    COMMON_EFFECT_CAPABILITIES,
    EFFECT_RESULT_SCHEMA,
    EFFECT_RESULT_VERSION,
)


REQUESTER, APPROVER = {"user_id": 1}, {"user_id": 2}
CASES = (
    ("monitor.alert.sync", None, {}),
    ("monitor.capacity.execute", 20, {}),
    ("monitor.observability.bind", None, {}),
    ("performance.workload.collect", 20, {}),
    ("performance.sql_profile.collect", 20, {}),
    ("performance.index.collect", 20, {}),
    ("performance.table_profile.collect", 20, {}),
    ("performance.baseline.collect", 20, {}),
    ("performance.capacity.collect", 20, {}),
    ("operations.startup.plan", 10, {}),
    ("operations.shutdown.plan", 10, {}),
    ("operations.switchover.plan", 10, {}),
    ("governance.candidate.scan", 20, {"min_rows": 1}),
    ("protection.backup.plan", 20, {}),
    ("protection.restore.plan", 20, {}),
    ("integration.zabbix.sync", None, {"config_id": 1}),
    ("integration.prometheus.sync", None, {"source_id": 1}),
)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'effect-delivery.db'}",
        connect_args={"timeout": 10, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            User(id=1, username="effect-requester", password_hash="unused", role="admin", status="active"),
            User(id=2, username="effect-approver", password_hash="unused", role="admin", status="active"),
            DBInstance(id=10, instance_name="oracle-10", db_type="oracle", host="127.0.0.1", port=1521,
                       service_name="orcl", oracle_connect_type="service_name", username="unused",
                       password_cipher="env://SYNTHETIC_EFFECT_ORACLE", enabled=1),
            DBInstance(id=20, instance_name="mysql-20", db_type="mysql", host="127.0.0.1", port=3306,
                       username="unused", password_cipher="env://SYNTHETIC_EFFECT_MYSQL", enabled=1),
        ])
        db.commit()
        registry.sync_definitions(db, actor=REQUESTER)
    yield SimpleNamespace(engine=engine, factory=factory)
    engine.dispose()


def _client(factory):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    app.dependency_overrides[assistant._actor] = lambda: REQUESTER
    return TestClient(app)


def _approve(db, plan):
    governance.request_approval(
        db, plan["plan_id"], actor=REQUESTER, expected_revision=plan["revision"])
    row = plans._load(db, plan["plan_id"], REQUESTER, owner_only=True)
    governance.approve_plan(
        db, plan["plan_id"], actor=APPROVER, expected_revision=plan["revision"],
        expected_binding_hash=row.approval_binding_hash)


@pytest.mark.parametrize("capability_id,instance_id,params", CASES)
def test_every_common_effect_is_admitted_once_and_archived_by_the_real_worker(
        env, monkeypatch, capability_id, instance_id, params):
    assert {item[0] for item in CASES} == COMMON_EFFECT_CAPABILITIES
    with env.factory() as db:
        plan = plans.create_plan(
            db, actor=REQUESTER, capability_id=capability_id,
            definition_version=EFFECT_RESULT_VERSION, instance_id=instance_id, params=params)

    with _client(env.factory) as client:
        refused = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert refused.status_code == 409
        assert refused.json()["detail"] == {"code": "approval_required"}
        with env.factory() as db:
            assert db.query(DBCapabilityBatch).count() == 0
            assert db.query(DBCapabilityExecution).count() == 0
            _approve(db, plan)

        queued = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        replay = client.post(f"/api/v1/assistant/plans/{plan['plan_id']}/execute", json={})
        assert queued.status_code == replay.status_code == 200
        assert queued.json()["execution_queue_state"] == replay.json()["execution_queue_state"] == "queued"
        assert queued.json()["execution_batch_id"] == replay.json()["execution_batch_id"]

        def synthetic_effect(context):
            assert context.execution_id == f"{plan['plan_id']}:1:1"
            context.execution_guard()
            data = {"outcome": "success"}
            if capability_id.endswith(".plan"):
                data.update({"plan_only": True, "execution_blocked": True,
                             "script": "-- reviewed plan only; no operation executed"})
            return data

        def transport_proof(_db, saved_plan, step):
            data = plans._common_effect_payload(saved_plan, step, step.result_ref_json)
            assert data["outcome"] == "success"
            return True

        handler_name = registry.EFFECT_HANDLERS[capability_id]
        monkeypatch.setitem(registry.HANDLERS, handler_name, synthetic_effect)
        monkeypatch.setattr(plans, "validate_effect_reference", transport_proof)
        worker = plan_queue.process_one(env.factory, worker_id=f"effect-{capability_id}")
        assert worker["queue_state"] == "archived"
        assert worker["outcome"] == "succeeded"

        saved = client.get(f"/api/v1/assistant/plans/{plan['plan_id']}")
        assert saved.status_code == 200
        value = saved.json()
        assert value["execution_queue_state"] == "archived"
        reference = value["steps"][0]["result_ref"]
        assert reference["kind"] == "capability_effect"
        assert reference["schema"] == EFFECT_RESULT_SCHEMA
        assert reference["inline"]["capability_id"] == capability_id
        assert reference["inline"]["execution_id"] == f"{plan['plan_id']}:1:1"
        with env.factory() as db:
            receipt = db.query(DBCapabilityEffectReceipt).one()
            assert receipt.execution_id == reference["inline"]["execution_id"]
            assert receipt.capability_id == capability_id
            assert receipt.instance_id == instance_id


def test_effect_receipt_migration_is_registered_repeatable_and_retains_history(tmp_path):
    from app.migrations import runner
    from app.migrations.versions import v20261023_01_capability_effect_receipt as migration

    assert migration in runner.MIGRATIONS
    engine = create_engine(f"sqlite:///{tmp_path / 'effect-receipt-migration.db'}")
    try:
        with engine.begin() as connection:
            migration.upgrade(connection)
            migration.upgrade(connection)
            assert inspect(connection).has_table(DBCapabilityEffectReceipt.__tablename__)
            connection.execute(DBCapabilityEffectReceipt.__table__.insert().values(
                execution_id="pl" + "a" * 26 + ":1:1",
                capability_id="monitor.alert.sync",
                definition_version=3,
                requested_by=1,
                instance_id=None,
                target_hash="a" * 64,
                params_hash="b" * 64,
                result_hash="c" * 64,
                created_at=plans._now(),
            ))
            with pytest.raises(RuntimeError, match="must be retained"):
                migration.downgrade(connection)
            assert inspect(connection).has_table(DBCapabilityEffectReceipt.__tablename__)
    finally:
        engine.dispose()

