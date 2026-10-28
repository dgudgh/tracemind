"""Local, fail-closed release checkpoints. Never stores commands or credentials."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


class CheckpointError(RuntimeError):
    """Persistence is uncertain; do not perform further runtime mutations."""


def sync_directory(path: Path) -> None:
    # Do not silently downgrade durability on filesystems/platforms without this
    # primitive. Such installations must use a supported local updater host.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_symlink():
            continue
        if child.is_dir():
            sync_tree(child)
        elif child.is_file():
            with child.open("rb") as handle:
                os.fsync(handle.fileno())
    sync_directory(path)


def durable_json(path: Path, value: object) -> None:
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    except OSError as exc:
        raise CheckpointError("checkpoint durability failed; inspect before recovery") from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class ReleaseCheckpoint:
    def __init__(self, root: Path) -> None:
        self.path = root / "release-transactions.json"

    def read(self) -> dict:
        try:
            if self.path.is_symlink():
                raise ValueError("symlink")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("format_version") != 1 or not isinstance(value.get("transactions"), list):
                raise ValueError("format")
            unfinished = 0
            for transaction in value["transactions"]:
                if (not isinstance(transaction, dict)
                        or not isinstance(transaction.get("events"), list)
                        or not isinstance(transaction.get("terminal"), bool)
                        or not isinstance(transaction.get("id"), str)
                        or not isinstance(transaction.get("facts"), dict)):
                    raise ValueError("transaction")
                unfinished += not transaction["terminal"]
            if unfinished > 1 or (unfinished and value["transactions"][-1]["terminal"]):
                raise ValueError("unfinished transaction order")
            return value
        except FileNotFoundError:
            return {"format_version": 1, "transactions": []}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CheckpointError("invalid release checkpoint; manual inspection required") from exc

    def current(self) -> dict | None:
        transactions = self.read()["transactions"]
        return transactions[-1] if transactions and not transactions[-1]["terminal"] else None

    def begin(self, facts: dict) -> dict:
        value = self.read()
        if self.current():
            raise CheckpointError("unfinished release transaction; use inspect/recover")
        transaction = {"id": uuid.uuid4().hex, "terminal": False, "facts": facts, "events": []}
        value["transactions"].append(transaction)
        durable_json(self.path, value)
        return transaction

    def record(self, phase: str, status: str, *, facts: dict | None = None, terminal: bool = False) -> dict:
        value = copy.deepcopy(self.read())
        if not value["transactions"] or value["transactions"][-1]["terminal"]:
            raise CheckpointError("no unfinished release transaction")
        transaction = value["transactions"][-1]
        transaction["facts"].update(facts or {})
        transaction["events"].append({
            "phase": phase, "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        transaction["terminal"] = terminal
        durable_json(self.path, value)
        return transaction
