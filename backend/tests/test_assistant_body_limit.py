"""Count ASGI chunks through real auth middleware and assistant request parsers."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from app import main
from app.api.v1 import assistant
from app.core.database import get_db
from app.core.request_limits import ASSISTANT_BODY_LIMIT


LIMIT = ASSISTANT_BODY_LIMIT
CHUNK_SIZE = 8192
PLAN_ID = "pl" + "a" * 26


class CountedStream:
    def __init__(self, chunks, *, disconnect_after=None):
        self.chunks = chunks
        self.disconnect_after = disconnect_after
        self.consumed = 0
        self.disconnects = 0

    async def receive(self):
        if (self.consumed == self.disconnect_after or self.consumed == len(self.chunks)):
            self.disconnects += 1
            return {"type": "http.disconnect"}
        body = self.chunks[self.consumed]
        self.consumed += 1
        return {"type": "http.request", "body": body,
                "more_body": self.consumed < len(self.chunks)}


@pytest.fixture(params=[(role, action) for role in ("admin", "user")
                       for action in ("create", "execute")],
                ids=lambda pair: "-".join(pair))
def boundary(request, monkeypatch):
    role, action = request.param
    observed = SimpleNamespace(
        role=role, action=action, requests=[], policy_bodies=[], principals=[], services=[], auth_reads=[],
    )
    database = object()
    identity = {"source": "db", "role": role, "user_id": 7}
    real_bounded_body = main.bounded_body

    def authenticate(token):
        assert token == "synthetic-body-limit-token"
        observed.auth_reads.append(observed.stream.consumed)
        return identity

    async def capture_ingestion(incoming, *, limit):
        observed.requests.append(incoming)
        assert incoming.state.auth == identity
        assert limit == LIMIT
        return await real_bounded_body(incoming, limit=limit)

    def authorize(incoming, auth_info, body):
        assert auth_info == identity
        assert incoming._body == body
        observed.policy_bodies.append(body)

    def principal(db, actor):
        assert db is database
        assert actor == {"user_id": 7}
        observed.principals.append(actor)
        return SimpleNamespace(id=7), {"asset.read"}, None

    def create(db, **values):
        assert db is database
        observed.services.append(values)
        return {"plan_id": PLAN_ID, "outcome": None, "reused": False}

    def execute(db, plan_id, *, actor):
        assert db is database
        observed.services.append({"plan_id": plan_id, "actor": actor})
        return {"plan_id": plan_id, "outcome": "succeeded"}

    def isolated_db():
        yield database

    monkeypatch.setattr(main, "get_token_info", authenticate)
    monkeypatch.setattr(main, "bounded_body", capture_ingestion)
    monkeypatch.setattr(main, "_authorize_request", authorize)
    monkeypatch.setattr(assistant.registry, "_principal", principal)
    monkeypatch.setattr(assistant.plans, "create_plan", create)
    monkeypatch.setattr(assistant.plans, "execute_plan", execute)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.include_router(assistant.router, prefix=main.settings.API_V1_PREFIX)
    app.add_middleware(main.AuthMiddleware)
    app.dependency_overrides[get_db] = isolated_db
    observed.app = app
    observed.path = main.settings.API_V1_PREFIX + "/assistant/plans"
    if action == "execute":
        observed.path += "/" + PLAN_ID + "/execute"
    observed.payload = ({"capability_id": "inspection.run.query", "definition_version": 1,
                         "params": {"page": 2}} if action == "create" else {})
    return observed


def invoke(boundary, chunks, *, content_length=None, disconnect_after=None, path=None):
    stream = CountedStream(chunks, disconnect_after=disconnect_after)
    boundary.stream = stream
    messages = []
    headers = [(b"authorization", b"Bearer synthetic-body-limit-token"),
               (b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path or boundary.path, "root_path": "", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 12345), "server": ("review", 80),
    }

    async def send(message):
        messages.append(message)

    async def run():
        await asyncio.wait_for(boundary.app(scope, stream.receive, send), timeout=5)

    asyncio.run(run())
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    body = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    return starts[0]["status"], json.loads(body)


def assert_stopped_before_business(boundary):
    assert boundary.auth_reads == [0]
    assert len(boundary.requests) == 1
    assert not hasattr(boundary.requests[0], "_body")
    assert boundary.policy_bodies == []
    assert boundary.principals == []
    assert boundary.services == []


@pytest.mark.parametrize("content_length", [None, "invalid", "-1", "0", "1.5", "9" * 5000 + "x"],
                         ids=["chunked", "malformed", "negative", "underdeclared", "decimal", "huge-malformed"])
def test_stream_stops_at_first_over_budget_chunk(boundary, content_length):
    assert LIMIT == 4 * CHUNK_SIZE
    chunks = [b"x" * CHUNK_SIZE] * 128
    status, body = invoke(boundary, chunks, content_length=content_length)
    assert status == 413
    assert body == {"detail": {"code": "request_too_large"}}
    assert boundary.stream.consumed == LIMIT // CHUNK_SIZE + 1
    assert boundary.stream.consumed < len(chunks)
    assert_stopped_before_business(boundary)


@pytest.mark.parametrize("trailing_slash", [False, True])
def test_declared_oversize_is_rejected_without_receiving_a_chunk(boundary, trailing_slash):
    path = boundary.path + ("/" if trailing_slash else "")
    status, body = invoke(boundary, [b"x" * CHUNK_SIZE] * 128,
                          content_length=str(LIMIT + 1), path=path)
    assert status == 413
    assert body == {"detail": {"code": "request_too_large"}}
    assert boundary.stream.consumed == 0
    assert_stopped_before_business(boundary)


def test_dispatch_intent_uses_the_same_streaming_body_limit(boundary):
    path = main.settings.API_V1_PREFIX + "/assistant/dispatch-intent"
    status, body = invoke(boundary, [b"x" * CHUNK_SIZE] * 128,
                          content_length=str(LIMIT + 1), path=path)
    assert status == 413
    assert body == {"detail": {"code": "request_too_large"}}
    assert boundary.stream.consumed == 0
    assert_stopped_before_business(boundary)


@pytest.mark.parametrize("at_limit", [False, True], ids=["small", "exact-limit"])
def test_cached_body_replays_through_policy_and_actual_assistant_handler(boundary, at_limit):
    raw = json.dumps(boundary.payload).encode("ascii")
    if at_limit:
        raw += b" " * (LIMIT - len(raw))
    chunks = [raw[start:start + CHUNK_SIZE] for start in range(0, len(raw), CHUNK_SIZE)]
    status, body = invoke(boundary, chunks, content_length=str(len(raw)))
    assert status == (201 if boundary.action == "create" else 200)
    assert body["plan_id"] == PLAN_ID
    assert boundary.stream.consumed == len(chunks)
    assert boundary.auth_reads == [0]
    assert len(boundary.requests) == 1
    assert boundary.requests[0]._body == raw
    assert len(boundary.requests[0]._body) <= LIMIT
    assert boundary.policy_bodies == ([raw] if boundary.role == "user" else [])
    assert boundary.principals == [{"user_id": 7}]
    if boundary.action == "create":
        assert boundary.services == [{"actor": {"user_id": 7}, **boundary.payload,
                                      "idempotency_key": None}]
    else:
        assert boundary.services == [{"plan_id": PLAN_ID, "actor": {"user_id": 7}}]


def test_disconnect_during_ingestion_returns_safe_400_without_downstream_execution(boundary):
    status, body = invoke(boundary, [b"{", b"unread"], disconnect_after=1)
    assert status == 400
    assert body == {"detail": {"code": "request_body_interrupted"}}
    assert boundary.stream.consumed == 1
    assert boundary.stream.disconnects == 1
    assert_stopped_before_business(boundary)
