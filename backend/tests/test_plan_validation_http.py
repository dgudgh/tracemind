"""U03 review through real authentication, HTTP routes and isolated metadata."""
from copy import deepcopy

import pytest
from sqlalchemy import event

from app.models.capability_definition import DBCapabilityDefinition
from app.models.capability_plan import DBCapabilityPlan, DBCapabilityPlanRevision
from app.services import capability_registry as registry, plan_service as plans
from test_assistant_api import api, signing_key_path, create, assert_rejected  # noqa: F401


def test_http_review_is_read_only_and_execution_rechecks_later_disable(api, monkeypatch):
    saved = create(api, instance_id=10)
    writes, calls = [], []
    def record(_connection, _cursor, statement, *_rest):
        operation = statement.strip().split(None, 1)[0].upper()
        if operation in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    monkeypatch.setattr(registry, "execute_read", lambda *args, **kwargs: calls.append(kwargs))
    event.listen(api.engine, "before_cursor_execute", record)
    try:
        response = api.request("GET", "/plans/" + saved["plan_id"] + "/validation")
    finally:
        event.remove(api.engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert response.json()["can_execute"] is True and response.json()["issues"] == []
    assert response.json()["requires_recheck"] is True
    # Existing persistent authentication touches its session's last_seen_at.
    # It is not a plan/revision/approval/domain-data write by the review service.
    assert all(statement.startswith("UPDATE security_auth_session SET last_seen_at=") for statement in writes), writes
    assert calls == []
    with api.factory() as db:
        registry.set_enabled(db, "inspection.run.query", enabled=False, actor={"user_id": 1})
    denied = api.request("POST", "/plans/" + saved["plan_id"] + "/execute", json={})
    assert denied.status_code == 200 and denied.json()["outcome_kind"] == "capability_disabled"
    assert calls == []


def test_http_review_owner_and_current_asset_scope_are_not_approval(api):
    saved = create(api, instance_id=20)
    suffix = "/plans/" + saved["plan_id"] + "/validation"
    assert_rejected(api.request("GET", suffix, user=None), 401)
    assert_rejected(api.request("GET", suffix, user=3), 404, "plan_not_found")
    admin = api.request("GET", suffix, user=1)
    assert admin.status_code == 200 and admin.json()["issues"] == [{"code": "plan_owner_required"}]
    assert not admin.json()["can_execute"]
    assert_rejected(api.request("GET", suffix + "?actor=admin"), 400, "invalid_query")
    scoped = create(api, instance_id=10, user=4)
    # Scope is resolved from the current account, not from a copied owner field.
    assert_rejected(api.request("GET", "/plans/" + scoped["plan_id"] + "/validation", user=2), 404)


@pytest.mark.parametrize("corruption", ["null", "nan", "revision"])
def test_corrupt_snapshot_returns_conflict_not_http_500(api, monkeypatch, corruption):
    saved = create(api)
    with api.factory() as db:
        row = db.get(DBCapabilityPlan, saved["plan_id"])
        revision = db.query(DBCapabilityPlanRevision).filter_by(plan_id=row.plan_id).one()
        if corruption == "revision":
            revision.snapshot_json = {}
        else:
            snapshot = deepcopy(row.capability_snapshot_json)
            if corruption == "null":
                snapshot["definitions"] = None
                revision.binding_hash = plans._hash(snapshot)
            else:
                snapshot["unexpected"] = float("nan")
            row.capability_snapshot_json = snapshot
        db.commit()
    calls = []
    monkeypatch.setattr(registry, "execute_read", lambda *args, **kwargs: calls.append(kwargs))
    for suffix in ("", "/validation", "/execute"):
        response = api.request("POST" if suffix == "/execute" else "GET", "/plans/" + saved["plan_id"] + suffix,
                               **({"json": {}} if suffix == "/execute" else {}))
        assert_rejected(response, 409, "plan_snapshot_invalid")
    assert calls == []


def test_http_invalid_literal_in_later_step_creates_no_plan(api):
    response = api.request("POST", "/plans", json={
        "capability_id": "inspection.template.read", "definition_version": 1,
        "steps": [{"seq": 1, "capability_id": "inspection.template.read", "definition_version": 1},
                  {"seq": 2, "capability_id": "inspection.run.query", "definition_version": 1,
                   "params": {"outcome": "NOT_AN_OUTCOME"}}],
    })
    assert_rejected(response, 400, "invalid_params")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 0
