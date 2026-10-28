from __future__ import annotations

import pytest

from app.runtime.engine_host.store import FixtureStore, Package, StoreError


def test_disable_stops_new_admission_without_rebinding_existing_task(tmp_path):
    store = FixtureStore(tmp_path / "store.sqlite")
    try:
        store.bootstrap_active(Package("test.fake", "v1", "1.0.0", state="active"))
        first = store.prepare_task(task_id="old", engine_id="test.fake", payload={"echo":"old"})
        store.set_package_state("test.fake", "v1", "disabled")
        with pytest.raises(StoreError, match="package_disabled"):
            store.prepare_task(task_id="new", engine_id="test.fake", payload={"echo":"new"})
        assert store.prepare_task(task_id="old", engine_id="test.fake", payload={"echo":"old"}) == first
        assert store.task("new") is None
        assert store.task("old")["digest"] == "v1"
    finally:
        store.close()


@pytest.mark.parametrize("flag,code", [("revoked","package_revoked"),("quarantined","package_quarantined")])
def test_bad_package_cannot_be_selected_for_a_new_task(tmp_path, flag, code):
    store = FixtureStore(tmp_path / "store.sqlite")
    try:
        store.add_package(Package("test.fake", "bad", "1.0.0"))
        store.quarantine_package("test.fake", "bad", revoked=flag == "revoked")
        with pytest.raises(StoreError, match=code):
            store.prepare_task(task_id="new", engine_id="test.fake", payload={}, digest="bad", generation=1)
        assert store.task("new") is None
    finally:
        store.close()
