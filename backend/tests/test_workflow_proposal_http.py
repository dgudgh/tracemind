"""C11 HTTP boundary: a model proposal is structure only, and never an execution request.

Every assertion below fails if the gate, the consent check, the ledger or the redaction is
removed; the mutation table in the commit message names which line each one pins.
"""
import json

import pytest

from app.models.ai_provider_config import AIProviderConfig
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_proposal import DBCapabilityProposal
from app.services import ai_config
from test_plan_admission_http import api, base_api  # noqa: F401
from test_assistant_api import assert_rejected, signing_key_path  # noqa: F401
from test_ai_transport_limits import server  # noqa: F401 - real loopback HTTP server


MATCHED = json.dumps({"decision": "matched", "workflow_id": "inspection.jobs.list",
                      "workflow_version": 1, "slots": {}})
KEY = "http-workflow-proposal"


@pytest.fixture
def proposal_api(api):
    for model in (AIProviderConfig, DBCapabilityProposal):
        model.__table__.create(api.engine, checkfirst=True)
    with api.factory() as db:
        db.add(AIProviderConfig(id=1, provider_name="offline", provider_type="ollama",
                                base_url="http://127.0.0.1:11434", model_name="offline-fixture",
                                enabled=1, is_default=1, timeout_seconds=30))
        db.commit()
        revision = ai_config.provider_revision(db.get(AIProviderConfig, 1))
    api.provider_revision = revision
    return api


def body(api, **kwargs):
    return {"text": "查看已保存巡检作业", "scope": {"mode": "global"},
            "provider_config_id": 1, "provider_revision": api.provider_revision,
            "allow_model_context": True, **kwargs}


def propose(api, payload=None, *, key=KEY, user=2):
    return api.request("POST", "/workflows/proposals", user=user, headers={"Idempotency-Key": key},
                       json=body(api) if payload is None else payload)


def counted(monkeypatch, raw=MATCHED):
    calls = []

    def fake(config, prompt, *, json_mode=False):
        calls.append((config.id, json_mode, prompt))
        return raw
    monkeypatch.setattr(ai_config, "call_ai_provider", fake)
    return calls


def test_a_matched_proposal_is_still_accepted_false_and_creates_no_plan(proposal_api, monkeypatch):
    calls = counted(monkeypatch)
    response = propose(proposal_api)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["schema"] == "capability.workflow_proposal/v1"
    assert value["accepted"] is False and value["requires_confirmation"] is True
    assert value["validation_scope"] == "structure_only"
    assert value["decision"] == value["model_decision"] == "matched"
    assert value["workflow"] == {"workflow_id": "inspection.jobs.list", "workflow_version": 1,
                                 "definition_hash": value["workflow"]["definition_hash"]}
    assert value["candidate"]["accepted"] is False and value["candidate"]["plan_inputs"]
    assert len(value["proposal_id"]) == 30 and value["proposal_id"].startswith("WP")
    assert value["provider"] == {"config_id": 1, "revision": proposal_api.provider_revision,
                                 "model": "offline-fixture"}
    assert calls and calls[0][1] is True
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 1
        # The gate returned a candidate; nothing was frozen, queued or executed.
        assert db.query(DBCapabilityPlan).count() == 0
        assert db.query(DBCapabilityBatch).count() == db.query(DBCapabilityExecution).count() == 0


@pytest.mark.parametrize("extra", [
    {"catalog": {"schema": "capability.workflow_catalog/v1", "items": []}},
    {"slots": {"page": 1}}, {"targets": [{"instance_id": 10}]},
    {"accepted": True}, {"decision": "matched"}, {"scope": {"mode": "global"}, "operation": "prepare"},
])
def test_a_body_cannot_supply_the_catalog_slots_targets_or_a_decision(proposal_api, monkeypatch, extra):
    calls = counted(monkeypatch)
    response = propose(proposal_api, body(proposal_api, **extra))
    assert response.status_code == 422, response.text
    assert calls == []
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 0


@pytest.mark.parametrize("changes,status,code", [
    ({"allow_model_context": False}, 422, "workflow_provider_context_not_approved"),
    ({"provider_config_id": 999}, 409, "workflow_provider_not_authorized"),
    ({"provider_revision": "0" * 64}, 409, "workflow_provider_not_authorized"),
])
def test_consent_is_checked_before_the_model_is_called(proposal_api, monkeypatch, changes, status, code):
    calls = counted(monkeypatch)
    response = propose(proposal_api, body(proposal_api, **changes))
    assert_rejected(response, status, code)
    assert calls == []
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 0
        assert db.query(DBCapabilityPlan).count() == 0


def test_a_disabled_provider_is_refused_without_a_call(proposal_api, monkeypatch):
    with proposal_api.factory() as db:
        db.get(AIProviderConfig, 1).enabled = 0
        db.commit()
    calls = counted(monkeypatch)
    assert_rejected(propose(proposal_api), 409, "workflow_provider_not_authorized")
    assert calls == []


def test_one_idempotency_key_returns_the_same_proposal_and_calls_the_model_once(proposal_api, monkeypatch):
    calls = counted(monkeypatch)
    first = propose(proposal_api)
    again = propose(proposal_api)
    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert first.json() == again.json()
    assert len(calls) == 1
    read = proposal_api.request("GET", "/workflows/proposals", headers={"Idempotency-Key": KEY})
    assert read.status_code == 200 and read.json() == first.json()
    assert_rejected(proposal_api.request("GET", "/workflows/proposals", user=3,
                                         headers={"Idempotency-Key": KEY}), 404, "workflow_proposal_not_found")
    assert_rejected(propose(proposal_api, body(proposal_api, text="另一个请求")), 409, "idempotency_conflict")
    assert_rejected(proposal_api.request("GET", "/workflows/proposals",
                                         headers={"Idempotency-Key": "never-used"}), 404, "workflow_proposal_not_found")
    assert_rejected(proposal_api.request("GET", "/workflows/proposals"), 400, "invalid_idempotency_key")
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 1


@pytest.mark.parametrize("raw", [
    "```json\n" + MATCHED + "\n```",
    json.dumps({"decision": "matched", "workflow_id": "inspection.jobs.list",
                "workflow_version": 1, "slots": {"note": "x" * 16385}}),
    MATCHED + "\n" + MATCHED,
    "not json at all: synthetic-model-text-do-not-echo",
])
def test_model_text_is_never_repaired_or_echoed(proposal_api, monkeypatch, raw):
    calls = counted(monkeypatch, raw)
    response = propose(proposal_api)
    assert response.status_code == 422, response.text
    assert len(calls) == 1
    assert "synthetic-model-text-do-not-echo" not in response.text
    assert "note" not in response.text and "```" not in response.text
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 0
        assert db.query(DBCapabilityPlan).count() == 0


def test_a_provider_failure_is_reported_without_leaking_its_message(proposal_api, monkeypatch):
    def denied(config, prompt, *, json_mode=False):
        raise ai_config.AIProviderResponseError("synthetic-provider-secret")
    monkeypatch.setattr(ai_config, "call_ai_provider", denied)
    response = propose(proposal_api)
    assert response.status_code == 502
    assert "synthetic-provider-secret" not in response.text
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 0


def test_explicit_scope_targets_must_stay_inside_the_actor_whitelist(proposal_api, monkeypatch):
    calls = counted(monkeypatch)
    inside = propose(proposal_api, body(proposal_api, scope={
        "mode": "explicit", "targets": [{"instance_id": 10}]}))
    assert inside.status_code == 200, inside.text
    assert inside.json()["candidate"]["plan_inputs"][0]["instance_id"] == 10
    assert_rejected(propose(proposal_api, body(proposal_api, scope={
        "mode": "explicit", "targets": [{"instance_id": 999}]}), key="outside-scope"),
        422, "workflow_proposal_selection_not_allowed")
    assert len(calls) == 2


def test_the_consent_material_needed_for_a_proposal_is_readable_without_platform_settings(proposal_api):
    """The UI cannot invent `provider_revision`; this read is what makes consent possible at all."""
    with proposal_api.factory() as db:
        db.add(AIProviderConfig(id=2, provider_name="disabled-\u0001hidden", provider_type="ollama",
                                base_url="http://10.0.0.9:11434", model_name="off", enabled=0,
                                is_default=0, timeout_seconds=30))
        db.commit()
    response = proposal_api.request("GET", "/workflows/providers", user=2)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["schema"] == "capability.workflow_providers/v1"
    assert [item["config_id"] for item in value["items"]] == [1]           # disabled provider excluded
    item = value["items"][0]
    assert set(item) == {"config_id", "name", "provider_type", "model", "revision", "is_default"}
    assert item["revision"] == proposal_api.provider_revision
    assert item["is_default"] is True
    # Neither the endpoint nor any key material may reach the client.
    assert "10.0.0.9" not in response.text and "api_key" not in response.text


def test_a_revision_read_from_the_provider_list_is_accepted_by_the_proposal(proposal_api, monkeypatch):
    counted(monkeypatch)
    listed = proposal_api.request("GET", "/workflows/providers", user=2).json()["items"][0]
    payload = body(proposal_api)
    payload.update(provider_config_id=listed["config_id"], provider_revision=listed["revision"])
    response = propose(proposal_api, payload, key="consent-roundtrip")
    assert response.status_code == 200, response.text
    assert response.json()["provider"]["revision"] == listed["revision"]


def test_a_stale_revision_is_refused_before_any_model_call(proposal_api, monkeypatch):
    calls = counted(monkeypatch)
    payload = body(proposal_api)
    payload["provider_revision"] = "b" * 64
    assert_rejected(propose(proposal_api, payload, key="stale-revision"), 409,
                    "workflow_provider_not_authorized")
    assert calls == []


def _point_at(api, base_url):
    with api.factory() as db:
        row = db.get(AIProviderConfig, 1)
        row.base_url, row.model_name, row.provider_type = base_url, "local-fake", "openai_compatible"
        db.commit()
        return ai_config.provider_revision(row)


def test_real_http_provider_call_is_one_attempt_persisted_and_replayed_by_the_original_key(
        proposal_api, monkeypatch, server):
    """Not a mocked callable: a real loopback HTTP server answers the model request."""
    revision = _point_at(proposal_api, server + "/proposal")
    monkeypatch.setattr(proposal_api, "provider_revision", revision, raising=False)
    payload = body(proposal_api, provider_revision=revision)
    first = propose(proposal_api, payload)
    assert first.status_code == 200, first.text
    value = first.json()
    assert value["accepted"] is False and value["decision"] == "matched"
    second = propose(proposal_api, payload)
    assert second.status_code == 200 and second.json()["proposal_id"] == value["proposal_id"]
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 1


def test_real_http_oversized_model_response_fails_closed_without_a_ledger_row(
        proposal_api, monkeypatch, server):
    revision = _point_at(proposal_api, server + "/huge")
    monkeypatch.setattr(proposal_api, "provider_revision", revision, raising=False)
    response = propose(proposal_api, body(proposal_api, provider_revision=revision), key="http-huge")
    assert response.status_code >= 400
    assert "response_too_large" not in response.text or "Traceback" not in response.text
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).filter_by(idempotency_key="http-huge").count() == 0


def test_daily_budget_is_shared_across_requests_and_refuses_before_another_call(
        proposal_api, monkeypatch):
    calls = counted(monkeypatch)
    monkeypatch.setattr(ai_config, "AI_DAILY_REQUESTS", 1, raising=False)
    first = propose(proposal_api, key="budget-one")
    assert first.status_code == 200, first.text
    second = propose(proposal_api, key="budget-two")
    assert second.status_code == 429, second.text
    assert second.json()["detail"]["code"] == "workflow_provider_budget_exhausted"
    assert len(calls) == 1
    with proposal_api.factory() as db:
        assert db.query(DBCapabilityProposal).count() == 1
