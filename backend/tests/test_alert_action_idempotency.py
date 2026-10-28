import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import alert_events
from app.core.database import Base, get_db
from app.models.alert_event import DBAlertEvent, DBAlertActionLog


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    with session() as db:
        db.add_all([
            DBAlertEvent(id=11, event_uid="allowed", source="manual", title="Allowed", instance_id=101),
            DBAlertEvent(id=22, event_uid="private", source="manual", title="PRIVATE", instance_id=202),
        ])
        db.commit()
    app = FastAPI()
    identity = {"source": "db", "user_id": 7, "username": "operator", "role": "user"}

    @app.middleware("http")
    async def fixture_identity(request: Request, call_next):
        request.state.auth = dict(identity)
        return await call_next(request)

    def database():
        with session() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.include_router(alert_events.router, prefix="/api/v1")
    monkeypatch.setattr(alert_events, "_scope_native_ids", lambda *a, **kw: frozenset({101}))
    with TestClient(app) as client:
        yield client, session, identity
    engine.dispose()


def test_foreign_event_key_is_conflict_without_returning_private_payload(api):
    client, sessions, _ = api
    with sessions() as db:
        db.add(DBAlertActionLog(event_id=22, action_type="notify", action_status="pending",
                               idempotency_key="collision", operator="operator", max_retries=3,
                               request_json='{"secret":"PRIVATE"}', response_json='{"token":"PRIVATE"}'))
        db.commit()
    response = client.post("/api/v1/alert-events/11/actions", json={"idempotency_key": "collision", "plan_only": False})
    assert response.status_code == 409
    assert "PRIVATE" not in response.text
    assert client.post("/api/v1/alert-events/22/actions", json={}).status_code == 404
    with sessions() as db:
        assert db.query(DBAlertActionLog).count() == 1


def test_identical_retry_reuses_action_and_operator_cannot_be_spoofed(api):
    client, sessions, _ = api
    data = {"idempotency_key": "retry", "operator": "spoofed", "request": {"a": 1, "b": 2}}
    first = client.post("/api/v1/alert-events/11/actions", json=data)
    assert first.status_code == 200
    assert first.json()["operator"] == "operator"
    data["request"] = {"b": 2, "a": 1}
    second = client.post("/api/v1/alert-events/11/actions", json=data)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    with sessions() as db:
        assert db.query(DBAlertActionLog).count() == 1


@pytest.mark.parametrize("changed", [
    {"action_type": "ticket"}, {"target": "other"}, {"plan_only": False},
    {"request": {"changed": True}}, {"max_retries": 5},
])
def test_reused_key_with_changed_action_parameters_is_rejected(api, changed):
    client, _, _ = api
    data = {"idempotency_key": "same"}
    assert client.post("/api/v1/alert-events/11/actions", json=data).status_code == 200
    assert client.post("/api/v1/alert-events/11/actions", json={**data, **changed}).status_code == 409


def test_another_actor_cannot_replay_an_existing_action(api):
    client, _, identity = api
    data = {"idempotency_key": "same"}
    assert client.post("/api/v1/alert-events/11/actions", json=data).status_code == 200
    identity.update(user_id=8, username="another")
    assert client.post("/api/v1/alert-events/11/actions", json=data).status_code == 409
