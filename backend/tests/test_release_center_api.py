"""The release transaction manager must be observable, not just implemented.

`release_manager` could install, back up, activate and roll back, but no endpoint
exposed any of it - an operator could not tell which release was live, nor that a
half-finished transaction was silently blocking the next upgrade.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import release_center
from app.runtime import release_manager


@pytest.fixture
def install_root(tmp_path):
    root = tmp_path / "app"
    (root / "releases" / "2026.10.1").mkdir(parents=True)
    (root / "releases" / "2026.09.0").mkdir(parents=True)
    (root / "active-release.json").write_text(
        json.dumps({"release_id": "2026.10.1", "previous_release_id": "2026.09.0"}),
        encoding="utf-8",
    )
    (root / "release-state.json").write_text(
        json.dumps({"format_version": 1, "history": [{"id": "tx-1", "status": "finished"}]}),
        encoding="utf-8",
    )
    (root / "releases" / "2026.10.1" / release_manager.MANIFEST_FILENAME).write_text(
        json.dumps({"release_id": "2026.10.1", "version": "2026.10.1"}), encoding="utf-8"
    )
    return root


@pytest.fixture
def client(monkeypatch, install_root):
    monkeypatch.setenv("TRACEMIND_INSTALL_ROOT", str(install_root))
    app = FastAPI()
    app.include_router(release_center.router, prefix="/api/v1")
    return TestClient(app)


def test_status_reports_active_release_and_history(client):
    body = client.get("/api/v1/release-center/status").json()
    assert body["available"] is True
    assert body["active"]["release_id"] == "2026.10.1"
    assert body["releases"] == ["2026.09.0", "2026.10.1"]
    assert body["history"][-1]["id"] == "tx-1"
    assert body["blocked"] is False
    assert body["mutation_channel"] == "operator_cli"


def test_unfinished_transaction_is_reported_as_blocking(client, install_root):
    """A dead transaction blocks every later mutation; silence would be a trap."""
    (install_root / "release-transactions.json").write_text(
        json.dumps({
            "format_version": 1,
            "transactions": [{
                "id": "tx-dead", "terminal": False, "facts": {},
                "events": [{"phase": "activate", "status": "started"}],
            }],
        }),
        encoding="utf-8",
    )
    body = client.get("/api/v1/release-center/status").json()
    assert body["blocked"] is True
    assert body["pending_transaction"]["id"] == "tx-dead"


def test_non_versioned_install_says_so_instead_of_inventing_history(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACEMIND_INSTALL_ROOT", str(tmp_path / "checkout"))
    app = FastAPI()
    app.include_router(release_center.router, prefix="/api/v1")
    body = TestClient(app).get("/api/v1/release-center/status").json()
    assert body["available"] is False
    assert body["reason"] == "not_a_versioned_install"


def test_manifest_endpoint_refuses_traversal(client):
    assert client.get("/api/v1/release-center/releases/..%2F..%2Fetc/manifest").status_code in {404, 400}
    assert client.get("/api/v1/release-center/releases/nope/manifest").status_code == 404
