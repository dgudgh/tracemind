"""Offline, fixture-only lifecycle vectors; no production secret material."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import multiprocessing
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.engine_packs.trust import (PURPOSE, TrustError, key_id_for,
    canonical_manifest_bytes, verify_engine_manifest_signature)
from app.runtime.engine_packs.trust_metadata import TrustStore

NOW = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
NAMESPACE = "tmengine.test"


def digest(value):
    return "sha256:" + hashlib.sha256(canonical_manifest_bytes(value)).hexdigest()


def key(namespace=NAMESPACE):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return private, key_id_for(namespace, public), base64.b64encode(public).decode()


def envelope(signed, *signers):
    return {"signed": signed, "signatures": [
        {"key_id": name, "value": base64.b64encode(private.sign(canonical_manifest_bytes(signed))).decode()}
        for private, name in signers]}


def signed(role, version=1, namespace=NAMESPACE, expires=None, **extra):
    expiry = expires or NOW + timedelta(days=30)
    return {"role": role, "namespace": namespace, "version": version,
            "expires": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}


class Fixture:
    def __init__(self, namespace=NAMESPACE):
        self.namespace = namespace
        self.roots = [key(namespace) for _ in range(3)]
        self.roles = {name: key(namespace) for name in ("targets", "snapshot", "timestamp", "revocations")}
        self.release = key(namespace)
        self.root = self.make_root(1, self.roots[:2])
        self.manifest = {"namespace": namespace, "channel": "test" if namespace == NAMESPACE else "production",
                         "purpose": PURPOSE, "package_kind": "tmengine", "publisher": "acme",
                         "engine_id": "dm", "signing_key_id": self.release[1], "version": "1.0.0"}
        self.package_digest = digest(self.manifest)

    def make_root(self, version, root_signers):
        all_keys = self.roots + list(self.roles.values())
        return envelope(signed("root", version, self.namespace,
            keys={name: public for _, name, public in all_keys},
            roles={"root": {"key_ids": [x[1] for x in root_signers], "threshold": 2},
                   **{role: {"key_ids": [data[1]], "threshold": 1} for role, data in self.roles.items()}}),
            *[(x[0], x[1]) for x in root_signers])

    def bundle(self, *, version=1, revoked_keys=(), revoked_digests=(), packages=None, root_chain=()):
        release = self.release
        entries = [{"digest": self.package_digest, "signing_key_id": release[1],
                    "publisher": "acme", "engine_id": "dm", "purpose": PURPOSE,
                    "package_kind": "tmengine"}] if packages is None else packages
        target = envelope(signed("targets", version, self.namespace, packages=entries,
            release_keys={release[1]: {"public_key": release[2], "publisher": "acme", "engine_scopes": ["dm"],
                                       "purpose": PURPOSE, "channel": self.manifest["channel"]}}),
            self.roles["targets"][:2])
        revocation = envelope(signed("revocations", version, self.namespace,
            key_ids=list(revoked_keys), digests=list(revoked_digests), reason="fixture exercise"),
            self.roles["revocations"][:2])
        snapshot = envelope(signed("snapshot", version, self.namespace,
            meta={"targets_version": version, "targets_digest": digest(target),
                  "revocations_version": version, "revocations_digest": digest(revocation)}),
            self.roles["snapshot"][:2])
        timestamp = envelope(signed("timestamp", version, self.namespace,
            snapshot={"version": version, "digest": digest(snapshot)}),
            self.roles["timestamp"][:2])
        return {"root_chain": list(root_chain), "targets": target, "revocations": revocation,
                "snapshot": snapshot, "timestamp": timestamp}


def ready(tmp_path, fixture=None):
    fixture = fixture or Fixture()
    store = TrustStore(tmp_path / "state", fixture.namespace)
    store.bootstrap(fixture.root, expected_root_sha256=digest(fixture.root), now=NOW, fixture_only=True)
    store.update(fixture.bundle(), now=NOW)
    return fixture, store


def rejects(code, fn):
    with pytest.raises(TrustError) as exc:
        fn()
    assert exc.value.code == code


def test_bootstrap_anchor_signature_and_namespace(tmp_path):
    f = Fixture()
    store = TrustStore(tmp_path / "state", f.namespace)
    rejects("bootstrap_anchor_mismatch", lambda: store.bootstrap(f.root, expected_root_sha256="sha256:" + "0" * 64, now=NOW))
    bad = copy.deepcopy(f.root)
    bad["signatures"] = bad["signatures"][:1]
    rejects("metadata_threshold_not_met", lambda: store.bootstrap(bad, expected_root_sha256=digest(bad), now=NOW))
    store.bootstrap(f.root, expected_root_sha256=digest(f.root), now=NOW, fixture_only=True)
    rejects("bootstrap_already_exists", lambda: store.bootstrap(f.root, expected_root_sha256=digest(f.root), now=NOW))
    other = TrustStore(tmp_path / "other", "tmengine.production")
    rejects("metadata_role_or_namespace_mismatch", lambda: other.bootstrap(f.root, expected_root_sha256=digest(f.root), now=NOW))


def test_complete_chain_authorization_reopen_and_scope(tmp_path):
    f, store = ready(tmp_path)
    key_obj = store.authorize_package(f.manifest, f.package_digest, now=NOW, rollback=True)
    assert key_obj.key_id == f.release[1] and key_obj.fixture_only
    reopened = TrustStore(tmp_path / "state", f.namespace)
    assert reopened.authorize_package(f.manifest, f.package_digest, now=NOW).key_id == key_obj.key_id
    signature = {"algorithm": "ed25519", "key_id": key_obj.key_id,
                 "value": base64.b64encode(f.release[0].sign(canonical_manifest_bytes(f.manifest))).decode()}
    verified = verify_engine_manifest_signature(f.manifest, signature, trusted_keys=[key_obj],
        expected_namespace=f.namespace, expected_channel="test")
    assert verified.manifest_sha256 == f.package_digest.removeprefix("sha256:")
    invalid = dict(signature, value=base64.b64encode(b"x" * 64).decode())
    rejects("signature_invalid", lambda: verify_engine_manifest_signature(f.manifest, invalid,
        trusted_keys=[key_obj], expected_namespace=f.namespace, expected_channel="test"))
    wrong = dict(f.manifest, publisher="stranger")
    rejects("package_digest_mismatch", lambda: reopened.authorize_package(wrong, f.package_digest, now=NOW))
    rejects("package_not_endorsed", lambda: reopened.authorize_package(wrong, digest(wrong), now=NOW))
    rejects("trusted_clock_rollback", lambda: reopened.authorize_package(f.manifest, f.package_digest, now=NOW-timedelta(seconds=1)))


def test_binding_expiry_replay_equivocation_and_atomic_failure(tmp_path):
    f, store = ready(tmp_path)
    bundle = f.bundle(version=2)
    broken = copy.deepcopy(bundle)
    broken["timestamp"]["signed"]["snapshot"]["digest"] = "sha256:" + "0" * 64
    broken["timestamp"] = envelope(broken["timestamp"]["signed"], f.roles["timestamp"][:2])
    rejects("timestamp_binding_invalid", lambda: store.update(broken, now=NOW))
    assert store.status()["timestamp_version"] == 1
    store.update(bundle, now=NOW)
    rejects("timestamp_replay", lambda: store.update(bundle, now=NOW))
    rejects("metadata_rollback_or_equivocation", lambda: store.update(f.bundle(version=1), now=NOW))
    changed = f.bundle(version=3)
    changed["targets"] = f.bundle(version=2, packages=[])["targets"]
    rejects("metadata_rollback_or_equivocation", lambda: store.update(changed, now=NOW))
    rejects("trusted_clock_rollback", lambda: store.update(f.bundle(version=3), now=NOW-timedelta(seconds=1)))
    rejects("metadata_expired", lambda: store.update(f.bundle(version=3), now=NOW+timedelta(days=31)))


def test_continuous_dual_quorum_root_rotation(tmp_path):
    f, store = ready(tmp_path)
    old = f.root
    new = f.make_root(2, f.roots[1:3])
    # Both old and new quorums: signer 0 + shared signer 1 + signer 2.
    new = envelope(new["signed"], *[(x[0], x[1]) for x in f.roots])
    rejects("root_version_not_consecutive", lambda: store.update(f.bundle(version=2, root_chain=[f.make_root(3, f.roots[1:3])]), now=NOW))
    only_new = f.make_root(2, f.roots[1:3])
    rejects("metadata_threshold_not_met", lambda: store.update(f.bundle(version=2, root_chain=[only_new]), now=NOW))
    result = store.update(f.bundle(version=2, root_chain=[new]), now=NOW)
    assert result["root_version"] == 2
    assert old["signed"]["version"] == 1


def test_revocation_persists_and_old_target_cannot_reendorse(tmp_path):
    f, store = ready(tmp_path)
    store.update(f.bundle(version=2, revoked_digests=[f.package_digest]), now=NOW)
    rejects("package_revoked", lambda: store.authorize_package(f.manifest, f.package_digest, now=NOW))
    store.update(f.bundle(version=3), now=NOW)
    reopened = TrustStore(tmp_path / "state", f.namespace)
    rejects("package_revoked", lambda: reopened.authorize_package(f.manifest, f.package_digest, now=NOW, rollback=True))
    assert reopened.status()["revoked_digests"] == 1


def test_key_revocation_and_current_reendorsement_required(tmp_path):
    f, store = ready(tmp_path)
    store.update(f.bundle(version=2, packages=[]), now=NOW)
    rejects("package_not_endorsed", lambda: store.authorize_package(f.manifest, f.package_digest, now=NOW, rollback=True))
    store.update(f.bundle(version=3), now=NOW)
    assert store.authorize_package(f.manifest, f.package_digest, now=NOW, rollback=True).key_id == f.release[1]
    store.update(f.bundle(version=4, revoked_keys=[f.release[1]]), now=NOW)
    rejects("package_revoked", lambda: store.authorize_package(f.manifest, f.package_digest, now=NOW))


def _worker(path, namespace, bundle, queue):
    try:
        result = TrustStore(path, namespace).update(bundle, now=NOW)
        queue.put(result["timestamp_version"])
    except TrustError as exc:
        queue.put(exc.code)


def test_cross_process_lock_serializes_duplicate_update(tmp_path):
    f, store = ready(tmp_path)
    queue = multiprocessing.Queue()
    bundle = f.bundle(version=2)
    procs = [multiprocessing.Process(target=_worker, args=(tmp_path / "state", f.namespace, bundle, queue)) for _ in range(2)]
    for process in procs:
        process.start()
    for process in procs:
        process.join(10)
        assert process.exitcode == 0
    assert sorted([queue.get(timeout=2), queue.get(timeout=2)], key=str) == [2, "timestamp_replay"]
    assert store.status()["timestamp_version"] == 2


def test_fixture_production_namespace_isolated(tmp_path):
    f, store = ready(tmp_path, Fixture("tmengine.production"))
    assert store.authorize_package(f.manifest, f.package_digest, now=NOW).fixture_only
    assert store.status()["namespace"] == "tmengine.production"
    test_store = TrustStore(tmp_path / "separate", NAMESPACE)
    rejects("metadata_role_or_namespace_mismatch", lambda: test_store.bootstrap(
        f.root, expected_root_sha256=digest(f.root), now=NOW))


def test_failed_commit_recovers_previous_complete_state(tmp_path, monkeypatch):
    f, store = ready(tmp_path)
    original = store._write

    def crash_after_write(db, state):
        original(db, state)
        raise RuntimeError("simulated interruption before commit")

    monkeypatch.setattr(store, "_write", crash_after_write)
    with pytest.raises(RuntimeError):
        store.update(f.bundle(version=2), now=NOW)
    reopened = TrustStore(tmp_path / "state", f.namespace)
    assert reopened.status()["timestamp_version"] == 1
    assert reopened.authorize_package(f.manifest, f.package_digest, now=NOW).key_id == f.release[1]


def test_corrupt_stored_signature_fails_closed(tmp_path):
    f, store = ready(tmp_path)
    with sqlite3.connect(store.db_path) as db:
        row = db.execute("SELECT payload FROM ledger").fetchone()
        state = __import__("json").loads(row[0])
        state["targets"]["signed"]["packages"] = []
        db.execute("UPDATE ledger SET payload=?", (__import__("json").dumps(state),))
    rejects("metadata_threshold_not_met", lambda: store.authorize_package(f.manifest, f.package_digest, now=NOW))


def test_release_key_cannot_be_root_metadata_key(tmp_path):
    f, store = ready(tmp_path)
    target = f.bundle(version=2)
    key_record = target["targets"]["signed"]["release_keys"].pop(f.release[1])
    root_key = f.roots[0][1]
    key_record["public_key"] = f.roots[0][2]
    target["targets"]["signed"]["release_keys"][root_key] = key_record
    target["targets"]["signed"]["packages"][0]["signing_key_id"] = root_key
    target["targets"] = envelope(target["targets"]["signed"], f.roles["targets"][:2])
    target["snapshot"]["signed"]["meta"]["targets_digest"] = digest(target["targets"])
    target["snapshot"] = envelope(target["snapshot"]["signed"], f.roles["snapshot"][:2])
    target["timestamp"]["signed"]["snapshot"]["digest"] = digest(target["snapshot"])
    target["timestamp"] = envelope(target["timestamp"]["signed"], f.roles["timestamp"][:2])
    rejects("release_root_key_reuse", lambda: store.update(target, now=NOW))


def test_guard_serializes_revocation_after_install_commit(tmp_path):
    f, store = ready(tmp_path)
    started, finished = threading.Event(), threading.Event()
    results = []

    def revoke():
        started.set()
        results.append(store.update(f.bundle(version=2, revoked_digests=[f.package_digest]), now=NOW))
        finished.set()

    with store.guard_package(f.manifest, f.package_digest, now=NOW) as authorized:
        assert authorized.key_id == f.release[1]
        worker = threading.Thread(target=revoke)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        # The caller's durable package commit would occur here.
    worker.join(2)
    assert finished.is_set() and results[0]["revoked_digests"] == 1
    rejects("package_revoked", lambda: store.authorize_package(f.manifest, f.package_digest, now=NOW))
