"""Owner-only lab package repository; not a production management endpoint.

Installs verified immutable files, never activates a Provider or changes a
platform database. Production trust admission and privileged IPC are separate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .archive import (ArchiveError, _directory_sync, _no_link_path,
                      _regular_open, open_verified_archive, safe_name, approved_entrypoints)
from .contract import build_payload_inventory
from .trust import canonical_manifest_bytes, parse_manifest_json, verify_engine_manifest_signature, NAMESPACE_CHANNELS


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


class EngineRepository:
    """All writes are serialized under an OS lock and a private owner root."""

    def __init__(self, root, *, trusted_keys=(), namespace="tmengine.test", channel="test",
                 trust_guard=None, mode="managed", checkpoint=None,
                 entrypoint_ids=None):
        if os.name != "posix":
            raise ArchiveError("repository_os_unsupported")
        self.root = _no_link_path(root)
        self.keys = tuple(trusted_keys)
        self.namespace, self.channel, self.trust_guard, self.mode = namespace, channel, trust_guard, mode
        self.fixture_only = mode == "lab" or any(key.fixture_only for key in self.keys)
        if trust_guard is not None:
            state = trust_guard.status()
            self.fixture_only = self.fixture_only or state.get("fixture_only", True)
        self.entrypoint_ids = approved_entrypoints(entrypoint_ids if entrypoint_ids is not None else
            (("python.sdk.provider.v1",) if namespace == "tmengine.production" else
             ("python.sdk.fixture.v1", "python.sdk.provider.v1")))
        if namespace == "tmengine.production" and not self.fixture_only and "python.sdk.fixture.v1" in self.entrypoint_ids:
            raise ArchiveError("repository_fixture_entrypoint_forbidden")
        if namespace not in NAMESPACE_CHANNELS or NAMESPACE_CHANNELS[namespace] != channel:
            raise ArchiveError("repository_trust_domain_invalid")
        if mode not in {"lab", "managed"}:
            raise ArchiveError("repository_mode_invalid")
        if mode == "lab" and (namespace != "tmengine.test" or not self.keys):
            raise ArchiveError("repository_test_trust_required")
        if mode == "managed" and (trust_guard is None or trust_guard.namespace != namespace):
            raise ArchiveError("repository_lifecycle_trust_required")
        if any(k.namespace != namespace or k.channel != channel for k in self.keys):
            raise ArchiveError("repository_trust_domain_invalid")
        self._marker = ({"schema": "tmengine.lab-repository/v1", "uid": os.getuid()} if mode == "lab" else
            {"schema": "tmengine.repository/v1", "uid": os.getuid(), "namespace":namespace, "channel":channel})
        self.checkpoint = checkpoint or (lambda _point, _operation: None)
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
        marker = self.root / "REPOSITORY.json"
        self._private(self.root, directory=True)
        if not marker.exists():
            if any(self.root.iterdir()):
                raise ArchiveError("repository_owner_missing")
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(_json(self._marker))
                stream.flush()
                os.fsync(stream.fileno())
            _directory_sync(self.root)
        self._guard()
        for name in ("incoming", "packages", ".staging"):
            directory = self.root / name
            directory.mkdir(mode=0o700, exist_ok=True)
            self._private(directory, directory=True)
        with self._locked() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                  operation_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                  import_name TEXT NOT NULL, archive_sha256 TEXT NOT NULL,
                  package_digest TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, error_code TEXT);
                CREATE TABLE IF NOT EXISTS packages (
                  package_digest TEXT PRIMARY KEY, engine_id TEXT NOT NULL,
                  publisher TEXT NOT NULL, version TEXT NOT NULL, namespace TEXT NOT NULL,
                  os TEXT NOT NULL, cpu TEXT NOT NULL, abi TEXT NOT NULL,
                  payload_digest TEXT NOT NULL, installed_at TEXT NOT NULL,
                  UNIQUE(engine_id,publisher,version,namespace,os,cpu,abi));
                CREATE TABLE IF NOT EXISTS audit (
                  sequence INTEGER PRIMARY KEY, record TEXT NOT NULL,
                  previous_digest TEXT NOT NULL, record_digest TEXT NOT NULL);
            """)

    @staticmethod
    def _private(path, *, directory=False):
        st = path.lstat()
        if (st.st_uid != os.getuid() or st.st_mode & 0o077 or stat.S_ISLNK(st.st_mode)
                or (not stat.S_ISDIR(st.st_mode) if directory else not stat.S_ISREG(st.st_mode))
                or (not directory and st.st_nlink != 1)):
            raise ArchiveError("repository_owner_mismatch")

    def _guard(self):
        _no_link_path(self.root)
        self._private(self.root, directory=True)
        marker = self.root / "REPOSITORY.json"
        self._private(marker)
        with _regular_open(marker) as source:
            value = parse_manifest_json(source.read(4097))
        if value != self._marker:
            raise ArchiveError("repository_owner_mismatch")

    @contextmanager
    def _locked(self):
        import fcntl
        self._guard()
        for name in ("incoming", "packages", ".staging"):
            self._private(self.root / name, directory=True)
        lock_path, db_path = self.root / "repository.lock", self.root / "repository.sqlite"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self._private(lock_path)
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not db_path.exists():
                os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            self._private(db_path)
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                yield db
            finally:
                db.close()
        finally:
            os.close(fd)

    def _audit(self, db, action, status, *, operation_id=None, code=None):
        prior = db.execute("SELECT record_digest FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = prior[0] if prior else "0" * 64
        record = _json({"at": _now(), "uid": os.getuid(), "action": action, "status": status,
                        "operation_id": operation_id, "code": code})
        digest = hashlib.sha256((previous + "\n" + record).encode()).hexdigest()
        db.execute("INSERT INTO audit(record,previous_digest,record_digest) VALUES(?,?,?)",
                   (record, previous, digest))

    def _stage(self, operation):
        op = operation["operation_id"]
        if re.fullmatch(r"[0-9a-f]{32}", op) is None:
            raise ArchiveError("repository_operation_invalid")
        return self.root / ".staging" / op

    def _target(self, digest):
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ArchiveError("repository_digest_invalid")
        return self.root / "packages" / digest[7:]

    @contextmanager
    def _verify_tree_guard(self, path, expected_digest, *, rollback=False):
        _no_link_path(path)
        with _regular_open(path / "ENGINE-MANIFEST.json") as source:
            manifest = parse_manifest_json(source.read(4 * 1024 * 1024 + 1))
        with _regular_open(path / "ENGINE-MANIFEST.sig") as source:
            signature = parse_manifest_json(source.read(4 * 1024 * 1024 + 1))
        digest = "sha256:" + hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
        if digest != expected_digest:
            raise ArchiveError("repository_package_identity_mismatch")
        @contextmanager
        def keys_for_transaction():
            if self.trust_guard is None:
                yield self.keys
            else:
                with self.trust_guard.guard_package(manifest, digest, rollback=rollback) as key:
                    yield (key,)
        with keys_for_transaction() as keys:
            verify_engine_manifest_signature(manifest, signature, trusted_keys=keys,
                expected_namespace=self.namespace, expected_channel=self.channel)
            payload = build_payload_inventory(path, manifest)
            yield manifest, payload["self_payload_digest"], signature, keys

    def _verify_tree(self, path, expected_digest):
        with self._verify_tree_guard(path, expected_digest) as (manifest, payload, _signature, _keys):
            return manifest, payload

    @contextmanager
    def guard_installed(self, digest, *, rollback=False):
        """Business lock then trust lock, held through caller readiness and CAS."""
        with self._locked() as db:
            if not db.execute("SELECT 1 FROM packages WHERE package_digest=?", (digest,)).fetchone():
                raise ArchiveError("repository_package_missing")
            with self._verify_tree_guard(self._target(digest), digest, rollback=rollback) as (manifest, payload, signature, keys):
                yield {"manifest":manifest,"payload_digest":payload,"signature":signature,
                       "trusted_keys":keys,"source":self._target(digest),"package_digest":digest,
                       "fixture_only":self.fixture_only}

    def _discard_stage(self, operation):
        stage = self._stage(operation)
        if stage.is_symlink():
            raise ArchiveError("repository_stage_untrusted")
        if stage.exists():
            for item in stage.rglob("*"):
                if item.is_symlink() or item.lstat().st_uid != os.getuid():
                    raise ArchiveError("repository_stage_untrusted")
                if item.is_dir():
                    item.chmod(0o700)
            stage.chmod(0o700)
            shutil.rmtree(stage)
            _directory_sync(stage.parent)

    @staticmethod
    def _freeze(stage, *, seal_root=True):
        for item in stage.rglob("*"):
            item.chmod(0o500 if item.is_dir() else 0o400)
        # Darwin requires the source directory itself to be writable for rename.
        # The repository lock and private parent keep this unpublished root private.
        stage.chmod(0o500 if seal_root else 0o700)
        _directory_sync(stage)

    def _publish(self, db, operation):
        stage, target = self._stage(operation), self._target(operation["package_digest"])
        with self._verify_tree_guard(target if target.exists() else stage, operation["package_digest"]) as (manifest, payload, _signature, _keys):
            self._publish_verified(db, operation, manifest, payload)

    def _publish_verified(self, db, operation, manifest, payload):
        stage, target = self._stage(operation), self._target(operation["package_digest"])
        identity = (manifest["engine_id"], manifest["publisher"], manifest["version"], manifest["namespace"],
                    manifest["os"], manifest["cpu"], manifest["python_abi"])
        existing = db.execute("SELECT package_digest FROM packages WHERE engine_id=? AND publisher=? "
                              "AND version=? AND namespace=? AND os=? AND cpu=? AND abi=?", identity).fetchone()
        if existing and existing[0] != operation["package_digest"]:
            raise ArchiveError("repository_version_conflict")
        if not target.exists():
            self._freeze(stage, seal_root=False)
            stage.rename(target)
            _directory_sync(stage.parent)
            _directory_sync(target.parent)
        else:
            self._discard_stage(operation)
        self._freeze(target)
        self.checkpoint("after_rename", operation)
        with db:
            db.execute("INSERT OR IGNORE INTO packages VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (operation["package_digest"], *identity, payload, _now()))
            db.execute("UPDATE operations SET state='installed',error_code=NULL WHERE operation_id=?",
                       (operation["operation_id"],))
            self._audit(db, "install", "installed", operation_id=operation["operation_id"])
        self.checkpoint("after_commit", operation)

    def _resume(self, db, operation):
        if operation["state"] == "prepared":
            self._discard_stage(operation)
            name = self._import_name(operation["import_name"])
            with open_verified_archive(self.root / "incoming" / name, trusted_keys=self.keys,
                    expected_namespace=self.namespace, expected_channel=self.channel,
                    expected_archive_sha256=operation["archive_sha256"],trust_guard=self.trust_guard,
                    entrypoint_ids=self.entrypoint_ids) as package:
                if package.package_digest != operation["package_digest"]:
                    raise ArchiveError("repository_package_identity_mismatch")
                package.extract(self._stage(operation))
            with db:
                db.execute("UPDATE operations SET state='staged' WHERE operation_id=?", (operation["operation_id"],))
                self._audit(db, "install", "staged", operation_id=operation["operation_id"])
            operation = dict(operation, state="staged")
            self.checkpoint("after_stage", operation)
        if operation["state"] == "staged":
            self._publish(db, operation)

    @staticmethod
    def _import_name(name):
        safe_name(name)
        if "/" in name or not name.endswith(".tmengine"):
            raise ArchiveError("repository_import_name_invalid")
        return name

    def _receipt(self, db, operation):
        manifest, payload = self._verify_tree(self._target(operation["package_digest"]), operation["package_digest"])
        return {"operation_id": operation["operation_id"], "request_id": operation["request_id"],
                "state": "installed", "package_digest": operation["package_digest"],
                "archive_sha256": operation["archive_sha256"], "payload_digest": payload,
                "engine_id": manifest["engine_id"], "version": manifest["version"],
                "activated": False, "lab_only": self.mode == "lab", "fixture_only":self.fixture_only}

    def install(self, import_name, request_id, *, expected_archive_sha256=None):
        with self._locked() as db:
            operation = None
            try:
                name = self._import_name(import_name)
                if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) is None:
                    raise ArchiveError("repository_request_invalid")
                with open_verified_archive(self.root / "incoming" / name, trusted_keys=self.keys,
                        expected_namespace=self.namespace, expected_channel=self.channel,
                        expected_archive_sha256=expected_archive_sha256,trust_guard=self.trust_guard,
                        entrypoint_ids=self.entrypoint_ids) as package:
                    prior = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
                    if prior:
                        operation = dict(prior)
                        if operation["archive_sha256"] != package.archive_sha256 or operation["package_digest"] != package.package_digest:
                            raise ArchiveError("repository_idempotency_conflict")
                        if operation["state"] == "blocked":
                            raise ArchiveError("repository_recovery_blocked")
                    else:
                        operation = {"operation_id": uuid.uuid4().hex, "request_id": request_id,
                            "import_name": name, "archive_sha256": package.archive_sha256,
                            "package_digest": package.package_digest, "state": "prepared", "created_at": _now(), "error_code": None}
                        with db:
                            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", tuple(operation.values()))
                            self._audit(db, "install", "prepared", operation_id=operation["operation_id"])
                        self.checkpoint("after_prepare", operation)
                        package.extract(self._stage(operation))
                        with db:
                            db.execute("UPDATE operations SET state='staged' WHERE operation_id=?", (operation["operation_id"],))
                            self._audit(db, "install", "staged", operation_id=operation["operation_id"])
                        operation["state"] = "staged"
                        self.checkpoint("after_stage", operation)
                self._resume(db, operation)
                receipt = self._receipt(db, operation)
                with db:
                    self._audit(db, "install", "receipt", operation_id=operation["operation_id"])
                return receipt
            except Exception as exc:
                with db:
                    self._audit(db, "install", "rejected", operation_id=operation["operation_id"] if operation else None,
                                code=getattr(exc, "code", "installation_io_error"))
                raise

    def recover(self):
        results = []
        with self._locked() as db:
            for row in db.execute("SELECT * FROM operations WHERE state IN ('prepared','staged') ORDER BY created_at").fetchall():
                operation = dict(row)
                try:
                    self._resume(db, operation)
                    results.append(self._receipt(db, operation))
                except Exception as exc:
                    code = getattr(exc, "code", "installation_io_error")
                    with db:
                        db.execute("UPDATE operations SET state='blocked',error_code=? WHERE operation_id=?", (code, operation["operation_id"]))
                        self._audit(db, "recover", "blocked", operation_id=operation["operation_id"], code=code)
                    results.append({"operation_id": operation["operation_id"], "state": "blocked", "code": code})
            return results

    def list(self):
        with self._locked() as db:
            return [dict(x) for x in db.execute("SELECT * FROM packages ORDER BY engine_id,version,package_digest")]

    def inspect(self, digest):
        with self._locked() as db:
            row = db.execute("SELECT * FROM packages WHERE package_digest=?", (digest,)).fetchone()
            if row is None:
                raise ArchiveError("repository_package_missing")
            manifest, payload = self._verify_tree(self._target(digest), digest)
            return {**dict(row), "manifest": manifest, "actual_payload_digest": payload, "activated": False, "lab_only": self.mode == "lab", "fixture_only":self.fixture_only}

    def status(self, request_id):
        with self._locked() as db:
            row = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ArchiveError("repository_request_missing")
            return {**dict(row), "activated": False, "lab_only": self.mode == "lab", "fixture_only":self.fixture_only}

    def audit(self):
        with self._locked() as db:
            rows = [dict(x) for x in db.execute("SELECT * FROM audit ORDER BY sequence")]
            previous = "0" * 64
            for sequence, row in enumerate(rows, 1):
                digest = hashlib.sha256((previous + "\n" + row["record"]).encode()).hexdigest()
                if row["sequence"] != sequence or row["previous_digest"] != previous or row["record_digest"] != digest:
                    raise ArchiveError("repository_audit_chain_invalid")
                previous = digest
            return rows


class LabRepository(EngineRepository):
    """Compatibility wrapper; test-only policies can never open a production root."""
    def __init__(self, root, *, trusted_keys, checkpoint=None):
        super().__init__(root, trusted_keys=trusted_keys, namespace="tmengine.test", channel="test",
                         mode="lab", checkpoint=checkpoint)
