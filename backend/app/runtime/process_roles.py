"""Explicit process roles and the managed platform migration gate."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROLES = frozenset({"legacy", "api", "scheduler", "worker", "migrator"})


def process_role() -> str:
    role = os.environ.get("TRACEMIND_PROCESS_ROLE", "legacy").strip().lower()
    if role not in ROLES:
        raise ValueError(f"invalid TRACEMIND_PROCESS_ROLE: {role!r}")
    return role


def schema_may_migrate() -> bool:
    return process_role() in {"legacy", "migrator"}


def receipt_path() -> Path:
    value = os.environ.get("TRACEMIND_MIGRATION_RECEIPT", "").strip()
    if not value:
        raise ValueError("TRACEMIND_MIGRATION_RECEIPT is required for managed roles")
    return Path(value).expanduser().resolve()


def _target_identity(url: str) -> dict:
    from app.core.database_options import database_target_summary

    target = database_target_summary(url)
    return {key: target.get(key) for key in ("driver", "host", "port", "database")}


def write_migration_receipt(path: Path, contract: dict, database_url: str) -> dict:
    """Publish only a receipt for an already verified, fully applied schema."""
    if not contract.get("ready") or contract.get("pending") or contract.get("bootstrap_pending"):
        raise ValueError("migration contract is not ready")
    if not isinstance(contract.get("source_checksum"), str) or len(contract["source_checksum"]) != 64:
        raise ValueError("migration_source_checksum_missing")
    payload = {
        "format": 1,
        "revision": contract["revision"],
        "checksum": contract["checksum"],
        "source_checksum": contract["source_checksum"],
        "applied": contract["applied"],
        "target": _target_identity(database_url),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        from app.runtime.release_checkpoint import sync_directory
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def verify_migration_receipt(path: Path, contract: dict, database_url: str) -> dict:
    """Compare durable receipt against a fresh read of the actual database schema."""
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("migration_receipt_missing_or_invalid") from exc
    if not isinstance(receipt, dict) or receipt.get("format") != 1:
        raise ValueError("migration_receipt_invalid_format")
    expected = {
        "revision": contract.get("revision"),
        "checksum": contract.get("checksum"),
        "source_checksum": contract.get("source_checksum"),
        "applied": contract.get("applied"),
        "target": _target_identity(database_url),
    }
    if not contract.get("ready") or contract.get("pending") or contract.get("bootstrap_pending"):
        raise ValueError("migration_schema_not_ready")
    if not isinstance(contract.get("source_checksum"), str) or len(contract["source_checksum"]) != 64:
        raise ValueError("migration_source_checksum_missing")
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("migration_receipt_schema_mismatch")
    return receipt


def platform_readiness(bind, database_url: str) -> dict:
    from app.migrations.contract import verify_schema_ready

    contract = verify_schema_ready(bind)
    receipt = verify_migration_receipt(receipt_path(), contract, database_url)
    return {
        "status": "ready",
        "role": process_role(),
        "pid": os.getpid(),
        "generation": os.environ.get("TRACEMIND_PLATFORM_GENERATION", "").strip(),
        "artifact_digest": os.environ.get("TRACEMIND_PLATFORM_ARTIFACT_DIGEST", "").strip(),
        "migration": {"revision": receipt["revision"], "checksum": receipt["checksum"],
                      "source_checksum": receipt["source_checksum"]},
    }
