"""RQ12: an all-instance read request persists a batch and a worker settles every instance."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch, capability_registry as registry, plan_queue


@pytest.fixture()
def storage(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session(engine) as db:
        db.add(User(id=1, username="batch-admin", password_hash="unused", role="admin", status="active"))
        for index, instance_id in enumerate((10, 11), start=1):
            db.add(DBInstance(id=instance_id, instance_name=f"mysql-{instance_id}", db_type="mysql",
                              host="127.0.0.1", port=instance_id, username="u", enabled=1,
                              password_cipher="env://NO"))
            db.add(AssetInstance(id=instance_id, native_instance_id=instance_id,
                                 stable_key=f"asset:{instance_id}", name=f"a{instance_id}", db_type="mysql",
                                 ip="127.0.0.1", port=instance_id, host_id=index, region_id=1, site_id=1,
                                 zone_id=1, env_id=1, status="active"))
        db.add(AIProviderConfig(provider_name="fake", provider_type="openai_compatible",
                                base_url="http://127.0.0.1:9", model_name="fake", enabled=1, is_default=1))
        db.commit()
        registry.sync_definitions(db, actor={"user_id": 1})
    yield engine, factory
    engine.dispose()


def test_all_instance_read_is_processed_by_the_worker(storage, monkeypatch):
    engine, factory = storage
    monkeypatch.setattr(capability_dispatch, "call_ai_provider", lambda *a, **k: json.dumps({
        "capability_id": "monitor.observability.read", "params": {}, "confidence": 0.9,
        "reason": "all instances"}))
    with factory() as db:
        value = capability_dispatch.dispatch(
            db, actor={"user_id": 1}, text="检查所有实例的可观测联动",
            instance=db.get(DBInstance, 10), request_id="req-batch-worker",
        )
        batch_id = value["batch"]["batch_id"]
        assert value["action"] == "batch"
        assert len(value["plans"]) == 2
    claims = []
    while True:
        claim = plan_queue.process_one(factory, worker_id="batch-worker")
        if claim is None:
            break
        claims.append(claim)
    assert len(claims) == 2
    with factory() as db:
        assert db.query(DBCapabilityBatch).filter_by(batch_id=batch_id).count() == 1
        members = db.query(DBCapabilityExecution).filter_by(batch_id=batch_id).all()
        assert len(members) == 2
        assert {row.state for row in members} <= {"succeeded", "failed", "partial", "unknown", "archived"}
        assert all(row.state not in {"queued", "running"} for row in members)
