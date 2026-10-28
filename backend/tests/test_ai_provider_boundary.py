import hashlib
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import ai_config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Provider boundary tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(ai_config.requests.sessions.Session, "request", denied)
    monkeypatch.setenv("MESGUARD_OLLAMA_NUM_CTX", "8192")
    monkeypatch.setenv("MESGUARD_OLLAMA_NUM_PREDICT", "1200")


def config(**changes):
    return SimpleNamespace(**{
        "id": 1, "provider_type": "ollama", "model_name": "qwen3-local",
        "base_url": "http://provider.invalid", "api_key_cipher": None,
        "timeout_seconds": 7, **changes,
    })


def response(body, status=200):
    result = ai_config.requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result.url = "http://provider.invalid"
    return result


def complete(provider_type):
    if provider_type == "ollama":
        return {"model": "actual-revision", "done": True, "done_reason": "stop", "response": "probe complete"}
    return {"model": "actual-revision", "choices": [
        {"finish_reason": "stop", "message": {"content": "probe complete"}},
    ]}


def transport(monkeypatch, *, provider_type="ollama", capacity=32768, show=None, inference=None):
    if show is None:
        show = response({"model_info": {"test.context_length": capacity}})
    if inference is None:
        inference = response(complete(provider_type))

    def post(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        expected = "/api/generate" if provider_type == "ollama" else "/chat/completions"
        assert url in {"http://provider.invalid/api/show", f"http://provider.invalid{expected}"}
        result = show if url.endswith("/api/show") else inference
        if isinstance(result, Exception):
            raise result
        return result

    mock = Mock(side_effect=post)
    monkeypatch.setattr(ai_config.requests, "post", mock)
    return mock


@pytest.mark.parametrize("entry", [ai_config.call_ollama, ai_config.call_ai_provider])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("prompt", ["x" * 100000, "select '" + "\u4e2d" * 12000 + "' from dual",
                                   "AWR: " + "SQL evidence\n" * 4000])
def test_all_ollama_entries_reject_full_oversized_prompt_before_generation(monkeypatch, entry, json_mode, prompt):
    post = transport(monkeypatch)
    with pytest.raises(ai_config.AIProviderResponseError, match="context_exceeded"):
        entry(config(), prompt, json_mode=json_mode)
    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/api/show")
    assert post.call_args.kwargs["json"] == {"model": "qwen3-local"}
    assert prompt not in json.dumps(post.call_args.kwargs)


@pytest.mark.parametrize("entry", [ai_config.call_ollama, ai_config.call_ai_provider])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
def test_capacity_boundary_includes_qwen_prefix_output_and_overhead(monkeypatch, entry, json_mode, extra):
    prefix = "/no_think\n"
    prompt = "x" * (4096 - 1200 - 512 - len(prefix) + extra)
    post = transport(monkeypatch, capacity=4096)
    if extra:
        with pytest.raises(ai_config.AIProviderResponseError, match="context_exceeded"):
            entry(config(), prompt, json_mode=json_mode)
        assert post.call_count == 1
        return
    result = entry(config(), prompt, json_mode=json_mode)
    assert result == "probe complete"
    assert post.call_count == 2
    payload = post.call_args.kwargs["json"]
    assert payload["prompt"] == prefix + prompt
    assert payload["options"]["num_ctx"] == 4096
    assert payload["options"]["num_predict"] == 1200
    assert (payload.get("format") == "json") is json_mode
    assert result.metadata["prompt_digest"] == hashlib.sha256(payload["prompt"].encode()).hexdigest()


@pytest.mark.parametrize("prompt", ["\u4e2d" * 2000, "x" * 10000, "/think\nfull evidence", "/no_think\nfull evidence"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_context_expands_without_changing_or_truncating_prompt(monkeypatch, prompt, json_mode):
    post = transport(monkeypatch)
    ai_config.call_ai_provider(config(), prompt, json_mode=json_mode)
    prepared = prompt if prompt.startswith(("/think", "/no_think")) else "/no_think\n" + prompt
    payload = post.call_args.kwargs["json"]
    assert payload["prompt"] == prepared
    assert payload["think"] is False
    assert payload["options"]["num_ctx"] == max(8192, len(prepared.encode()) + 1200 + 512)


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("body", [
    {}, [], {"error": "unavailable"}, {"model_info": None}, {"model_info": []},
    {"model_info": {}}, {"model_info": {"test.context_length": "32768"}},
    {"model_info": {"test.context_length": True}}, {"model_info": {"test.context_length": 0}},
    {"model_info": {"test.context_length": -1}}, {"model_info": {"test.context_length": 32768.0}},
    {"model_info": {"test.context_length": 32768, "other.context_length": "unknown"}},
    {"model_info": {"test.context_length": 32768, "other.context_length": 0}},
])
def test_unknown_context_fails_closed(monkeypatch, json_mode, body):
    post = transport(monkeypatch, show=response(body))
    with pytest.raises(ai_config.AIProviderResponseError, match="context_unavailable"):
        ai_config.call_ai_provider(config(), "private evidence", json_mode=json_mode)
    assert post.call_count == 1


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "redirect"])
def test_context_probe_failure_never_sends_evidence(monkeypatch, failure):
    show = response({}, 503 if failure == "http" else 307 if failure == "redirect" else 200)
    if failure == "timeout":
        show = ai_config.requests.Timeout("offline")
    elif failure == "json":
        show._content = b"{"
    post = transport(monkeypatch, show=show)
    code = "provider_redirect_rejected" if failure == "redirect" else "context_unavailable"
    with pytest.raises(ai_config.AIProviderResponseError, match=code):
        ai_config.call_ollama(config(), "private evidence")
    assert post.call_count == 1


def test_smallest_reported_capacity_and_configured_output_budget_are_used(monkeypatch):
    monkeypatch.setenv("MESGUARD_OLLAMA_NUM_PREDICT", "4096")
    show = response({"model_info": {"first.context_length": 32768, "second.context_length": 4096}})
    post = transport(monkeypatch, show=show)
    with pytest.raises(ai_config.AIProviderResponseError, match="context_exceeded"):
        ai_config.call_ollama(config(), "tiny")
    assert post.call_count == 1


def connection_data(provider_type):
    return {"provider_type": provider_type, "base_url": "http://provider.invalid/",
            "model_name": "qwen3-local", "timeout_seconds": 7, "api_key": "offline-test-key",
            "context": "PRIVATE_CONTEXT_MUST_NOT_BE_SENT"}


@pytest.mark.parametrize("provider_type", ["ollama", "openai_compatible"])
def test_connection_probe_uses_real_transport_and_complete_response(monkeypatch, provider_type):
    post = transport(monkeypatch, provider_type=provider_type)
    result = ai_config.test_ai_provider(connection_data(provider_type))
    assert set(result) == {"ok", "message", "detail"}
    assert result["ok"] is True
    assert result["detail"]["response"] == "probe complete"
    assert result["detail"]["inference"]["actual_model"] == "actual-revision"
    assert result["detail"]["inference"]["validation_status"] == "transport_complete"
    assert "PRIVATE_CONTEXT_MUST_NOT_BE_SENT" not in str(post.call_args_list)
    assert "offline-test-key" not in json.dumps(result)
    # (connect, read) bounded by the same wall clock, and the body is streamed so the
    # byte ceiling can stop before the whole response is materialized.
    assert post.call_args.kwargs["timeout"] == (7, 7)
    assert post.call_args.kwargs["stream"] is True
    payload = post.call_args.kwargs["json"]
    if provider_type == "ollama":
        assert post.call_count == 2
        assert payload["prompt"].startswith("/no_think\n")
        assert payload["options"]["num_predict"] == 1200
        assert payload["think"] is False
        assert payload["stream"] is False
    else:
        assert post.call_count == 1
        assert [item["role"] for item in payload["messages"]] == ["system", "user"]
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer offline-test-key"


@pytest.mark.parametrize("provider_type", ["ollama", "openai_compatible"])
@pytest.mark.parametrize("failure", ["error", "empty_error", "object_error", "empty", "incomplete", "refusal", "missing_end",
                                     "invalid_shape", "invalid_json", "redirect", "http", "timeout"])
def test_connection_probe_rejects_same_transport_failures_as_inference(monkeypatch, provider_type, failure):
    body = complete(provider_type)
    status = 200
    expected = "invalid_response"
    if failure in {"error", "empty_error", "object_error"}:
        body["error"] = {"error": "synthetic model unavailable", "empty_error": "", "object_error": {}}[failure]
    elif failure == "empty":
        if provider_type == "ollama":
            body["response"] = " "
        else:
            body["choices"][0]["message"]["content"] = " "
        expected = "empty_response"
    elif failure in {"incomplete", "missing_end", "refusal"}:
        if provider_type == "ollama":
            body["done_reason"] = "length" if failure == "incomplete" else "stop"
            if failure == "missing_end":
                body.pop("done")
            if failure == "refusal":
                body["refusal"] = "refused"
        else:
            body["choices"][0]["finish_reason"] = "length" if failure == "incomplete" else None
            if failure == "refusal":
                body["choices"][0]["message"]["refusal"] = "refused"
        expected = "model_refusal" if failure == "refusal" else "generation_incomplete"
    elif failure == "invalid_shape":
        body = []
    elif failure == "redirect":
        status, expected = 307, "provider_redirect_rejected"
    elif failure == "http":
        # Transport failures surface as stable codes, not raw exception text.
        status, expected = 503, "provider_http_error"
    elif failure == "timeout":
        expected = "provider_timeout"
    elif failure == "invalid_json":
        expected = "invalid_response"
    inference = response(body, status)
    if failure == "invalid_json":
        inference._content = b"{"
    elif failure == "timeout":
        inference = ai_config.requests.Timeout(expected)
    post = transport(monkeypatch, provider_type=provider_type, inference=inference)
    result = ai_config.test_ai_provider(connection_data(provider_type))
    assert set(result) == {"ok", "message", "detail"}
    assert result["ok"] is False
    assert expected in result["detail"]
    assert post.call_count == (2 if provider_type == "ollama" else 1)
    with pytest.raises((ai_config.AIProviderResponseError, ai_config.requests.RequestException, ValueError)) as error:
        ai_config.call_ai_provider(config(provider_type=provider_type), "probe")
    assert expected in str(error.value)


@pytest.mark.parametrize("capacity", [None, 1024])
def test_connection_probe_also_requires_ollama_capacity(monkeypatch, capacity):
    post = transport(monkeypatch, capacity=capacity)
    result = ai_config.test_ai_provider(connection_data("ollama"))
    assert result["ok"] is False
    assert result["detail"] == ("context_unavailable" if capacity is None else "context_exceeded")
    assert post.call_count == 1
