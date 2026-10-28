"""F02: one request id may enter the model at most once, even under concurrency."""
from __future__ import annotations

import json
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.cmdb.models import AssetInstance
from app.core.database import Base
from app.models.ai_provider_config import AIProviderConfig
from app.models.db_instance import DBInstance
from app.models.user import User
from app.services import capability_dispatch, capability_registry as registry


def test_concurrent_same_request_calls_the_model_once(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session(engine) as db:
        db.add(User(id=1, username="claim-admin", password_hash="unused", role="admin", status="active"))
        db.add(DBInstance(id=10, instance_name="m", db_type="mysql", host="127.0.0.1", port=1,
                          username="u", enabled=1, password_cipher="env://NO"))
        db.add(AIProviderConfig(provider_name="fake", provider_type="openai_compatible",
                                base_url="http://127.0.0.1:9", model_name="fake", enabled=1, is_default=1))
        db.add(AssetInstance(id=10, native_instance_id=10, stable_key="asset:10", name="a", db_type="mysql",
                             ip="127.0.0.1", port=1, host_id=1, region_id=1, site_id=1, zone_id=1,
                             env_id=1, status="active"))
        db.commit()
        registry.sync_definitions(db, actor={"user_id": 1})
    started, release = threading.Event(), threading.Event()
    calls = {"count": 0}

    def model(*args, **kwargs):
        calls["count"] += 1
        started.set()
        assert release.wait(5)
        return json.dumps({"capability_id": "report.catalog.read", "params": {"page": 1, "page_size": 10},
                           "confidence": 0.9, "reason": "test"})

    monkeypatch.setattr(capability_dispatch, "call_ai_provider", model)
    first = {}

    def run_first():
        with factory() as db:
            first["value"] = capability_dispatch.dispatch(
                db, actor={"user_id": 1}, text="列出报告", instance=db.get(DBInstance, 10),
                request_id="req-concurrent",
            )

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(5)
    with factory() as db:
        second = capability_dispatch.dispatch(
            db, actor={"user_id": 1}, text="列出报告", instance=db.get(DBInstance, 10),
            request_id="req-concurrent",
        )
    assert second["reason_code"] == "request_in_progress"
    release.set()
    thread.join(10)
    assert calls["count"] == 1
    assert not thread.is_alive()
    assert first["value"]["action"] == "executed"
    engine.dispose()
