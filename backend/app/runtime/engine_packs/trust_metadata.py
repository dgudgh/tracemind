"""Offline engine trust metadata and persistent anti-rollback ledger.

Wire format is deliberately small and independent of package supplied keys.
Every envelope is {"signed": object, "signatures": [{"key_id", "value"}]}.
Signatures cover canonical UTF-8 JSON of ``signed``.  The caller supplies the
first root digest out of band; later roots require consecutive dual quorum.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .trust import (NAMESPACE_CHANNELS, PACKAGE_KIND, PURPOSE, TrustedEngineKey,
                    TrustError, canonical_manifest_bytes, key_id_for, parse_manifest_json)

MAX_METADATA_BYTES = 1024 * 1024
MAX_ITEMS = 4096
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _fail(code):
    raise TrustError(code)


def _exact(value, fields, code="metadata_schema_invalid"):
    if type(value) is not dict or set(value) != set(fields):
        _fail(code)


def _positive(value):
    if type(value) is not int or value < 1 or value > 2**31 - 1:
        _fail("metadata_version_invalid")
    return value


def _digest(value):
    if type(value) is not str or not SHA.fullmatch(value):
        _fail("metadata_digest_invalid")
    return value


def _bytes(value):
    try:
        raw = base64.b64decode(value, validate=True) if type(value) is str else None
    except (ValueError, binascii.Error):
        raw = None
    if raw is None:
        _fail("metadata_base64_invalid")
    return raw


def _canonical(value):
    data = canonical_manifest_bytes(value)
    if len(data) > MAX_METADATA_BYTES:
        _fail("metadata_too_large")
    return data


def _sha(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _time(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        _fail("metadata_time_invalid")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail("metadata_time_invalid")
    return instant


def _now(value):
    if value is None:
        value = datetime.now(timezone.utc)
    if type(value) is not datetime or value.tzinfo is None:
        _fail("trusted_time_required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _envelope(raw, role, namespace, now):
    if isinstance(raw, (bytes, str)):
        if len(raw) > MAX_METADATA_BYTES:
            _fail("metadata_too_large")
        raw = parse_manifest_json(raw)
    _exact(raw, ("signed", "signatures"))
    signed = raw["signed"]
    if type(signed) is not dict or signed.get("role") != role or signed.get("namespace") != namespace:
        _fail("metadata_role_or_namespace_mismatch")
    _positive(signed.get("version"))
    if _time(signed.get("expires")) <= now:
        _fail("metadata_expired")
    if type(raw["signatures"]) is not list or not 1 <= len(raw["signatures"]) <= 64:
        _fail("metadata_signatures_invalid")
    seen = set()
    for sig in raw["signatures"]:
        _exact(sig, ("key_id", "value"))
        if type(sig["key_id"]) is not str or sig["key_id"] in seen or len(_bytes(sig["value"])) != 64:
            _fail("metadata_signatures_invalid")
        seen.add(sig["key_id"])
    _canonical(signed)
    return raw


def _root(raw, namespace, now):
    raw = _envelope(raw, "root", namespace, now)
    signed = raw["signed"]
    _exact(signed, ("role", "namespace", "version", "expires", "keys", "roles"))
    keys, roles = signed["keys"], signed["roles"]
    if type(keys) is not dict or not 1 <= len(keys) <= 64 or type(roles) is not dict:
        _fail("root_policy_invalid")
    for key_id, encoded in keys.items():
        public = _bytes(encoded)
        if len(public) != 32 or key_id_for(namespace, public) != key_id:
            _fail("root_key_id_invalid")
    if set(roles) != {"root", "targets", "snapshot", "timestamp", "revocations"}:
        _fail("root_roles_invalid")
    for role in roles.values():
        _exact(role, ("key_ids", "threshold"))
        ids = role["key_ids"]
        if (type(ids) is not list or not ids or any(type(x) is not str for x in ids)
                or len(ids) != len(set(ids)) or any(x not in keys for x in ids)):
            _fail("root_role_keys_invalid")
        if type(role["threshold"]) is not int or not 1 <= role["threshold"] <= len(ids):
            _fail("root_threshold_invalid")
    return raw


def _verify(raw, root, role):
    signed = raw["signed"]
    policy = root["signed"]["roles"][role]
    keys = root["signed"]["keys"]
    good = set()
    message = _canonical(signed)
    for sig in raw["signatures"]:
        key_id = sig["key_id"]
        if key_id not in policy["key_ids"]:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(_bytes(keys[key_id])).verify(_bytes(sig["value"]), message)
        except InvalidSignature:
            continue
        good.add(key_id)
    if len(good) < policy["threshold"]:
        _fail("metadata_threshold_not_met")


def _targets(raw, namespace, now):
    raw = _envelope(raw, "targets", namespace, now)
    value = raw["signed"]
    _exact(value, ("role", "namespace", "version", "expires", "packages", "release_keys"))
    packages, keys = value["packages"], value["release_keys"]
    if type(packages) is not list or type(keys) is not dict or len(packages) > MAX_ITEMS or len(keys) > 64:
        _fail("targets_schema_invalid")
    for key_id, item in keys.items():
        _exact(item, ("public_key", "publisher", "engine_scopes", "purpose", "channel"))
        public = _bytes(item["public_key"])
        if len(public) != 32 or key_id_for(namespace, public) != key_id:
            _fail("release_key_invalid")
        if (item["purpose"] != PURPOSE or item["channel"] != NAMESPACE_CHANNELS[namespace]
                or type(item["publisher"]) is not str or not IDENT.fullmatch(item["publisher"]) 
                or type(item["engine_scopes"]) is not list or not item["engine_scopes"]
                or any(type(x) is not str or not IDENT.fullmatch(x) for x in item["engine_scopes"])
                or len(item["engine_scopes"]) != len(set(item["engine_scopes"]))):
            _fail("release_scope_invalid")
    digests = set()
    for item in packages:
        _exact(item, ("digest", "signing_key_id", "publisher", "engine_id", "purpose", "package_kind"))
        digest = _digest(item["digest"])
        if (digest in digests or type(item["signing_key_id"]) is not str
                or item["signing_key_id"] not in keys):
            _fail("targets_package_invalid")
        digests.add(digest)
        key = keys[item["signing_key_id"]]
        if (item["publisher"] != key["publisher"] or item["engine_id"] not in key["engine_scopes"]
                or item["purpose"] != PURPOSE or item["package_kind"] != PACKAGE_KIND):
            _fail("targets_scope_mismatch")
    return raw


def _revocations(raw, namespace, now):
    raw = _envelope(raw, "revocations", namespace, now)
    value = raw["signed"]
    _exact(value, ("role", "namespace", "version", "expires", "key_ids", "digests", "reason"))
    for field in ("key_ids", "digests"):
        items = value[field]
        if (type(items) is not list or len(items) > MAX_ITEMS or any(type(x) is not str for x in items)
                or len(items) != len(set(items))):
            _fail("revocations_schema_invalid")
        for item in items:
            if field == "digests":
                _digest(item)
            elif type(item) is not str or not item.startswith(namespace + ":") or not HEX.fullmatch(item[len(namespace)+1:]):
                _fail("revocations_schema_invalid")
    if type(value["reason"]) is not str or not 1 <= len(value["reason"]) <= 512:
        _fail("revocations_reason_required")
    return raw


def _binding(raw, role, namespace, now, bound_role):
    raw = _envelope(raw, role, namespace, now)
    value = raw["signed"]
    _exact(value, ("role", "namespace", "version", "expires", bound_role))
    links = value[bound_role]
    expected = ("version", "digest") if role == "timestamp" else ("targets_version", "targets_digest", "revocations_version", "revocations_digest")
    _exact(links, expected)
    for name in expected:
        _digest(links[name]) if name.endswith("digest") else _positive(links[name])
    return raw


def _blob(raw):
    return _canonical(raw)


class TrustStore:
    """Per-namespace trust ledger. Caller owns OS clock security and state backup.

    All operations take an exclusive cross-process lock and a SQLite FULL-sync
    transaction.  No private keys are loaded, created, or persisted here.
    """

    def __init__(self, state_dir, expected_namespace):
        if expected_namespace not in NAMESPACE_CHANNELS:
            _fail("trust_namespace_invalid")
        self.namespace = expected_namespace
        self.path = Path(state_dir)
        if self.path.is_symlink() or (self.path.exists() and (not self.path.is_dir() or self.path.stat().st_uid != os.getuid())):
            _fail("trust_state_path_untrusted")
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.stat().st_mode & 0o077:
            _fail("trust_state_permissions_invalid")
        self.lock_path = self.path / "trust.lock"
        self.db_path = self.path / "trust.sqlite3"
        for item in (self.lock_path, self.db_path):
            if item.is_symlink():
                _fail("trust_state_path_untrusted")

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
                _fail("trust_state_permissions_invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not self.db_path.exists():
                created = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(created)
            details = self.db_path.lstat()
            if (self.db_path.is_symlink() or not self.db_path.is_file()
                    or details.st_uid != os.getuid() or details.st_mode & 0o077):
                _fail("trust_state_permissions_invalid")
            db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            try:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS ledger (namespace TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, db):
        row = db.execute("SELECT payload FROM ledger WHERE namespace=?", (self.namespace,)).fetchone()
        if row is None:
            return None
        state = parse_manifest_json(row[0])
        _exact(state, ("namespace", "fixture_only", "last_seen", "root", "targets", "snapshot", "timestamp", "revocations", "revoked_keys", "revoked_digests"), "trust_state_invalid")
        if state["namespace"] != self.namespace:
            _fail("trust_state_invalid")
        if (type(state["fixture_only"]) is not bool
                or any(type(state[name]) is not list or any(type(x) is not str for x in state[name])
                       for name in ("revoked_keys", "revoked_digests"))):
            _fail("trust_state_invalid")
        _time(state["last_seen"])
        return state

    def _verify_stored(self, state, now):
        root = _root(state["root"], self.namespace, now)
        _verify(root, root, "root")
        if state["timestamp"] is None:
            return
        targets = _targets(state["targets"], self.namespace, now)
        revocations = _revocations(state["revocations"], self.namespace, now)
        snapshot = _binding(state["snapshot"], "snapshot", self.namespace, now, "meta")
        timestamp = _binding(state["timestamp"], "timestamp", self.namespace, now, "snapshot")
        for role, obj in (("targets", targets), ("revocations", revocations),
                          ("snapshot", snapshot), ("timestamp", timestamp)):
            _verify(obj, root, role)
        if set(targets["signed"]["release_keys"]) & set(root["signed"]["keys"]):
            _fail("release_root_key_reuse")
        meta = snapshot["signed"]["meta"]
        if (meta["targets_version"] != targets["signed"]["version"] or meta["targets_digest"] != _sha(_blob(targets))
                or meta["revocations_version"] != revocations["signed"]["version"]
                or meta["revocations_digest"] != _sha(_blob(revocations))):
            _fail("snapshot_binding_invalid")
        link = timestamp["signed"]["snapshot"]
        if link["version"] != snapshot["signed"]["version"] or link["digest"] != _sha(_blob(snapshot)):
            _fail("timestamp_binding_invalid")
        if (not set(revocations["signed"]["key_ids"]).issubset(state["revoked_keys"])
                or not set(revocations["signed"]["digests"]).issubset(state["revoked_digests"])):
            _fail("revocation_ledger_invalid")

    def _write(self, db, state):
        db.execute("INSERT INTO ledger(namespace,payload) VALUES(?,?) ON CONFLICT(namespace) DO UPDATE SET payload=excluded.payload",
                   (self.namespace, _blob(state)))

    @staticmethod
    def _time_guard(state, now):
        if state and now < _time(state["last_seen"]):
            _fail("trusted_clock_rollback")

    @staticmethod
    def _stamp(now):
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def bootstrap(self, root, *, expected_root_sha256, now=None, fixture_only=False):
        now = _now(now)
        if type(expected_root_sha256) is not str or not SHA.fullmatch(expected_root_sha256) or type(fixture_only) is not bool:
            _fail("bootstrap_anchor_required")
        root = _root(root, self.namespace, now)
        if root["signed"]["version"] != 1 or _sha(_blob(root)) != expected_root_sha256:
            _fail("bootstrap_anchor_mismatch")
        _verify(root, root, "root")
        with self._locked() as db:
            if self._read(db) is not None:
                _fail("bootstrap_already_exists")
            self._write(db, {"namespace": self.namespace, "fixture_only": fixture_only,
                "last_seen": self._stamp(now), "root": root, "targets": None,
                "snapshot": None, "timestamp": None, "revocations": None,
                "revoked_keys": [], "revoked_digests": []})

    def update(self, bundle, *, now=None):
        """Apply a complete offline bundle atomically, including consecutive roots."""
        now = _now(now)  # one fixed validation start instant
        _exact(bundle, ("root_chain", "targets", "snapshot", "timestamp", "revocations"))
        roots = bundle["root_chain"]
        if type(roots) is not list or len(roots) > 32:
            _fail("root_chain_invalid")
        with self._locked() as db:
            state = self._read(db)
            if state is None:
                _fail("bootstrap_required")
            self._time_guard(state, now)
            self._verify_stored(state, now)
            root = state["root"]
            for candidate in roots:
                new = _root(candidate, self.namespace, now)
                if new["signed"]["version"] != root["signed"]["version"] + 1:
                    _fail("root_version_not_consecutive")
                _verify(new, root, "root")
                _verify(new, new, "root")
                root = new
            targets = _targets(bundle["targets"], self.namespace, now)
            revocations = _revocations(bundle["revocations"], self.namespace, now)
            snapshot = _binding(bundle["snapshot"], "snapshot", self.namespace, now, "meta")
            timestamp = _binding(bundle["timestamp"], "timestamp", self.namespace, now, "snapshot")
            for role, obj in (("targets", targets), ("revocations", revocations),
                              ("snapshot", snapshot), ("timestamp", timestamp)):
                _verify(obj, root, role)
                old = state[role]
                if old is not None:
                    old_version = old["signed"]["version"]
                    version = obj["signed"]["version"]
                    if version < old_version or (version == old_version and _sha(_blob(obj)) != _sha(_blob(old))):
                        _fail("metadata_rollback_or_equivocation")
                    if role == "timestamp" and version <= old_version:
                        _fail("timestamp_replay")
            if set(targets["signed"]["release_keys"]) & set(root["signed"]["keys"]):
                _fail("release_root_key_reuse")
            meta = snapshot["signed"]["meta"]
            if (meta["targets_version"] != targets["signed"]["version"] or meta["targets_digest"] != _sha(_blob(targets))
                    or meta["revocations_version"] != revocations["signed"]["version"]
                    or meta["revocations_digest"] != _sha(_blob(revocations))):
                _fail("snapshot_binding_invalid")
            link = timestamp["signed"]["snapshot"]
            if link["version"] != snapshot["signed"]["version"] or link["digest"] != _sha(_blob(snapshot)):
                _fail("timestamp_binding_invalid")
            state.update(root=root, targets=targets, revocations=revocations,
                         snapshot=snapshot, timestamp=timestamp, last_seen=self._stamp(now),
                         revoked_keys=sorted(set(state["revoked_keys"]) | set(revocations["signed"]["key_ids"])),
                         revoked_digests=sorted(set(state["revoked_digests"]) | set(revocations["signed"]["digests"])))
            self._write(db, state)
            return {"root_version": root["signed"]["version"],
                    "targets_version": targets["signed"]["version"],
                    "timestamp_version": timestamp["signed"]["version"],
                    "revoked_keys": len(state["revoked_keys"]), "revoked_digests": len(state["revoked_digests"]),
                    "fixture_only": state["fixture_only"]}

    @contextlib.contextmanager
    def guard_package(self, manifest, package_digest, *, now=None, rollback=False):
        """Hold trust lock through caller's signature check and install commit.

        Installers should use this guard around archive verification and their
        own durable commit so a concurrent revocation cannot overtake approval.
        Acquire the repository lock before this lock consistently.
        """
        now = _now(now)
        if type(rollback) is not bool:
            _fail("trust_policy_invalid")
        _digest(package_digest)
        if package_digest != _sha(canonical_manifest_bytes(manifest)):
            _fail("package_digest_mismatch")
        with self._locked() as db:
            state = self._read(db)
            if state is None or state["timestamp"] is None:
                _fail("trust_metadata_not_ready")
            self._time_guard(state, now)
            self._verify_stored(state, now)
            if (manifest.get("namespace") != self.namespace or manifest.get("channel") != NAMESPACE_CHANNELS[self.namespace]
                    or manifest.get("purpose") != PURPOSE or manifest.get("package_kind") != PACKAGE_KIND):
                _fail("package_scope_invalid")
            target = next((x for x in state["targets"]["signed"]["packages"] if x["digest"] == package_digest), None)
            if target is None:
                _fail("package_not_endorsed")
            for field in ("publisher", "engine_id"):
                if manifest.get(field) != target[field]:
                    _fail("package_scope_invalid")
            key_id = target["signing_key_id"]
            if manifest.get("signing_key_id") != key_id:
                _fail("release_key_mismatch")
            if key_id in state["revoked_keys"] or package_digest in state["revoked_digests"]:
                _fail("package_revoked")
            key = state["targets"]["signed"]["release_keys"][key_id]
            trusted = TrustedEngineKey.create(_bytes(key["public_key"]), namespace=self.namespace,
                channel=NAMESPACE_CHANNELS[self.namespace], publisher=key["publisher"],
                engine_scopes=key["engine_scopes"], fixture_only=state["fixture_only"])
            yield trusted
            state["last_seen"] = self._stamp(now)
            self._write(db, state)

    def authorize_package(self, manifest, package_digest, *, now=None, rollback=False):
        """Select a scoped key. Use guard_package for atomic install/activation."""
        with self.guard_package(manifest, package_digest, now=now, rollback=rollback) as trusted:
            return trusted

    def status(self):
        with self._locked() as db:
            state = self._read(db)
            if state is None:
                return {"bootstrapped": False, "namespace": self.namespace}
            return {"bootstrapped": True, "namespace": self.namespace,
                    "fixture_only": state["fixture_only"], "last_seen": state["last_seen"],
                    "root_version": state["root"]["signed"]["version"],
                    "targets_version": state["targets"]["signed"]["version"] if state["targets"] else None,
                    "timestamp_version": state["timestamp"]["signed"]["version"] if state["timestamp"] else None,
                    "revoked_keys": len(state["revoked_keys"]), "revoked_digests": len(state["revoked_digests"])}
