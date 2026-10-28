"""Public, independently signed production management handoff verification.

No private signing material or package supplied trust anchor is accepted here.
The deployment pins are loaded by the fixed, OS protected service assembly.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.runtime.engine_sdk.wire import canonical, strict_json
from .management import ManagementError

SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
MAX_RECEIPT = 1024 * 1024
BODY_FIELDS = frozenset({
    "schema", "namespace", "sequence", "issued_at", "expires_at",
    "e07_status", "handoff_id", "root_envelope_sha256", "trust_root_version",
    "admin_policy_digest", "service_uid", "admin_gid", "ops_person",
    "sec_person", "rel_person", "build_operator", "ops_custody_ref",
    "ops_recovery_ref", "sec_approval_ref", "rel_signing_request_ref",
    "clock_baseline_ref", "packages",
})
PACKAGE_FIELDS = frozenset({
    "package_digest", "tested_payload_digest", "production_payload_digest",
    "test_receipt_digest", "production_verifier_digest", "install_smoke_digest",
    "production_release_ready",
})


def _fail(code):
    raise ManagementError(code)


def _utc(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        _fail("production_admission_time_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail("production_admission_time_invalid")


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _private(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.is_symlink():
        _fail("production_admission_path_unsafe")
    st = path.lstat()
    if not path.is_file() or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o077:
        _fail("production_admission_path_unsafe")
    return path


class ProductionAdmissionVerifier:
    """Verify the exact OPS+SEC handoff and retain its monotonic high water mark."""

    def __init__(self, *, receipt_path, ledger_path, pins, pinned_root_sha256,
                 policy_digest, service_uid, admin_gid, clock_baseline):
        self.receipt_path = _private(receipt_path)
        self.ledger_path = Path(ledger_path)
        self.pins = pins
        self.pinned_root_sha256 = pinned_root_sha256
        self.policy_digest = policy_digest
        self.service_uid, self.admin_gid = service_uid, admin_gid
        self.clock_baseline = _utc(clock_baseline)
        if (type(pins) is not dict or set(pins) != {"OPS", "SEC"}
                or any(type(value) is not dict or set(value) != {"key_id", "public_key_hex"}
                       or type(value["key_id"]) is not str or NAME.fullmatch(value["key_id"]) is None
                       or type(value["public_key_hex"]) is not str or
                       not re.fullmatch(r"[0-9a-f]{64}", value["public_key_hex"])
                       for value in pins.values())
                or pins["OPS"]["public_key_hex"] == pins["SEC"]["public_key_hex"]
                or not _sha(pinned_root_sha256) or not _sha(policy_digest)
                or type(service_uid) is not int or type(admin_gid) is not int):
            _fail("production_admission_pins_invalid")
        self.pin_digest = "sha256:" + hashlib.sha256(canonical(pins)).hexdigest()
        if not self.ledger_path.is_absolute() or self.ledger_path.parent != self.ledger_path.parent.resolve():
            _fail("production_admission_ledger_unsafe")
        if self.ledger_path.exists():
            _private(self.ledger_path)

    def _verify_body(self, body, now):
        if type(body) is not dict or set(body) != BODY_FIELDS:
            _fail("production_admission_schema_invalid")
        if (body["schema"] != "tmengine.production-handoff/v1"
                or body["namespace"] != "tmengine.production"
                or body["e07_status"] != "production-trust-accepted"
                or type(body["sequence"]) is not int or body["sequence"] < 1
                or type(body["trust_root_version"]) is not int or body["trust_root_version"] < 1
                or body["root_envelope_sha256"] != self.pinned_root_sha256
                or body["admin_policy_digest"] != self.policy_digest
                or body["service_uid"] != self.service_uid or body["admin_gid"] != self.admin_gid):
            _fail("production_admission_binding_invalid")
        issued, expires = _utc(body["issued_at"]), _utc(body["expires_at"])
        if not self.clock_baseline <= issued <= now < expires or (expires - issued).days > 31:
            _fail("production_admission_expired_or_clock_invalid")
        for key in ("handoff_id", "ops_person", "sec_person", "rel_person", "build_operator",
                    "ops_custody_ref", "ops_recovery_ref", "sec_approval_ref",
                    "rel_signing_request_ref", "clock_baseline_ref"):
            if type(body[key]) is not str or NAME.fullmatch(body[key]) is None:
                _fail("production_admission_evidence_invalid")
        if len({body[key] for key in ("ops_person", "sec_person", "rel_person", "build_operator")}) != 4:
            _fail("production_admission_independence_invalid")
        packages = body["packages"]
        if type(packages) is not list or not packages or len(packages) > 4096:
            _fail("production_admission_packages_invalid")
        seen = set()
        for package in packages:
            if type(package) is not dict or set(package) != PACKAGE_FIELDS:
                _fail("production_admission_packages_invalid")
            for key in PACKAGE_FIELDS - {"production_release_ready"}:
                if not _sha(package[key]):
                    _fail("production_admission_packages_invalid")
            if (package["production_release_ready"] is not True
                    or package["tested_payload_digest"] != package["production_payload_digest"]
                    or package["package_digest"] in seen):
                _fail("production_admission_packages_invalid")
            seen.add(package["package_digest"])
        return packages

    def _verify_signatures(self, envelope):
        if type(envelope) is not dict or set(envelope) != {"signed", "signatures"}:
            _fail("production_admission_schema_invalid")
        signatures = envelope["signatures"]
        if type(signatures) is not list or len(signatures) != 2:
            _fail("production_admission_signatures_invalid")
        by_role = {}
        for item in signatures:
            if type(item) is not dict or set(item) != {"role", "key_id", "value"}:
                _fail("production_admission_signatures_invalid")
            if item["role"] not in self.pins or item["role"] in by_role:
                _fail("production_admission_signatures_invalid")
            by_role[item["role"]] = item
        try:
            message = canonical(envelope["signed"])
            for role, pin in self.pins.items():
                item = by_role[role]
                if item["key_id"] != pin["key_id"]:
                    _fail("production_admission_signer_mismatch")
                signature = base64.b64decode(item["value"], validate=True)
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin["public_key_hex"])).verify(signature, message)
        except (ValueError, TypeError, binascii.Error, InvalidSignature):
            _fail("production_admission_signature_invalid")

    def _checkpoint(self, *, sequence, receipt_digest, now):
        self.ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.ledger_path.exists():
            fd = os.open(self.ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        _private(self.ledger_path)
        with sqlite3.connect(self.ledger_path, timeout=15, isolation_level=None) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), "
                       "sequence INTEGER NOT NULL, receipt_digest TEXT NOT NULL, "
                       "pin_digest TEXT NOT NULL, last_seen TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT sequence,receipt_digest,pin_digest,last_seen "
                             "FROM checkpoint WHERE id=1").fetchone()
            if row and (row[2] != self.pin_digest or sequence < row[0]
                        or (sequence == row[0] and receipt_digest != row[1])
                        or now < _utc(row[3])):
                db.execute("ROLLBACK")
                _fail("production_admission_rollback")
            timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute("INSERT INTO checkpoint(id,sequence,receipt_digest,pin_digest,last_seen) "
                       "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence, "
                       "receipt_digest=excluded.receipt_digest,last_seen=excluded.last_seen",
                       (sequence, receipt_digest, self.pin_digest, timestamp))
            db.execute("COMMIT")

    def __call__(self, *, action, manifest, package_digest, payload_digest=None):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.receipt_path.open("rb") as stream:
            raw = stream.read(MAX_RECEIPT + 1)
        if len(raw) > MAX_RECEIPT:
            _fail("production_admission_receipt_too_large")
        envelope = strict_json(raw, max_bytes=MAX_RECEIPT)
        self._verify_signatures(envelope)
        packages = self._verify_body(envelope["signed"], now)
        body = envelope["signed"]
        receipt_digest = "sha256:" + hashlib.sha256(canonical(envelope)).hexdigest()
        self._checkpoint(sequence=body["sequence"], receipt_digest=receipt_digest, now=now)
        release = next((item for item in packages if item["package_digest"] == package_digest), None)
        if manifest is not None:
            # The signed handoff and the installed/archive manifest must bind
            # the same pre/post wrapping payload.
            if (release is None or payload_digest != release["production_payload_digest"]
                    or not _sha(payload_digest)):
                _fail("production_admission_package_binding_invalid")
        return {"schema": "tmengine.production-management-admission/v1", "accepted": True,
                "namespace": "tmengine.production", "fixture_only": False,
                "handoff_digest": receipt_digest, "e07_status": body["e07_status"],
                "package_digest": package_digest if release else None,
                "production_release_ready": release is not None,
                "admin_policy_digest": body["admin_policy_digest"],
                "trust_root_version": body["trust_root_version"],
                "service_uid": body["service_uid"], "admin_gid": body["admin_gid"]}
