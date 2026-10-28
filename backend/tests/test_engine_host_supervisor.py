from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_host.supervisor import FixtureSupervisor
from app.runtime.engine_packs.contract import scan_payload_directory
from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes


def _package(root: Path):
    from app.runtime.engine_host.packages import build_fixture_package
    package = build_fixture_package(root)
    return package.source, package.manifest, package.signature, package.trusted_keys[0]


def test_real_subprocess_uses_private_uds(tmp_path):
    source, manifest, signature, key = _package(tmp_path)
    work = tmp_path / "work"
    supervisor = FixtureSupervisor(workdir=work)
    try:
        record = supervisor.start(source, manifest, signature, [key],
                                  expected_namespace="tmengine.test", expected_channel="test")
        assert record.pid > 0
        assert Path(record.socket_path).exists()
        assert (work / "runtime").exists()
        result = supervisor.call("invoke_read", {"echo": "hello"})
        values = result.get("values", result)
        assert values["marker"] == "v1" and values["echo"] == "hello"
        assert supervisor.call("selftest")["ok"] is True
    finally:
        supervisor.stop()
    assert not Path(record.socket_path).exists()


def test_timeout_terminates_owned_process_group(tmp_path):
    source, manifest, signature, key = _package(tmp_path)
    supervisor = FixtureSupervisor(workdir=tmp_path / "work-timeout")
    try:
        record = supervisor.start(source, manifest, signature, [key],
                                  expected_namespace="tmengine.test", expected_channel="test")
        with pytest.raises(Exception) as error:
            supervisor.call("invoke_read", {"sleep": 2}, timeout=0.2)
        assert getattr(error.value, "code", "") == "call_timeout_reconcile_required"
        assert record.pid > 0
    finally:
        supervisor.stop()


def test_binary_secret_with_newlines_is_not_truncated(tmp_path, monkeypatch):
    token = b"line1\nline2\r\n\x00\t  " + b"x" * 15
    assert len(token) == 32
    calls = {"n": 0}
    original = __import__("os").urandom

    def controlled(size):
        if size == 32:
            calls["n"] += 1
            if calls["n"] == 1:
                return token
        return original(size)

    monkeypatch.setattr("app.runtime.engine_host.supervisor.os.urandom", controlled)
    source, manifest, signature, key = _package(tmp_path)
    supervisor = FixtureSupervisor(workdir=tmp_path / "work-secret")
    try:
        supervisor.start(source, manifest, signature, [key],
                         expected_namespace="tmengine.test", expected_channel="test")
        assert supervisor.call("selftest")["ok"] is True
        assert calls["n"] >= 1
    finally:
        supervisor.stop()
