#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace

from _bootstrap import add_backend_to_path

os.environ.setdefault("DEBUG", "false")
add_backend_to_path()

from app.services import ai_config


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"response": "ok"}


def main() -> int:
    captured: dict = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    original_post = ai_config.requests.post
    ai_config.requests.post = fake_post
    try:
        config = SimpleNamespace(
            provider_type="ollama",
            base_url="http://127.0.0.1:11434",
            model_name="qwen3.6:27b",
            timeout_seconds=300,
        )
        result = ai_config.call_ollama(config, "请做端到端数据库诊断")
    finally:
        ai_config.requests.post = original_post

    payload = captured.get("payload") or {}
    options = payload.get("options") or {}
    assert result == "ok"
    assert payload.get("model") == "qwen3.6:27b"
    assert payload.get("stream") is False
    assert payload.get("think") is False
    assert str(payload.get("prompt") or "").startswith("/no_think\n")
    assert options.get("temperature") == 0.2
    assert options.get("top_p") == 0.9
    assert 800 <= int(options.get("num_predict") or 0) <= 1600
    assert int(options.get("num_ctx") or 0) >= 8192
    assert captured.get("timeout") == 300
    print("ollama request policy ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
