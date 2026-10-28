from __future__ import annotations

import pytest

from app.runtime.engine_host.store import FixtureStore, Package, StoreError
from app.runtime.engine_host.update import FixtureUpdater, UpdateError


def _store(tmp_path):
    return FixtureStore(tmp_path / "state.db")


def _ready(updater, operation_id):
    updater.verified(operation_id, {"verified": True})
    updater.staged(operation_id, {"staging_dir": "/fixture"})
    updater.candidate_started(operation_id, {"pid": 1})
    updater.handshake_complete(operation_id, {"ok": True})
    updater.selftest_complete(operation_id, {"ok": True})
    updater.candidate_ready(operation_id, capacity_receipt={"admitted": True})


def test_generation_cas_and_idempotent_update(tmp_path):
    store = _store(tmp_path)
    store.add_package(Package("test.fake", "d1", "1", 1, "active"))
    store.conn.execute("INSERT INTO active VALUES ('test.fake',1,'d1')")
    updater = FixtureUpdater(store)
    first = updater.prepare(engine_id="test.fake", digest="d2", expected_generation=1,
                            idempotency_key="k1", generation=2)
    again = updater.prepare(engine_id="test.fake", digest="d2", expected_generation=1,
                            idempotency_key="k1", generation=2)
    assert first.operation_id == again.operation_id
    with pytest.raises(StoreError, match="operation_not_ready"):
        updater.activate(first.operation_id)
    _ready(updater, first.operation_id)
    active = updater.activate(first.operation_id)
    assert active.generation == 2 and active.digest == "d2"
    with pytest.raises(StoreError) as error:
        updater.prepare(engine_id="test.fake", digest="d3", expected_generation=1,
                        idempotency_key="k2", generation=3)
    assert error.value.code == "stale_generation"


def test_rollback_requires_last_known_good(tmp_path):
    store = _store(tmp_path)
    store.add_package(Package("test.fake", "d0", "0", 0, "last_known_good"))
    store.add_package(Package("test.fake", "d1", "1", 1, "active"))
    store.conn.execute("INSERT INTO active VALUES ('test.fake',1,'d1')")
    updater = FixtureUpdater(store)
    result = updater.prepare_rollback(engine_id="test.fake", digest="d0", expected_generation=1)
    assert result.state == "prepared"
    with pytest.raises(StoreError, match="operation_not_ready"):
        updater.activate(result.operation_id)
    _ready(updater, result.operation_id)
    result = updater.activate(result.operation_id)
    assert result.generation == 2
    assert store.active("test.fake").generation == 2
    with pytest.raises(StoreError):
        updater.prepare_rollback(engine_id="test.fake", digest="missing", expected_generation=2)
