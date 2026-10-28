"""Governance over real AuthMiddleware/JWT and persisted, separate database sessions."""
import pytest

from app.models.access_control import SecurityAuditEvent, UserAccessRole
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.capability_plan import DBCapabilityPlanRevision
from test_assistant_api import api as base_api, signing_key_path, create, payload, assert_rejected  # noqa: F401


@pytest.fixture
def api(base_api):
    for model in (DBCapabilityBatch, DBCapabilityExecution):
        model.__table__.create(base_api.engine, checkfirst=True)
    return base_api


def requested(api, *, user=2, **params):
    saved = create(api, user=user, request_approval=True, **params)
    response = api.request("POST", f"/plans/{saved['plan_id']}/approval/request", user=user,
                           json={"expected_revision": 1})
    assert response.status_code == 200, response.text
    return saved


def approval_payload(saved, **changes):
    return {"expected_revision": saved["revision"], "expected_binding_hash": saved["binding_hash"], **changes}


def test_actual_http_approval_then_explicit_execution_and_fresh_readback(api):
    saved = requested(api)
    path = f"/plans/{saved['plan_id']}"
    assert_rejected(api.request("POST", path + "/execute", json={}), 409, "approval_required")
    approved = api.request("POST", path + "/approval/approve", user=1, json=approval_payload(saved))
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_state"] == "approved"
    assert api.request("GET", path + "/validation").json()["can_execute"] is True
    executed = api.request("POST", path + "/execute", json={})
    assert executed.status_code == 200 and executed.json()["outcome"] == "succeeded"
    assert api.request("GET", path).json()["result_ref"] == executed.json()["result_ref"]


def test_approver_can_read_specific_plan_without_becoming_owner(api):
    saved = requested(api, instance_id=10)
    with api.factory() as db:
        db.add(UserAccessRole(user_id=4, role_id=api.roles["approver"]))
        db.commit()
    path = f"/plans/{saved['plan_id']}"
    approved = api.request("POST", path + "/approval/approve", user=4, json=approval_payload(saved))
    assert approved.status_code == 200, approved.text
    history = api.request("GET", path + "/approval", user=4)
    assert history.status_code == 200 and history.json()["decisions"][0]["actor_id"] == 4
    assert_rejected(api.request("POST", path + "/execute", user=4, json={}), 404, "plan_not_found")
    assert_rejected(api.request("GET", path, user=3), 404, "plan_not_found")


def test_scoped_approver_cannot_approve_foreign_target(api):
    saved = requested(api, instance_id=20)
    with api.factory() as db:
        db.add(UserAccessRole(user_id=4, role_id=api.roles["approver"]))
        db.commit()
    assert_rejected(api.request("POST", f"/plans/{saved['plan_id']}/approval/approve", user=4,
                                json=approval_payload(saved)), 403)


@pytest.mark.parametrize("suffix,body", [
    ("approval/request", {"expected_revision": 1}),
    ("approval/approve", {"expected_revision": 1, "expected_binding_hash": "0" * 64}),
    ("approval/revoke", {"expected_revision": 1}),
    ("revisions", {"expected_revision": 1, **payload()}),
])
def test_new_mutations_still_require_a_real_token_and_reject_identity_injection(api, suffix, body):
    saved = create(api)
    path = f"/plans/{saved['plan_id']}/{suffix}"
    assert_rejected(api.request("POST", path, user=None, json=body), 401)
    assert_rejected(api.request("POST", path, user=1, json={**body, "approved_by": 1}), 400, "invalid_plan_request")
    assert_rejected(api.request("POST", path, user=1, content='{"expected_revision":1,"expected_revision":2}',
                                headers={"Content-Type": "application/json"}), 400, "invalid_json")
    oversized = '{"padding":"' + "a" * 32768 + '"}'
    assert_rejected(api.request("POST", path, user=1, content=oversized,
                                headers={"Content-Type": "application/json"}), 413, "request_too_large")


def test_real_http_revision_invalidates_old_binding_and_preserves_old_revision(api):
    saved = requested(api, instance_id=10)
    path = f"/plans/{saved['plan_id']}"
    response = api.request("POST", path + "/revisions", json={
        "expected_revision": 1, **payload(), "instance_id": 10,
        "params": {"page_size": 13}, "request_approval": True,
    })
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["revision"] == 2 and revised["approval_state"] == "pending"
    assert_rejected(api.request("POST", path + "/approval/approve", user=1,
                                json=approval_payload(saved)), 409, "plan_revision_conflict")
    with api.factory() as db:
        assert db.query(DBCapabilityPlanRevision).filter_by(plan_id=saved["plan_id"]).count() == 2
        assert db.query(SecurityAuditEvent).filter_by(action="revise_plan", target_id=saved["plan_id"]).count() == 1


def test_http_requester_cannot_self_approve_even_as_administrator(api):
    saved = requested(api, user=1)
    assert_rejected(api.request("POST", f"/plans/{saved['plan_id']}/approval/approve", user=1,
                                json=approval_payload(saved)), 409, "self_approval_forbidden")
