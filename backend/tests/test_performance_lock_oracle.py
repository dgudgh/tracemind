"""Oracle lock-wait requests use the live page reader and settle through the durable batch."""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.api.v1 import oracle as oracle_api
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_execution import DBCapabilityExecution
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch, capability_registry as registry, plan_queue
from app.services import plan_service as plans


ACTOR = {"user_id": 1}


@pytest.fixture()
def storage(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'oracle-lock-batch.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session(engine) as db:
        db.add(User(id=1, username="oracle-lock-admin", password_hash="unused", role="admin", status="active"))
        for index, (instance_id, db_type) in enumerate(((10, "mysql"), (11, "oracle"), (12, "oracle")), start=1):
            db.add(DBInstance(
                id=instance_id,
                instance_name=f"{db_type}-{instance_id}",
                db_type=db_type,
                host="127.0.0.1",
                port=1500 + instance_id,
                username="unused",
                enabled=1,
                password_cipher="env://NO_LIVE_CONNECTION",
            ))
            db.add(AssetInstance(
                id=instance_id,
                native_instance_id=instance_id,
                stable_key=f"asset:{instance_id}",
                name=f"asset-{instance_id}",
                db_type=db_type,
                ip="127.0.0.1",
                port=1500 + instance_id,
                host_id=index,
                region_id=1,
                site_id=1,
                zone_id=1,
                env_id=1,
                status="active",
            ))
        db.add(AIProviderConfig(
            provider_name="fake",
            provider_type="openai_compatible",
            base_url="http://127.0.0.1:9",
            model_name="fake",
            enabled=1,
            is_default=1,
        ))
        db.commit()
        registry.sync_definitions(db, actor=ACTOR)
    yield engine, factory
    engine.dispose()


def test_all_oracle_lock_waits_filter_targets_and_persist_live_results(storage, monkeypatch):
    _, factory = storage
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *args, **kwargs: json.dumps({
        "capability_id": "performance.lock.read",
        "params": {"page": 1, "page_size": 50},
        "confidence": 0.99,
        "reason": "read all Oracle lock waits",
    }))
    calls = []

    def live_blocking_tree(instance, *, strict=False):
        calls.append((instance.id, strict))
        rows = [] if instance.id == 11 else [
            {"sid": instance.id * 10, "blocking_sid": instance.id * 10 - 1, "wait_s": 9}
        ]
        return {
            "tree": rows,
            "lock_pairs": ([] if not rows else [
                {"blocker_sid": instance.id * 10 - 1, "waiter_sid": instance.id * 10}
            ]),
            "summary": {"blocker_count": int(bool(rows)), "waiter_count": int(bool(rows)),
                        "total": len(rows), "max_wait_seconds": 9 if rows else 0},
        }

    monkeypatch.setattr(oracle_api, "_get_blocking_tree", live_blocking_tree)
    with factory() as db:
        dispatched = capability_dispatch.dispatch(
            db,
            actor=ACTOR,
            text="检查所有 Oracle 数据库的锁等待",
            instance=None,
            request_id="req-all-oracle-lock-waits",
        )
        assert dispatched["action"] == "batch"
        assert {item["instance_id"] for item in dispatched["plans"]} == {11, 12}
        batch_id = dispatched["batch"]["batch_id"]

    completed = []
    while result := plan_queue.process_one(factory, worker_id="oracle-lock-worker"):
        completed.append(result)

    assert calls == [(11, True), (12, True)]
    assert len(completed) == 2
    assert all(item["queue_state"] == "archived" and item["outcome"] == "succeeded" for item in completed)
    with factory() as db:
        members = db.query(DBCapabilityExecution).filter_by(batch_id=batch_id).order_by(DBCapabilityExecution.id).all()
        assert [item.state for item in members] == ["archived", "archived"]
        saved = [plans.read_plan(db, item.plan_id, actor=ACTOR) for item in members]
        assert [item["instance_id"] for item in saved] == [11, 12]
        assert all(item["definition_version"] == 3 for item in saved)
        assert [item["steps"][0]["result_ref"]["inline"]["items"] for item in saved] == [
            [], [{"sid": 120, "blocking_sid": 119, "wait_s": 9}],
        ]
        assert [item["steps"][0]["result_ref"]["inline"]["summary"]["waiter_count"]
                for item in saved] == [0, 1]


def test_strict_oracle_reader_rejects_connection_success_when_both_queries_fail(storage, monkeypatch):
    class BrokenCursor:
        def execute(self, _sql):
            raise RuntimeError("missing dictionary privilege")

    class Connection:
        def cursor(self):
            return BrokenCursor()

    @contextmanager
    def connected(_instance):
        yield Connection()

    from app.collectors.oracle import oracle_connection

    monkeypatch.setattr(oracle_connection, "oracle_connection", connected)
    _, factory = storage
    with factory() as db:
        instance = db.get(DBInstance, 11)
        compatible = oracle_api._get_blocking_tree(instance)
        assert compatible["tree"] == [] and compatible["summary"]["total"] == 0
        with pytest.raises(RuntimeError, match="oracle_lock_query_failed"):
            oracle_api._get_blocking_tree(instance, strict=True)


def test_oracle_query_failure_settles_plans_failed_without_empty_success(storage, monkeypatch):
    _, factory = storage
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *args, **kwargs: json.dumps({
        "capability_id": "performance.lock.read",
        "params": {"page": 1, "page_size": 50},
        "confidence": 0.99,
        "reason": "read all Oracle lock waits",
    }))
    calls = []

    def failed_query(instance, *, strict=False):
        calls.append((instance.id, strict))
        raise RuntimeError("oracle_lock_query_failed")

    monkeypatch.setattr(oracle_api, "_get_blocking_tree", failed_query)
    with factory() as db:
        dispatched = capability_dispatch.dispatch(
            db,
            actor=ACTOR,
            text="检查所有 Oracle 数据库的锁等待",
            instance=None,
            request_id="req-all-oracle-lock-failure",
        )
        batch_id = dispatched["batch"]["batch_id"]

    completed = []
    while result := plan_queue.process_one(factory, worker_id="oracle-lock-worker"):
        completed.append(result)

    assert calls == [(11, True), (12, True)]
    assert len(completed) == 2
    assert all(item["queue_state"] == "archived" and item["outcome"] == "failed" for item in completed)
    with factory() as db:
        members = db.query(DBCapabilityExecution).filter_by(batch_id=batch_id).order_by(DBCapabilityExecution.id).all()
        saved = [plans.read_plan(db, item.plan_id, actor=ACTOR) for item in members]
        assert all(item["outcome"] == "failed" for item in saved)
        assert all(item["steps"][0]["status"] == "failed"
                   and item["steps"][0]["result_ref"] is None for item in saved)
