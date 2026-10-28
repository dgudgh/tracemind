from __future__ import annotations

import multiprocessing as mp
import os
import time

import pytest

from app.runtime.engine_host.store import FixtureStore, Package, StoreError, SyntheticEffectLedger
from app.runtime.engine_host.update import FixtureUpdater


def _prepare_worker(path, key, digest, queue):
    store = FixtureStore(path)
    try:
        row = store.start_update(engine_id="test.fake", digest=digest,
                                 expected_generation=1, idempotency_key=key)
        queue.put(("ok", row["operation_id"], row["expected_fence"]))
    except StoreError as exc:
        queue.put(("error", exc.code, None))
    finally:
        store.close()


def _activate_worker(path, operation_id, queue):
    store = FixtureStore(path)
    try:
        row = store.activate(operation_id)
        queue.put(("ok", row["target_generation"], row["digest"]))
    except StoreError as exc:
        queue.put(("error", exc.code, None))
    finally:
        store.close()


def _ready(store, operation_id):
    previous = "prepared"
    for phase in ("verified", "staged", "candidate_started", "handshake_complete",
                  "selftest_complete", "candidate_ready"):
        store.transition_update(operation_id, phase, allowed_from=(previous,),
                                detail={"test": True, "phase": phase})
        previous = phase


def _initialized(tmp_path):
    store = FixtureStore(tmp_path / "state.db")
    store.bootstrap_active(Package("test.fake", "v1", "1.0.0", state="active",
                                   payload_digest="p1", runtime_digest="r1", sdk_version="s1"))
    store.add_package(Package("test.fake", "v2", "2.0.0", state="candidate",
                              payload_digest="p2", runtime_digest="r2", sdk_version="s1"))
    store.add_package(Package("test.fake", "v3", "3.0.0", state="candidate",
                              payload_digest="p3", runtime_digest="r3", sdk_version="s1"))
    return store


def test_two_process_same_request_replays_one_operation(tmp_path):
    store = _initialized(tmp_path)
    store.close()
    context = mp.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_prepare_worker,
                               args=(tmp_path / "state.db", "same", "v2", queue))
               for _ in range(2)]
    for process in workers:
        process.start()
    values = [queue.get(timeout=10) for _ in workers]
    for process in workers:
        process.join(10)
        assert process.exitcode == 0
    assert {value[0] for value in values} == {"ok"}
    assert len({value[1] for value in values}) == 1
    reopened = FixtureStore(tmp_path / "state.db")
    assert reopened.counts()["updates"] == 1


def test_two_process_different_updates_have_one_lease_winner(tmp_path):
    store = _initialized(tmp_path)
    store.close()
    context = mp.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_prepare_worker,
                               args=(tmp_path / "state.db", key, digest, queue))
               for key, digest in (("a", "v2"), ("b", "v3"))]
    for process in workers:
        process.start()
    values = [queue.get(timeout=10) for _ in workers]
    for process in workers:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(value[0] for value in values) == ["error", "ok"]
    assert next(value[1] for value in values if value[0] == "error") == "lease_busy"


def test_prepare_cannot_activate_before_candidate_ready(tmp_path):
    store = _initialized(tmp_path)
    operation = store.start_update(engine_id="test.fake", digest="v2",
                                   expected_generation=1, idempotency_key="not-ready")
    with pytest.raises(StoreError, match="operation_not_ready"):
        store.activate(operation["operation_id"])
    assert store.active("test.fake").digest == "v1"


def test_expired_lease_new_fence_blocks_old_cas(tmp_path):
    store = _initialized(tmp_path)
    first = store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                               idempotency_key="old", lease_seconds=0.01)
    _ready(store, first["operation_id"])
    time.sleep(0.03)
    second = store.start_update(engine_id="test.fake", digest="v3", expected_generation=1,
                                idempotency_key="new")
    _ready(store, second["operation_id"])
    assert second["expected_fence"] > first["expected_fence"]
    with pytest.raises(StoreError, match="stale_fence"):
        store.activate(first["operation_id"])
    activated = store.activate(second["operation_id"])
    assert activated["target_generation"] == 2
    assert store.active("test.fake").digest == "v3"


def test_activation_replay_returns_original_receipt_after_later_rollback(tmp_path):
    store = _initialized(tmp_path)
    update = store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                                idempotency_key="update")
    _ready(store, update["operation_id"])
    activated = store.activate(update["operation_id"])
    rollback = store.prepare_rollback("test.fake", "v1", 2, idempotency_key="rollback")
    _ready(store, rollback["operation_id"])
    rollback = store.activate(rollback["operation_id"])
    assert rollback["target_generation"] == 3
    replay = store.activate(update["operation_id"])
    assert replay["target_generation"] == 2
    assert replay["digest"] == "v2"
    assert store.active("test.fake").generation == 3


def test_four_old_task_classes_keep_exact_identity_and_core_result(tmp_path):
    store = _initialized(tmp_path)
    for status in ("queued", "running", "retry", "reconcile_required"):
        store.prepare_task(task_id=f"old-{status}", engine_id="test.fake",
                           payload={"status": status}, status=status,
                           effect_state="unknown" if status == "reconcile_required" else "none")
    update = store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                                idempotency_key="switch")
    _ready(store, update["operation_id"])
    store.activate(update["operation_id"])
    new = store.prepare_task(task_id="new", engine_id="test.fake", payload={"new": True})
    assert new["generation"] == 2 and new["digest"] == "v2"
    for status in ("queued", "running", "retry", "reconcile_required"):
        old = store.task(f"old-{status}")
        assert old["generation"] == 1 and old["digest"] == "v1"
        assert old["payload_digest"] == "p1" and old["runtime_digest"] == "r1"
    store.set_task_result("old-queued", status="succeeded", effect_state="none",
                          result={"schema": "engine.result/v1", "value": 7})
    result = store.result("old-queued")
    assert result["digest"] == "v1"
    assert '"value":7' in result["result_json"]
    assert store.task("old-queued")["input_json"] == '{"status":"queued"}'


def test_idempotency_conflict_and_failed_prepare_cleanup(tmp_path):
    store = _initialized(tmp_path)
    updater = FixtureUpdater(store)
    updater.prepare(engine_id="test.fake", digest="v2", expected_generation=1,
                    idempotency_key="same", version="2.0.0",
                    payload_digest="p2", runtime_digest="r2", sdk_version="s1")
    with pytest.raises(StoreError, match="package_identity_conflict|idempotency_conflict"):
        updater.prepare(engine_id="test.fake", digest="v3", expected_generation=1,
                        idempotency_key="same", version="3.0.0")
    assert store.package("test.fake", "v3") is not None  # it pre-existed and was not overwritten
    with pytest.raises(StoreError, match="stale_generation"):
        updater.prepare(engine_id="test.fake", digest="unused", expected_generation=99,
                        idempotency_key="stale", version="9.0.0")
    assert store.package("test.fake", "unused") is None


@pytest.mark.parametrize("changed", [
    {"operation_id": "effect-b"},
    {"authorization": {"scope": "b"}},
    {"attempt": 2},
    {"deadline": "4102444800"},
    {"generation": 1, "digest": "v1"},
    {"identity": {"sdk_version": "different"}},
    {"status": "retry"},
    {"effect_state": "unknown"},
])
def test_task_idempotency_binds_full_request_semantics(tmp_path, changed):
    store = _initialized(tmp_path)
    base = {"task_id": "fixed", "engine_id": "test.fake", "payload": {"value": 1},
            "operation_id": "effect-a", "authorization": {"scope": "a"},
            "attempt": 1, "status": "queued", "effect_state": "none"}
    store.prepare_task(**base)
    with pytest.raises(StoreError, match="task_idempotency_conflict"):
        store.prepare_task(**{**base, **changed})


def test_default_route_task_replay_does_not_rebind_or_false_conflict(tmp_path):
    store = _initialized(tmp_path)
    request = {"task_id": "default-route", "engine_id": "test.fake",
               "payload": {"value": 1}, "operation_id": "effect-a"}
    first = store.prepare_task(**request)
    update = store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                                idempotency_key="route-change")
    _ready(store, update["operation_id"])
    store.activate(update["operation_id"])
    replay = store.prepare_task(**request)
    assert replay["generation"] == first["generation"] == 1
    assert replay["digest"] == first["digest"] == "v1"


def test_terminal_receipt_effect_state_is_immutable_and_atomic(tmp_path):
    store = _initialized(tmp_path)
    store.prepare_task(task_id="terminal", engine_id="test.fake", payload={"value": 1})
    store.set_task_result("terminal", status="succeeded", effect_state="completed",
                          result={"value": 1})
    with pytest.raises(StoreError, match="result_conflict"):
        store.set_task_result("terminal", status="succeeded", effect_state="unknown",
                              result={"value": 1})
    assert store.task("terminal")["effect_state"] == "completed"
    assert store.result("terminal")["effect_state"] == "completed"
    with pytest.raises(StoreError, match="terminal_result_immutable"):
        store.set_task_status("terminal", status="retry", effect_state="unknown")


def test_reconcile_explicitly_allows_only_unknown_to_completed_transition(tmp_path):
    store = _initialized(tmp_path)
    store.prepare_task(task_id="reconcile", engine_id="test.fake", payload={"value": 1},
                       status="reconcile_required", effect_state="unknown")
    store.set_task_result("reconcile", status="reconcile_required", effect_state="unknown",
                          result={})
    store.set_task_result("reconcile", status="succeeded", effect_state="completed",
                          result={"value": 1}, reconcile=True)
    assert store.task("reconcile")["effect_state"] == "completed"
    assert store.result("reconcile")["effect_state"] == "completed"


def test_launch_persists_and_refreshes_exact_owned_members(tmp_path):
    store = _initialized(tmp_path)
    pid, pgid = os.getpid(), os.getpgid(os.getpid())
    record = {
        "pid": pid, "start_time": 123.5, "pgid": pgid, "uid": os.getuid(),
        "launch_instance_id": "launch-1", "socket_path": str(tmp_path / "ipc" / "x.sock"),
        "ipc_root": str(tmp_path / "ipc"), "staging_dir": str(tmp_path / "stage"),
        "workdir": str(tmp_path / "host"), "manifest_sha256": "a" * 64,
        "provider_version": "1.0.0", "owner_token": "token",
        "members": [{"pid": pid, "start_time": 123.5, "uid": os.getuid(), "pgid": pgid}],
    }
    saved = store.save_launch(engine_id="test.fake", generation=1,
                              package_digest="v1", record=record)
    assert saved["members_json"] == ('[{"pgid":%d,"pid":%d,"start_time":123.5,"uid":%d}]'
                                      % (pgid, pid, os.getuid()))
    store.update_launch_members("launch-1", [
        {"pid": pid + 1, "start_time": 124.5, "uid": os.getuid(), "pgid": pgid}])
    assert f'"pid":{pid + 1}' in store.launch("launch-1")["members_json"]


def test_failed_transaction_rolls_back_fence_and_lease(tmp_path):
    store = _initialized(tmp_path)
    store.conn.execute(
        "CREATE TRIGGER fail_update BEFORE INSERT ON updates BEGIN SELECT RAISE(ABORT,'fault'); END"
    )
    before = store.route("test.fake")["fence"]
    with pytest.raises(Exception, match="fault"):
        store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                           idempotency_key="fault")
    assert store.route("test.fake")["fence"] == before
    assert store.conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0


def test_revocation_and_references_block_unsafe_reuse_or_collection(tmp_path):
    store = _initialized(tmp_path)
    store.prepare_task(task_id="old", engine_id="test.fake", payload={"read": True})
    assert store.can_collect_package("test.fake", "v1") is False
    store.quarantine_package("test.fake", "v2", revoked=True)
    with pytest.raises(StoreError, match="package_untrusted"):
        store.start_update(engine_id="test.fake", digest="v2", expected_generation=1,
                           idempotency_key="revoked")
    store.release_task_reference("old")
    # Active and last-known-good are protected even after task references clear.
    assert store.can_collect_package("test.fake", "v1") is False


def test_synthetic_effect_ledger_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "effects.db"
    ledger = SyntheticEffectLedger(path)
    ledger.execute("effect-1", {"amount": 1})
    ledger.execute("effect-1", {"amount": 1})
    assert ledger.count("effect-1") == 1
    ledger.close()
    reopened = SyntheticEffectLedger(path)
    assert reopened.verify("effect-1", {"amount": 1})["verified"] is True
    with pytest.raises(StoreError, match="effect_idempotency_conflict"):
        reopened.execute("effect-1", {"amount": 2})


def test_core_result_readback_does_not_need_provider_source(tmp_path):
    store = _initialized(tmp_path)
    provider = tmp_path / "provider.py"
    provider.write_text("raise RuntimeError('must not load')\n")
    store.prepare_task(task_id="historical", engine_id="test.fake", payload={"read": True})
    store.set_task_result("historical", status="succeeded", effect_state="none",
                          result={"rows": [{"id": 1}], "marker": "v1"})
    provider.unlink()
    store.close()
    reopened = FixtureStore(tmp_path / "state.db")
    assert reopened.read_result("historical") == {"rows": [{"id": 1}], "marker": "v1"}
