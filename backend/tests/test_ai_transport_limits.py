"""C11 transport boundaries against a real local HTTP server, not a mocked response.

A mocked ``requests.post`` cannot prove that a streaming byte ceiling stops reading, that a
redirect is refused rather than followed, or that a slow provider is cut off by the wall
clock. This file runs an actual HTTP server on loopback; it never reaches a model or the
internet, and the fake model returns fixed text.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from app.services import ai_config


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if "/slow" in self.path:
            time.sleep(1.5)
            return self._send(200, _completion("late"))
        if "/redirect" in self.path:
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "/invalid" in self.path:
            return self._send(200, b"{not json")
        if "/huge" in self.path:
            payload = json.dumps({"choices": [{"finish_reason": "stop",
                                               "message": {"content": "x" * (ai_config.MAX_PROVIDER_RESPONSE_BYTES + 10)}}]}).encode()
            return self._send(200, payload)
        if "/http_error" in self.path:
            return self._send(503, b'{"error":"down"}')
        if "/proposal" in self.path:
            matched = json.dumps({"decision": "matched", "workflow_id": "inspection.jobs.list",
                                  "workflow_version": 1, "slots": {}})
            return self._send(200, _completion(matched))
        return self._send(200, _completion("probe complete"))

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client deliberately hung up (byte ceiling / deadline)


def _completion(text):
    return json.dumps({"model": "local-fake", "choices": [
        {"finish_reason": "stop", "message": {"content": text}}]}).encode()


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _config(base_url, *, timeout=10):
    return ai_config.AIProviderConfig(
        provider_type="openai_compatible", base_url=base_url, model_name="local-fake",
        api_key_cipher=None, timeout_seconds=timeout,
    )


def test_a_complete_response_is_the_only_success(server):
    text = ai_config.call_openai_compatible(_config(server), "hello", json_mode=True)
    assert str(text) == "probe complete"
    assert text.metadata["validation_status"] == "transport_complete"


def test_oversized_response_is_stopped_by_the_byte_ceiling(server):
    with pytest.raises(ai_config.AIProviderResponseError) as error:
        ai_config.call_openai_compatible(_config(server + "/huge"), "hello")
    assert error.value.code == "response_too_large"


def test_a_redirect_is_refused_instead_of_followed(server):
    with pytest.raises(ai_config.AIProviderResponseError) as error:
        ai_config.call_openai_compatible(_config(server + "/redirect"), "hello")
    assert error.value.code == "provider_redirect_rejected"


def test_http_error_and_invalid_json_are_codes_not_raw_text(server):
    for suffix, code in (("/http_error", "provider_http_error"), ("/invalid", "invalid_response")):
        with pytest.raises(ai_config.AIProviderResponseError) as error:
            ai_config.call_openai_compatible(_config(server + suffix), "hello")
        assert error.value.code == code


def test_slow_provider_hits_the_wall_clock(server):
    started = time.monotonic()
    with pytest.raises(ai_config.AIProviderResponseError) as error:
        ai_config.call_openai_compatible(_config(server + "/slow", timeout=1), "hello")
    assert error.value.code == "provider_timeout"
    assert time.monotonic() - started < 3


def test_the_budget_is_a_shared_row_not_per_worker_memory(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    import app.models  # noqa: F401
    from app.core.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ai_config, "AI_DAILY_REQUESTS", 2, raising=False)
    monkeypatch.setattr(ai_config, "AI_DAILY_PROMPT_BYTES", 10_000, raising=False)
    with Session(engine, autoflush=False) as first:
        assert ai_config.consume_budget(first, prompt_bytes=100)["requests"] == 1
    with Session(engine, autoflush=False) as second:
        assert ai_config.consume_budget(second, prompt_bytes=100)["requests"] == 2
        with pytest.raises(ai_config.AIBudgetExceeded):
            ai_config.consume_budget(second, prompt_bytes=1)
    engine.dispose()
