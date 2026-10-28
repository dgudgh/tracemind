"""Recovery reads use persisted request identity, never retry a lost execution response."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, MetaData, String
from sqlalchemy.exc import OperationalError

from app.models.capability_plan import DBCapabilityPlan
from app.models.capability_preparation import DBCapabilityPreparation
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.models.access_control import UserAssetScope
from app.services import plan_queue
from app.migrations.versions import v20261020_06_capability_preparation as migration
from test_plan_admission_http import api, base_api, prepare, admit  # noqa: F401
from test_assistant_api import assert_rejected, signing_key_path  # noqa: F401


def recovery(api, kind, key, *, user=2, **kwargs):
    return api.request("GET", "/batches/" + kind, user=user, headers={"Idempotency-Key": key}, **kwargs)


def test_lost_prepare_response_is_recovered_in_new_session_without_more_plans_or_execution(api):
    original = prepare(api, key="receipt-lost-prepare").json()
    with api.factory() as db:
        receipt = db.query(DBCapabilityPreparation).one()
        assert receipt.total == 2 and len(receipt.plan_bindings_json) == 2
        assert all(set(item) == {"plan_id", "instance_id", "revision", "binding_hash"}
                   for item in receipt.plan_bindings_json)
        assert db.query(DBCapabilityExecution).count() == 0
    # Original response is deliberately not needed for the recovery request.
    response = recovery(api, "preparation", "receipt-lost-prepare")
    assert response.status_code == 200, response.text
    found = response.json()
    assert found["accepted"] is False
    assert [item["plan_id"] for item in found["items"]] == [item["plan_id"] for item in original["items"]]
    assert all(item["reused"] for item in found["items"])
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityPreparation).count() == 1
        assert db.query(DBCapabilityExecution).count() == db.query(DBCapabilityBatch).count() == 0


def test_concurrent_repeated_prepare_commits_one_receipt_and_one_complete_target_set(api):
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda _: prepare(api, key="concurrent-receipt"), range(2)))
    assert all(item.status_code == 200 for item in responses), [item.text for item in responses]
    ids = [[item["plan_id"] for item in response.json()["items"]] for response in responses]
    assert ids[0] == ids[1] and len(ids[0]) == 2
    with api.factory() as db:
        assert db.query(DBCapabilityPreparation).count() == 1 and db.query(DBCapabilityPlan).count() == 2


def test_preparation_keys_remain_distinct_without_relying_on_case_sensitive_database_collation(api):
    with api.engine.begin() as connection:
        table = DBCapabilityPreparation.__table__
        table.drop(connection)
        insensitive = table.to_metadata(MetaData())
        insensitive.c.idempotency_key.type = String(128, collation="NOCASE")
        insensitive.create(connection)
    lower = prepare(api, key="prepare:abc").json()
    upper = prepare(api, key="prepare:ABC").json()
    assert [item["plan_id"] for item in lower["items"]] != [item["plan_id"] for item in upper["items"]]
    assert recovery(api, "preparation", "prepare:abc").json()["items"][0]["plan_id"] == lower["items"][0]["plan_id"]
    assert recovery(api, "preparation", "prepare:ABC").json()["items"][0]["plan_id"] == upper["items"][0]["plan_id"]
    with api.factory() as db:
        values = [row.request_key_hash for row in db.query(DBCapabilityPreparation).all()]
        assert len(set(values)) == 2 and all(value == value.lower() for value in values)


def test_admission_response_recovery_preserves_batch_identity_before_and_after_worker(api):
    prepared = prepare(api, key="receipt-prepare").json()
    accepted = admit(api, prepared, key="receipt-enqueue").json()
    found = recovery(api, "admission", "receipt-enqueue").json()
    assert found["batch_id"] == accepted["batch_id"] and found["queue_counts"] == {"queued": 2}
    assert plan_queue.process_one(api.factory, worker_id="receipt-worker")["outcome"] == "succeeded"
    assert plan_queue.process_one(api.factory, worker_id="receipt-worker")["outcome"] == "succeeded"
    done = recovery(api, "admission", "receipt-enqueue").json()
    assert done["batch_id"] == accepted["batch_id"] and done["outcome"] == "succeeded"
    with api.factory() as db:
        assert db.query(DBCapabilityBatch).count() == 1 and db.query(DBCapabilityExecution).count() == 2


@pytest.mark.parametrize("kind", ["preparation", "admission"])
def test_receipts_are_owner_only_and_missing_receipts_never_create_anything(api, kind):
    prepared = prepare(api, key="shared-recovery-key").json()
    admit(api, prepared, key="shared-recovery-key")
    expected = "preparation_not_found" if kind == "preparation" else "batch_not_found"
    for user in (1, 3):
        assert_rejected(recovery(api, kind, "shared-recovery-key", user=user), 404, expected)
    assert_rejected(recovery(api, kind, "never-committed"), 404, expected)
    assert_rejected(recovery(api, kind, "shared-recovery-key", user=None), 401)
    assert_rejected(recovery(api, kind, "shared-recovery-key", user=5), 403)
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2 and db.query(DBCapabilityPreparation).count() == 1
        assert db.query(DBCapabilityBatch).count() == 1


def test_scope_is_rechecked_for_every_plan_when_recovering_receipt(api):
    prepare(api, key="scope-receipt")
    with api.factory() as db:
        db.add(UserAssetScope(user_id=2, scope_type="instance", scope_id=10, access_level="read"))
        db.commit()
    assert_rejected(recovery(api, "preparation", "scope-receipt"), 403)


def test_revision_invalidates_preparation_receipt_without_resaving_or_retargeting_it(api):
    prepared = prepare(api, key="revision-receipt").json()
    plan = prepared["items"][0]
    response = api.request("POST", f"/plans/{plan['plan_id']}/revisions", json={
        "expected_revision": 1, "capability_id": "inspection.run.query", "definition_version": 1,
        "instance_id": 10, "params": {"page_size": 33},
    })
    assert response.status_code == 200, response.text
    assert_rejected(recovery(api, "preparation", "revision-receipt"), 409, "preparation_binding_changed")
    with api.factory() as db:
        assert db.query(DBCapabilityPlan).count() == 2
        assert db.query(DBCapabilityPreparation).one().plan_bindings_json[0]["revision"] == 1


@pytest.mark.parametrize("damage", ["shape", "duplicate", "hash", "origin", "order", "other_request"])
def test_corrupt_receipt_is_not_returned_as_a_different_authorized_request(api, damage):
    prepare(api, key="corrupt-receipt")
    if damage == "other_request":
        prepare(api, key="another-real-request")
    with api.factory() as db:
        row = db.query(DBCapabilityPreparation).filter_by(idempotency_key="corrupt-receipt").one()
        bindings = deepcopy(row.plan_bindings_json)
        if damage == "shape":
            bindings[0]["plan_id"] = []
        elif damage == "duplicate":
            bindings[1] = bindings[0]
        elif damage == "hash":
            bindings[0]["binding_hash"] = "0" * 64
        elif damage == "order":
            bindings.reverse()
        elif damage == "other_request":
            bindings = deepcopy(db.query(DBCapabilityPreparation).filter_by(
                idempotency_key="another-real-request").one().plan_bindings_json)
        else:
            row.input_hash = "0" * 64
        row.plan_bindings_json = bindings
        db.commit()
    assert_rejected(recovery(api, "preparation", "corrupt-receipt"), 409, "preparation_binding_changed")


def test_receipt_insert_failure_rolls_back_all_plans_and_never_leaves_a_partial_receipt(api):
    def fail(*_args):
        raise OperationalError("insert receipt", {}, RuntimeError("isolated storage failure"))
    event.listen(DBCapabilityPreparation, "before_insert", fail)
    try:
        assert_rejected(prepare(api, key="storage-failure"), 503, "plan_storage_unavailable")
    finally:
        event.remove(DBCapabilityPreparation, "before_insert", fail)
    assert_rejected(recovery(api, "preparation", "storage-failure"), 404, "preparation_not_found")
    with api.factory() as db:
        assert db.query(DBCapabilityPreparation).count() == db.query(DBCapabilityPlan).count() == 0


def test_receipt_migration_preserves_history_and_refuses_nonempty_downgrade(api):
    with api.engine.begin() as connection:
        migration.upgrade(connection)
        migration.upgrade(connection)
    prepare(api, key="retained-receipt")
    with api.engine.begin() as connection:
        with pytest.raises(RuntimeError, match="non-empty downgrade refused"):
            migration.downgrade(connection)
    assert recovery(api, "preparation", "retained-receipt").status_code == 200


@pytest.mark.parametrize("kind", ["preparation", "admission"])
def test_recovery_rejects_header_and_query_overrides(api, kind):
    assert_rejected(api.request("GET", "/batches/" + kind), 400, "invalid_idempotency_key")
    assert_rejected(recovery(api, kind, "key", params={"instance_id": 10}), 400, "invalid_query")
    assert_rejected(api.client.get("/api/v1/assistant/batches/" + kind,
                                  headers=[("Authorization", "Bearer " + api.tokens[2]),
                                           ("Idempotency-Key", "a"), ("Idempotency-Key", "b")]),
                    400, "invalid_idempotency_key")
