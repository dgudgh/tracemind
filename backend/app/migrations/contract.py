"""Read-only migration contract and managed-hot readiness gate.

The migrator is the only role allowed to execute DDL in managed-hot deployments.
API/Worker processes call :func:`verify_schema_ready`, which never creates a table,
runs a revision or repairs an index.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect

from app.migrations import runner, runtime_schema
from app.migrations.initialization import BOOTSTRAP_REVISION
from app.migrations.locking import schema_lock
from app.migrations.policy import SchemaUpgradeRequired
from app.migrations.schema_contract import validate_schema

CONTRACT_VERSION = 1


@dataclass(frozen=True)
class MigrationContract:
    revision: str
    checksum: str
    source_checksum: str
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    bootstrap_pending: bool
    ready: bool

    def as_dict(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "revision": self.revision,
            "checksum": self.checksum,
            "source_checksum": self.source_checksum,
            "applied": list(self.applied),
            "pending": list(self.pending),
            "bootstrap_pending": self.bootstrap_pending,
            "ready": self.ready,
        }


def _revision_ids() -> list[str]:
    return [item.REVISION for item in (*runner.MIGRATIONS, runtime_schema)]


def _checksum(revisions: list[str]) -> str:
    payload = json.dumps(
        {"contract_version": CONTRACT_VERSION, "revisions": revisions},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_checksum() -> str:
    """Bind actual migrator bytes separately from the compatible revision-ID axis."""
    from app.migrations import initialization
    entries = []
    for module in (*runner.MIGRATIONS, runtime_schema, initialization):
        path = Path(module.__file__)
        entries.append({"module": module.__name__,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def contract_for(connection) -> MigrationContract:
    applied_set = runner._applied_revisions(connection)
    revisions = _revision_ids()
    applied = tuple(item for item in revisions if item in applied_set)
    pending = tuple(item for item in revisions if item not in applied_set)
    bootstrap_pending = BOOTSTRAP_REVISION not in applied_set
    return MigrationContract(
        revision=f"contract-{CONTRACT_VERSION}",
        checksum=_checksum(revisions),
        source_checksum=_source_checksum(),
        applied=applied,
        pending=pending,
        bootstrap_pending=bootstrap_pending,
        ready=not pending and not bootstrap_pending,
    )


def migration_status(bind) -> dict:
    """Return the contract without changing the database."""
    with schema_lock(bind) as connection:
        return contract_for(connection).as_dict()


def verify_schema_ready(bind, metadata=None) -> dict:
    """Fail closed when this process is not allowed to perform DDL and the schema is not ready."""
    if metadata is None:
        from app.core.database import Base
        metadata = Base.metadata
    with schema_lock(bind) as connection:
        contract = contract_for(connection)
        if not contract.ready:
            raise SchemaUpgradeRequired(
                "managed-hot 进程只允许校验结构；仍有未应用迁移，必须由专用 migrator 执行"
            )
        validate_schema(connection, metadata)
        return contract.as_dict()
