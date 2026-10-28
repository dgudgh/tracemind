"""Prove the production startup path fills the capability ledger.

Every other suite syncs the ledger inside its own fixture before asserting
anything, which is exactly how a registry with no production caller stayed
green. These tests therefore drive ``main.lifespan`` and never call
``sync_definitions``: if the startup call disappears, they go red.
"""
import asyncio
from datetime import datetime
import logging
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app import main
from app.api.v1 import assistant
from app.core import auth, database
from app.models.access_control import SecurityAuditEvent, UserAccessRole
from app.models.capability_definition import DBCapabilityDefinition
from app.models.user import User
from app.services import access_control, capability_registry as registry


WIRED = ("inspection.run.query", "inspection.template.read", "operations.capability_matrix.read")
PREFIX = "/api/v1/assistant"
DECLARATION_COUNT = len(registry.CAPABILITY_REGISTRY)


@pytest.fixture
def boot(tmp_path, monkeypatch, signing_key_path):
    """A fresh database with one admin and no manual sync, started via the real lifespan."""
    engine = create_engine(f"sqlite:///{tmp_path / 'startup.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        roles = {row.role_key: row for row in access_control.ensure_builtin_roles(db)}
        db.add_all([
            User(id=1, username="startup-admin", password_hash="unused", role="admin", status="active"),
            User(id=2, username="startup-reader", password_hash="unused", role="user", status="active"),
        ])
        db.add(UserAccessRole(user_id=2, role_id=roles["viewer"].id))
        db.commit()
        assert db.query(DBCapabilityDefinition).count() == 0, "the fixture must not pre-sync"

    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "initialize_schema", lambda _engine: [])
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(auth, "AUTH_TOKENS", {})
    monkeypatch.setattr(auth.settings, "JWT_PRIVATE_KEY_PATH", str(signing_key_path))
    from app.tasks import master_scheduler

    monkeypatch.setattr(master_scheduler, "start_master_scheduler", lambda: None)
    monkeypatch.setattr(master_scheduler, "stop_master_scheduler", lambda: None)

    async def startup_shutdown():
        async with main.lifespan(main.app):
            pass

    def start():
        asyncio.run(startup_shutdown())

    yield SimpleNamespace(start=start, factory=factory, engine=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def signing_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("startup-jwt") / "signing.pem"


def audit_rows(factory):
    with factory() as db:
        return db.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id).all()


def test_startup_makes_the_three_wired_readers_resolvable(boot):
    boot.start()

    with boot.factory() as db:
        assert db.query(DBCapabilityDefinition).count() == DECLARATION_COUNT
        for capability_id in WIRED:
            handler, context = registry.resolve_handler(db, capability_id, 1, actor={"user_id": 1})
            assert callable(handler), capability_id
            assert context.actor_id == 1
        # The ledger records the process, not a borrowed administrator identity.
        event = audit_rows(boot.factory)[-1]
        assert event.action == "sync_definitions"
        assert event.actor == registry.STARTUP_SYNC_ACTOR
        assert event.actor_user_id is None
        assert event.details["created"] == DECLARATION_COUNT


def test_capabilities_endpoint_reports_them_as_available(boot):
    boot.start()

    with boot.factory() as db:
        # Admin sees every declaration; a reader's list is filtered by its grants.
        token = auth.issue_user_token(db.get(User, 1), source="db", db=db)["token"]

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix="/api/v1")
    app.add_middleware(main.AuthMiddleware)

    def isolated_db():
        with boot.factory() as session:
            yield session

    app.dependency_overrides[database.get_db] = isolated_db
    try:
        with TestClient(app) as client:
            response = client.get(PREFIX + "/capabilities",
                                  headers={"Authorization": "Bearer " + token})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["items"]) == DECLARATION_COUNT
    by_id = {item["capability_id"]: item for item in body["items"]}
    for capability_id in WIRED:
        item = by_id[capability_id]
        assert item["unavailable_reason"] != "definition_not_synced"
        assert item["unavailable_reason"] is None, (capability_id, item["unavailable_reason"])
        assert item["availability"] == "available"
        assert item["plan_executable"] is True


def test_second_boot_writes_nothing_new(boot):
    boot.start()
    boot.start()

    events = audit_rows(boot.factory)
    assert len(events) == 2
    assert events[0].details["created"] == DECLARATION_COUNT
    assert events[0].details["updated"] == 0
    assert events[1].details["created"] == 0
    assert events[1].details["updated"] == 0
    assert events[1].details["unchanged"] == DECLARATION_COUNT


def test_operator_disable_survives_a_restart(boot):
    boot.start()
    with boot.factory() as db:
        registry.set_enabled(db, "inspection.run.query", enabled=False, actor={"user_id": 1})
    boot.start()
    with boot.factory() as db:
        assert registry.effective_capability(db, "inspection.run.query")["enabled"] is False


@pytest.mark.parametrize(("row_version", "row_hash", "reason"), [
    (99, "f" * 64, "definition_version_regression"),
    (1, "f" * 64, "definition_version_not_incremented"),
])
def test_conflicting_ledger_row_is_skipped_with_a_warning(boot, caplog, row_version, row_hash, reason):
    with boot.factory() as db:
        db.add(DBCapabilityDefinition(
            capability_id="inspection.run.query", definition_version=row_version,
            definition_hash=row_hash, domain="inspection", kind="read", execution="server",
            enabled=1, synced_at=datetime(2026, 10, 20),
        ))
        db.commit()

    with caplog.at_level(logging.WARNING, logger="app.services.capability_registry"):
        boot.start()  # must not raise: a newer ledger cannot gate the service

    assert "capability_registry_startup_sync_skipped" in caplog.text
    assert reason in caplog.text
    event = audit_rows(boot.factory)[-1]
    assert event.details["skipped"] == [{"capability_id": "inspection.run.query", "reason": reason}]

    with boot.factory() as db:
        row = db.get(DBCapabilityDefinition, "inspection.run.query")
        assert row.definition_version == row_version
        assert row.definition_hash == row_hash
        # The other two readers still came up; only the conflicting row was skipped.
        assert db.query(DBCapabilityDefinition).count() == DECLARATION_COUNT
        for capability_id in WIRED[1:]:
            handler, _ = registry.resolve_handler(db, capability_id, 1, actor={"user_id": 1})
            assert callable(handler)
        with pytest.raises(registry.CapabilityError, match="definition_out_of_sync"):
            registry.resolve_handler(db, "inspection.run.query", 1, actor={"user_id": 1})
