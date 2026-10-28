"""Durable fixture repository for trusted engine-host recovery tests.

This module owns a private SQLite database and never imports platform database
configuration. Mutations use fresh connections and ``BEGIN IMMEDIATE`` so
threads, store objects, and independent processes exercise the same guarantees.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class StoreError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class Package:
    engine_id: str
    digest: str
    version: str
    generation: int = 0  # compatibility only; routing generation is in active
    state: str = "candidate"
    payload_digest: str = ""
    runtime_digest: str = ""
    sdk_version: str = ""
    task_schema: str = "engine.task/v1"
    result_schema: str = "engine.result/v1"
    capability_version: str = "fixture/v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


class FixtureStore:
    """SQLite repository with immutable identities and fenced routing CAS."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 15_000):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms <= 0:
            raise StoreError("invalid_busy_timeout")
        self.conn = self._connect()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None,
                               timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS packages (
              engine_id TEXT NOT NULL, digest TEXT NOT NULL, version TEXT NOT NULL,
              generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
              payload_digest TEXT NOT NULL DEFAULT '', runtime_digest TEXT NOT NULL DEFAULT '',
              sdk_version TEXT NOT NULL DEFAULT '', task_schema TEXT NOT NULL DEFAULT 'engine.task/v1',
              result_schema TEXT NOT NULL DEFAULT 'engine.result/v1',
              capability_version TEXT NOT NULL DEFAULT 'fixture/v1',
              identity_json TEXT NOT NULL DEFAULT '{}', revoked INTEGER NOT NULL DEFAULT 0,
              quarantined INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (engine_id, digest)
            );
            -- Exactly three columns are retained for older fixture setup SQL.
            CREATE TABLE IF NOT EXISTS active (
              engine_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS route_meta (
              engine_id TEXT PRIMARY KEY, fence INTEGER NOT NULL DEFAULT 0,
              last_known_good_digest TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
              engine_id TEXT PRIMARY KEY, holder TEXT NOT NULL, token INTEGER NOT NULL,
              expires_at REAL NOT NULL, operation_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS updates (
              operation_id TEXT PRIMARY KEY, engine_id TEXT NOT NULL, kind TEXT NOT NULL,
              expected_generation INTEGER NOT NULL, expected_fence INTEGER NOT NULL,
              digest TEXT NOT NULL, state TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              request_hash TEXT NOT NULL, target_generation INTEGER, result_json TEXT,
              failure_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(engine_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS update_journal (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
              phase TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, operation_id TEXT, engine_id TEXT NOT NULL,
              generation INTEGER NOT NULL, digest TEXT NOT NULL,
              package_version TEXT NOT NULL DEFAULT '', payload_digest TEXT NOT NULL DEFAULT '',
              runtime_digest TEXT NOT NULL DEFAULT '', sdk_version TEXT NOT NULL DEFAULT '',
              task_schema TEXT NOT NULL DEFAULT 'engine.task/v1',
              result_schema TEXT NOT NULL DEFAULT 'engine.result/v1',
              capability_version TEXT NOT NULL DEFAULT 'fixture/v1',
              status TEXT NOT NULL, effect_state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
              fence INTEGER NOT NULL DEFAULT 0, deadline TEXT, authorization_json TEXT NOT NULL DEFAULT '{}',
              input_digest TEXT NOT NULL, request_hash TEXT NOT NULL DEFAULT '',
              input_json TEXT NOT NULL, payload TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
              task_id TEXT PRIMARY KEY, result_schema TEXT NOT NULL, status TEXT NOT NULL,
              effect_state TEXT NOT NULL, result_json TEXT NOT NULL, result_digest TEXT NOT NULL,
              generation INTEGER NOT NULL, digest TEXT NOT NULL, attempt INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_refs (
              ref_id TEXT PRIMARY KEY, engine_id TEXT NOT NULL, digest TEXT NOT NULL,
              task_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS launches (
              launch_instance_id TEXT PRIMARY KEY, engine_id TEXT NOT NULL,
              generation INTEGER NOT NULL, package_digest TEXT NOT NULL,
              runtime_digest TEXT NOT NULL DEFAULT '', pid INTEGER NOT NULL,
              start_time TEXT NOT NULL, pgid INTEGER NOT NULL, uid INTEGER NOT NULL,
              host_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              socket_path TEXT NOT NULL, ipc_root TEXT NOT NULL,
              staging_dir TEXT NOT NULL, workdir TEXT NOT NULL,
              members_json TEXT NOT NULL DEFAULT '[]',
              manifest_sha256 TEXT NOT NULL, provider_version TEXT NOT NULL,
              sdk_version TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
              recovery_state TEXT NOT NULL DEFAULT 'owned', created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_failures (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
              attempt INTEGER NOT NULL, error_code TEXT NOT NULL, category TEXT NOT NULL,
              retryable INTEGER NOT NULL, effect_state TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS launch_artifacts (
              artifact_id TEXT PRIMARY KEY, operation_id TEXT,
              launch_instance_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL,
              workdir TEXT NOT NULL, state TEXT NOT NULL, retained_reason TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_binding_idx
              ON tasks(engine_id, generation, digest, status);
            CREATE INDEX IF NOT EXISTS ref_digest_idx
              ON artifact_refs(engine_id, digest, state);
            CREATE INDEX IF NOT EXISTS journal_op_idx
              ON update_journal(operation_id, sequence);
            """
        )
        self._ensure_column("tasks", "request_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("launches", "members_json", "TEXT NOT NULL DEFAULT '[]'")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        self.conn.close()

    def _write(self, callback):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            value = callback(conn)
            conn.execute("COMMIT")
            return value
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _package_from(row: sqlite3.Row | None, *, route_generation: int | None = None) -> Package | None:
        if row is None:
            return None
        return Package(
            engine_id=row["engine_id"], digest=row["digest"], version=row["version"],
            generation=int(route_generation if route_generation is not None else row["generation"]),
            state=row["state"], payload_digest=row["payload_digest"],
            runtime_digest=row["runtime_digest"], sdk_version=row["sdk_version"],
            task_schema=row["task_schema"], result_schema=row["result_schema"],
            capability_version=row["capability_version"],
        )

    def add_package(self, package: Package, *, identity: dict | None = None) -> None:
        """Insert an immutable identity and never downgrade an existing lifecycle."""
        identity_value = {
            "engine_id": package.engine_id, "digest": package.digest, "version": package.version,
            "payload_digest": package.payload_digest, "runtime_digest": package.runtime_digest,
            "sdk_version": package.sdk_version, "task_schema": package.task_schema,
            "result_schema": package.result_schema,
            "capability_version": package.capability_version, **(identity or {}),
        }

        def write(conn):
            old = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                               (package.engine_id, package.digest)).fetchone()
            if old is not None:
                immutable = ("version", "payload_digest", "runtime_digest", "sdk_version",
                             "task_schema", "result_schema", "capability_version")
                for name in immutable:
                    incoming = getattr(package, name)
                    if incoming and old[name] and incoming != old[name]:
                        raise StoreError("package_identity_conflict", name)
                return
            conn.execute(
                "INSERT INTO packages(engine_id,digest,version,generation,state,payload_digest,"
                "runtime_digest,sdk_version,task_schema,result_schema,capability_version,"
                "identity_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (package.engine_id, package.digest, package.version, package.generation,
                 package.state, package.payload_digest, package.runtime_digest,
                 package.sdk_version, package.task_schema, package.result_schema,
                 package.capability_version, _canonical(identity_value), _utc_now()),
            )
        self._write(write)

    def package(self, engine_id: str, digest: str) -> Package | None:
        row = self.conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                                (engine_id, digest)).fetchone()
        return self._package_from(row)

    def discard_package_if_unreferenced(self, engine_id: str, digest: str) -> bool:
        """Remove only an unused candidate created by a failed prepare transaction."""
        def write(conn):
            package = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                                   (engine_id, digest)).fetchone()
            if package is None:
                return False
            active = conn.execute("SELECT 1 FROM active WHERE engine_id=? AND digest=?",
                                  (engine_id, digest)).fetchone()
            used = conn.execute("SELECT 1 FROM updates WHERE engine_id=? AND digest=? LIMIT 1",
                                (engine_id, digest)).fetchone()
            task = conn.execute("SELECT 1 FROM tasks WHERE engine_id=? AND digest=? LIMIT 1",
                                (engine_id, digest)).fetchone()
            if package["state"] != "candidate" or active or used or task:
                return False
            conn.execute("DELETE FROM packages WHERE engine_id=? AND digest=?", (engine_id, digest))
            return True
        return bool(self._write(write))

    def bootstrap_active(self, package: Package, *, generation: int = 1) -> Package:
        self.add_package(package)

        def write(conn):
            present = conn.execute("SELECT * FROM active WHERE engine_id=?", (package.engine_id,)).fetchone()
            if present is None:
                now = _utc_now()
                conn.execute("INSERT INTO active VALUES (?,?,?)",
                             (package.engine_id, generation, package.digest))
                conn.execute("INSERT OR IGNORE INTO route_meta VALUES (?,?,?,?)",
                             (package.engine_id, 1, package.digest, now))
            elif present["generation"] != generation or present["digest"] != package.digest:
                raise StoreError("active_already_initialized")
        self._write(write)
        active = self.active(package.engine_id)
        assert active is not None
        return active

    @staticmethod
    def _active(conn: sqlite3.Connection, engine_id: str) -> Package | None:
        row = conn.execute(
            "SELECT p.*,a.generation AS route_generation FROM active a JOIN packages p "
            "ON p.engine_id=a.engine_id AND p.digest=a.digest WHERE a.engine_id=?", (engine_id,)
        ).fetchone()
        return FixtureStore._package_from(row, route_generation=row["route_generation"] if row else None)

    def active(self, engine_id: str) -> Package | None:
        return self._active(self.conn, engine_id)

    def route(self, engine_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT a.engine_id,a.generation,a.digest,COALESCE(m.fence,0) AS fence,"
            "m.last_known_good_digest FROM active a LEFT JOIN route_meta m ON m.engine_id=a.engine_id "
            "WHERE a.engine_id=?", (engine_id,)).fetchone()
        return self._row(row)

    @staticmethod
    def _ensure_route_meta(conn: sqlite3.Connection, engine_id: str) -> sqlite3.Row:
        conn.execute("INSERT OR IGNORE INTO route_meta VALUES (?,?,?,?)",
                     (engine_id, 0, None, _utc_now()))
        return conn.execute("SELECT * FROM route_meta WHERE engine_id=?", (engine_id,)).fetchone()

    def start_update(self, *, engine_id: str, digest: str, expected_generation: int,
                     idempotency_key: str, expected_fence: int | None = None,
                     kind: str = "update", lease_seconds: float = 30.0,
                     request: dict | None = None) -> dict:
        if not engine_id or not digest or not idempotency_key or lease_seconds <= 0:
            raise StoreError("invalid_update_request")
        semantic = {"kind": kind, "engine_id": engine_id, "digest": digest,
                    "expected_generation": expected_generation,
                    "expected_fence": expected_fence, "request": request or {}}
        request_hash = _digest(semantic)

        def write(conn):
            prior = conn.execute("SELECT * FROM updates WHERE engine_id=? AND idempotency_key=?",
                                 (engine_id, idempotency_key)).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise StoreError("idempotency_conflict")
                return dict(prior)
            active = self._active(conn, engine_id)
            if active is None or active.generation != expected_generation:
                raise StoreError("stale_generation")
            package = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                                   (engine_id, digest)).fetchone()
            if package is None:
                raise StoreError("package_not_found")
            if package["revoked"] or package["quarantined"]:
                raise StoreError("package_untrusted")
            meta = self._ensure_route_meta(conn, engine_id)
            if expected_fence is not None and int(meta["fence"]) != expected_fence:
                raise StoreError("stale_fence")
            now_epoch = time.time()
            lease = conn.execute("SELECT * FROM leases WHERE engine_id=?", (engine_id,)).fetchone()
            if lease is not None and float(lease["expires_at"]) > now_epoch:
                raise StoreError("lease_busy")
            operation_id = ("rollback-" if kind == "rollback" else "op-") + uuid.uuid4().hex
            fence = int(meta["fence"]) + 1
            now = _utc_now()
            conn.execute("UPDATE route_meta SET fence=?,updated_at=? WHERE engine_id=?",
                         (fence, now, engine_id))
            conn.execute("INSERT OR REPLACE INTO leases VALUES (?,?,?,?,?)",
                         (engine_id, operation_id, fence, now_epoch + lease_seconds, operation_id))
            conn.execute(
                "INSERT INTO updates(operation_id,engine_id,kind,expected_generation,expected_fence,"
                "digest,state,idempotency_key,request_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, engine_id, kind, expected_generation, fence, digest, "prepared",
                 idempotency_key, request_hash, now, now),
            )
            self._journal(conn, operation_id, "prepared", semantic)
            return dict(conn.execute("SELECT * FROM updates WHERE operation_id=?",
                                     (operation_id,)).fetchone())
        return self._write(write)

    @staticmethod
    def _journal(conn, operation_id: str, phase: str, detail: dict | None = None) -> None:
        conn.execute("INSERT INTO update_journal(operation_id,phase,detail_json,created_at) VALUES (?,?,?,?)",
                     (operation_id, phase, _canonical(detail or {}), _utc_now()))

    def transition_update(self, operation_id: str, phase: str, *,
                          allowed_from: Iterable[str] | None = None,
                          detail: dict | None = None) -> dict:
        def write(conn):
            row = conn.execute("SELECT * FROM updates WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise StoreError("operation_not_found")
            if row["state"] == phase:
                return dict(row)
            if allowed_from is not None and row["state"] not in set(allowed_from):
                raise StoreError("invalid_update_transition", f"{row['state']}->{phase}")
            conn.execute("UPDATE updates SET state=?,updated_at=? WHERE operation_id=?",
                         (phase, _utc_now(), operation_id))
            self._journal(conn, operation_id, phase, detail)
            return dict(conn.execute("SELECT * FROM updates WHERE operation_id=?", (operation_id,)).fetchone())
        return self._write(write)

    def operation(self, operation_id: str) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM updates WHERE operation_id=?",
                                           (operation_id,)).fetchone())

    def journal(self, operation_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM update_journal WHERE operation_id=? ORDER BY sequence", (operation_id,))]

    def activate(self, operation_id: str) -> dict:
        """Fence-checked generation CAS; replay returns the original receipt."""
        def write(conn):
            row = conn.execute("SELECT * FROM updates WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise StoreError("operation_not_found")
            if row["target_generation"] is not None:
                return dict(row)
            if row["state"] != "candidate_ready":
                raise StoreError("operation_not_ready", row["state"])
            active = self._active(conn, row["engine_id"])
            if active is None or active.generation != row["expected_generation"]:
                raise StoreError("stale_generation")
            meta = self._ensure_route_meta(conn, row["engine_id"])
            if int(meta["fence"]) != int(row["expected_fence"]):
                raise StoreError("stale_fence")
            target = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                                  (row["engine_id"], row["digest"])).fetchone()
            if target is None:
                raise StoreError("package_not_found")
            if target["revoked"] or target["quarantined"]:
                raise StoreError("package_untrusted")
            if row["kind"] != "rollback" and row["digest"] == active.digest:
                raise StoreError("already_active")
            if row["kind"] == "rollback":
                safe = (meta["last_known_good_digest"] == row["digest"] or
                        target["state"] == "last_known_good")
                if not safe:
                    raise StoreError("no_safe_rollback")
            new_generation = active.generation + 1
            changed = conn.execute(
                "UPDATE active SET generation=?,digest=? WHERE engine_id=? AND generation=? AND digest=?",
                (new_generation, row["digest"], row["engine_id"],
                 row["expected_generation"], active.digest),
            )
            if changed.rowcount != 1:
                raise StoreError("stale_generation")
            last_good = active.digest if row["kind"] == "update" else meta["last_known_good_digest"]
            now = _utc_now()
            conn.execute("UPDATE route_meta SET last_known_good_digest=?,updated_at=? WHERE engine_id=?",
                         (last_good, now, row["engine_id"]))
            receipt = {"engine_id": row["engine_id"], "generation": new_generation,
                       "digest": row["digest"], "fence": row["expected_fence"],
                       "kind": row["kind"]}
            conn.execute("UPDATE updates SET state='active',target_generation=?,result_json=?,updated_at=? "
                         "WHERE operation_id=?",
                         (new_generation, _canonical(receipt), now, operation_id))
            conn.execute("DELETE FROM leases WHERE engine_id=? AND token=?",
                         (row["engine_id"], row["expected_fence"]))
            self._journal(conn, operation_id, "activate_generation", receipt)
            return dict(conn.execute("SELECT * FROM updates WHERE operation_id=?", (operation_id,)).fetchone())
        return self._write(write)

    def prepare_rollback(self, engine_id: str, digest: str, expected_generation: int,
                         *, idempotency_key: str | None = None,
                         expected_fence: int | None = None,
                         request: dict | None = None) -> dict:
        key = idempotency_key or f"rollback:{engine_id}:{expected_generation}:{digest}"
        return self.start_update(engine_id=engine_id, digest=digest,
                                 expected_generation=expected_generation,
                                 idempotency_key=key, expected_fence=expected_fence,
                                 kind="rollback", request=request)

    def rollback(self, engine_id: str, digest: str, expected_generation: int,
                 *, idempotency_key: str | None = None,
                 expected_fence: int | None = None) -> dict:
        """Compatibility alias that prepares but never activates a rollback."""
        return self.prepare_rollback(engine_id, digest, expected_generation,
                                     idempotency_key=idempotency_key,
                                     expected_fence=expected_fence)

    def prepare_task(self, *, task_id: str, engine_id: str, payload: dict,
                     operation_id: str | None = None, status: str = "queued",
                     effect_state: str = "none", attempt: int = 1,
                     deadline: str | None = None, authorization: dict | None = None,
                     generation: int | None = None, digest: str | None = None,
                     identity: dict | None = None) -> dict:
        if status not in {"queued", "running", "retry", "reconcile_required", "succeeded", "failed"}:
            raise StoreError("invalid_task_status")
        input_json = _canonical(payload)
        effective_deadline = deadline if deadline is not None else time.time() + 60
        request_semantic = {
            "engine_id": engine_id, "operation_id": operation_id, "payload": payload,
            "status": status, "effect_state": effect_state, "attempt": attempt,
            "deadline": ({"mode": "default"} if deadline is None else
                         {"mode": "explicit", "value": str(deadline)}),
            "authorization": authorization or {},
            # An omitted selector means "bind the route observed on first insert".
            # Keeping the selector omitted in this digest avoids a false conflict
            # when an identical replay occurs after a route change.
            "generation": generation, "digest": digest, "identity": identity or {},
        }
        request_hash = _digest(request_semantic)

        def write(conn):
            existing = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing is not None:
                if not existing["request_hash"] or existing["request_hash"] != request_hash:
                    raise StoreError("task_idempotency_conflict")
                return dict(existing)
            route = conn.execute(
                "SELECT a.generation,a.digest,COALESCE(m.fence,0) AS fence FROM active a "
                "LEFT JOIN route_meta m ON m.engine_id=a.engine_id WHERE a.engine_id=?", (engine_id,)
            ).fetchone()
            selected_generation = generation if generation is not None else (route["generation"] if route else None)
            selected_digest = digest if digest is not None else (route["digest"] if route else None)
            if selected_generation is None or selected_digest is None:
                raise StoreError("active_not_found")
            package = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                                   (engine_id, selected_digest)).fetchone()
            if package is None:
                raise StoreError("package_not_found")
            if package["revoked"]:
                raise StoreError("package_revoked")
            if package["quarantined"]:
                raise StoreError("package_quarantined")
            if package["state"] == "disabled":
                raise StoreError("package_disabled")
            fixed = dict(identity or {})
            checks = {
                "package_version": package["version"], "payload_digest": package["payload_digest"],
                "runtime_digest": package["runtime_digest"], "sdk_version": package["sdk_version"],
                "task_schema": package["task_schema"], "result_schema": package["result_schema"],
                "capability_version": package["capability_version"],
            }
            for name, actual in checks.items():
                if fixed.get(name) not in (None, "", actual):
                    raise StoreError("task_identity_mismatch", name)
                fixed[name] = actual
            fence = int(route["fence"] if route else 0)
            now = _utc_now()
            conn.execute(
                "INSERT INTO tasks(task_id,operation_id,engine_id,generation,digest,package_version,"
                "payload_digest,runtime_digest,sdk_version,task_schema,result_schema,capability_version,"
                "status,effect_state,attempt,fence,deadline,authorization_json,input_digest,request_hash,"
                "input_json,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, operation_id, engine_id, selected_generation, selected_digest,
                 fixed["package_version"], fixed["payload_digest"], fixed["runtime_digest"],
                 fixed["sdk_version"], fixed["task_schema"], fixed["result_schema"],
                 fixed["capability_version"], status, effect_state, attempt, fence, effective_deadline,
                 _canonical(authorization or {}), _digest(payload), request_hash,
                 input_json, input_json, now, now),
            )
            conn.execute("INSERT INTO artifact_refs VALUES (?,?,?,?,?,?,?)",
                         (f"task:{task_id}", engine_id, selected_digest, task_id,
                          "task_binding", "active", now))
            return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        return self._write(write)

    def add_task(self, *, task_id: str, engine_id: str, generation: int, digest: str,
                 payload: dict, effect_state: str = "none", **kwargs) -> None:
        self.prepare_task(task_id=task_id, engine_id=engine_id, generation=generation,
                          digest=digest, payload=payload, effect_state=effect_state, **kwargs)

    def task(self, task_id: str) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def tasks(self, *, engine_id: str | None = None, statuses: Iterable[str] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM tasks WHERE 1=1", []
        if engine_id is not None:
            sql += " AND engine_id=?"; params.append(engine_id)
        if statuses:
            values = tuple(statuses)
            sql += " AND status IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        return [dict(row) for row in self.conn.execute(sql + " ORDER BY created_at,task_id", params)]

    def set_task_status(self, task_id: str, *, status: str, effect_state: str | None = None,
                        expected_status: str | None = None) -> dict:
        def write(conn):
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise StoreError("task_not_found")
            if expected_status is not None and row["status"] != expected_status:
                raise StoreError("stale_task_status")
            next_effect = effect_state if effect_state is not None else row["effect_state"]
            receipt = conn.execute("SELECT status,effect_state FROM results WHERE task_id=?",
                                   (task_id,)).fetchone()
            if receipt is not None and (receipt["status"] != status or
                                        receipt["effect_state"] != next_effect):
                raise StoreError("terminal_result_immutable")
            conn.execute("UPDATE tasks SET status=?,effect_state=?,updated_at=? WHERE task_id=?",
                         (status, next_effect, _utc_now(), task_id))
            return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        return self._write(write)

    def record_task_failure(self, task_id: str, *, status: str, effect_state: str,
                            error_code: str, category: str = "runtime",
                            retryable: bool = False) -> dict:
        """Atomically persist the failure boundary before propagating it."""
        def write(conn):
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise StoreError("task_not_found")
            if conn.execute("SELECT 1 FROM results WHERE task_id=?", (task_id,)).fetchone():
                raise StoreError("terminal_result_immutable")
            conn.execute(
                "INSERT INTO task_failures(task_id,attempt,error_code,category,retryable,effect_state,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, row["attempt"], error_code, category, 1 if retryable else 0,
                 effect_state, _utc_now()),
            )
            conn.execute("UPDATE tasks SET status=?,effect_state=?,updated_at=? WHERE task_id=?",
                         (status, effect_state, _utc_now(), task_id))
            return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
        return self._write(write)

    def task_failures(self, task_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM task_failures WHERE task_id=? ORDER BY sequence", (task_id,))]

    def set_task_result(self, task_id: str, *, status: str, effect_state: str,
                        result: dict | None = None, reconcile: bool = False) -> None:
        value = result or {}
        result_json = _canonical(value)

        def write(conn):
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise StoreError("task_not_found")
            old = conn.execute("SELECT * FROM results WHERE task_id=?", (task_id,)).fetchone()
            result_digest = _digest(value)
            receipt = {
                "result_schema": task["result_schema"], "status": status,
                "effect_state": effect_state, "result_json": result_json,
                "result_digest": result_digest, "generation": task["generation"],
                "digest": task["digest"], "attempt": task["attempt"],
            }
            fields = tuple(receipt)
            if old is not None:
                if task["status"] != old["status"] or task["effect_state"] != old["effect_state"]:
                    raise StoreError("result_task_divergence")
                exact = all(old[name] == receipt[name] for name in fields)
                identity_same = all(old[name] == receipt[name] for name in
                                    ("result_schema", "generation", "digest", "attempt"))
                monotonic = (reconcile and identity_same and
                             old["effect_state"] in {"none", "unknown"} and
                             effect_state == "completed" and
                             old["status"] in {"failed", "reconcile_required"} and
                             status == "succeeded")
                if not exact and not monotonic:
                    raise StoreError("result_conflict")
                if monotonic:
                    conn.execute(
                        "UPDATE results SET status=?,effect_state=?,result_json=?,result_digest=? "
                        "WHERE task_id=?",
                        (status, effect_state, result_json, result_digest, task_id),
                    )
            else:
                conn.execute(
                    "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, receipt["result_schema"], status, effect_state, result_json,
                     result_digest, receipt["generation"], receipt["digest"],
                     receipt["attempt"], _utc_now()),
                )
            conn.execute("UPDATE tasks SET status=?,effect_state=?,updated_at=? WHERE task_id=?",
                         (status, effect_state, _utc_now(), task_id))
        self._write(write)

    def result(self, task_id: str) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM results WHERE task_id=?", (task_id,)).fetchone())

    def read_result(self, task_id: str) -> dict:
        """Decode the stable core schema without loading Provider code."""
        row = self.result(task_id)
        if row is None:
            raise StoreError("result_not_found")
        if row["result_schema"] != "engine.result/v1":
            raise StoreError("result_schema_unsupported")
        try:
            value = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StoreError("result_invalid") from exc
        if not isinstance(value, dict) or _digest(value) != row["result_digest"]:
            raise StoreError("result_digest_mismatch")
        return value

    def release_task_reference(self, task_id: str) -> None:
        self._write(lambda conn: conn.execute(
            "UPDATE artifact_refs SET state='released' WHERE task_id=?", (task_id,)))

    def references(self, engine_id: str, digest: str, *, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM artifact_refs WHERE engine_id=? AND digest=?"
        params: list[Any] = [engine_id, digest]
        if active_only:
            sql += " AND state='active'"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def generation_references(self, engine_id: str, generation: int, digest: str,
                              *, active_only: bool = True) -> list[dict]:
        sql = (
            "SELECT r.* FROM artifact_refs r JOIN tasks t ON t.task_id=r.task_id "
            "WHERE r.engine_id=? AND r.digest=? AND t.generation=?"
        )
        params: list[Any] = [engine_id, digest, generation]
        if active_only:
            sql += " AND r.state='active'"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def set_package_state(self, engine_id: str, digest: str, state: str) -> None:
        def write(conn):
            row = conn.execute("SELECT * FROM packages WHERE engine_id=? AND digest=?",
                               (engine_id, digest)).fetchone()
            if row is None:
                raise StoreError("package_not_found")
            conn.execute("UPDATE packages SET state=? WHERE engine_id=? AND digest=?",
                         (state, engine_id, digest))
            if state == "last_known_good":
                self._ensure_route_meta(conn, engine_id)
                conn.execute("UPDATE route_meta SET last_known_good_digest=?,updated_at=? WHERE engine_id=?",
                             (digest, _utc_now(), engine_id))
        self._write(write)

    def quarantine_package(self, engine_id: str, digest: str, *, revoked: bool = False) -> None:
        def write(conn):
            active = conn.execute("SELECT digest FROM active WHERE engine_id=?", (engine_id,)).fetchone()
            if active is not None and active["digest"] == digest:
                raise StoreError("cannot_quarantine_active")
            conn.execute("UPDATE packages SET quarantined=1,revoked=? WHERE engine_id=? AND digest=?",
                         (1 if revoked else 0, engine_id, digest))
        self._write(write)

    def can_collect_package(self, engine_id: str, digest: str) -> bool:
        route = self.route(engine_id)
        if route and digest in {route["digest"], route["last_known_good_digest"]}:
            return False
        if self.references(engine_id, digest):
            return False
        pending = self.conn.execute(
            "SELECT 1 FROM tasks WHERE engine_id=? AND digest=? AND status IN "
            "('queued','running','retry','reconcile_required') LIMIT 1", (engine_id, digest)).fetchone()
        return pending is None

    def save_launch(self, *, engine_id: str, generation: int, package_digest: str,
                    record: Any, workdir: str | Path | None = None,
                    state: str = "running") -> dict:
        value = asdict(record) if is_dataclass(record) else dict(record)
        required = ("pid", "launch_instance_id", "socket_path", "staging_dir",
                    "manifest_sha256", "provider_version")
        if any(value.get(name) in (None, "") for name in required):
            raise StoreError("launch_record_incomplete")
        pid = int(value["pid"])
        pgid = int(value.get("pgid") or os.getpgid(pid))
        start_time = str(value.get("start_time") or self._process_start_time(pid))
        now = _utc_now()
        launch = {
            "launch_instance_id": value["launch_instance_id"], "engine_id": engine_id,
            "generation": generation, "package_digest": package_digest,
            "runtime_digest": value.get("runtime_digest", ""), "pid": pid,
            "start_time": start_time, "pgid": pgid,
            "uid": int(value.get("uid") if value.get("uid") is not None else os.getuid()),
            "host_pid": int(value.get("host_pid") or os.getpid()),
            "owner_token": str(value.get("owner_token") or ""),
            "socket_path": str(value["socket_path"]),
            "ipc_root": str(value.get("ipc_root") or Path(value["socket_path"]).parent),
            "staging_dir": str(value["staging_dir"]),
            "workdir": str(Path(workdir or value.get("workdir") or
                                 Path(value["staging_dir"]).parents[1]).resolve()),
            "members_json": _canonical(value.get("members") or []),
            "manifest_sha256": value["manifest_sha256"],
            "provider_version": value["provider_version"],
            "sdk_version": value.get("sdk_version", ""), "state": state,
        }

        def write(conn):
            conn.execute(
                "INSERT INTO launches(launch_instance_id,engine_id,generation,package_digest,"
                "runtime_digest,pid,start_time,pgid,uid,host_pid,owner_token,socket_path,ipc_root,"
                "staging_dir,workdir,members_json,manifest_sha256,provider_version,sdk_version,"
                "state,recovery_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(launch_instance_id) DO UPDATE SET state=excluded.state,"
                "recovery_state=excluded.recovery_state,updated_at=excluded.updated_at",
                (launch["launch_instance_id"], launch["engine_id"], launch["generation"],
                 launch["package_digest"], launch["runtime_digest"], launch["pid"],
                 launch["start_time"], launch["pgid"], launch["uid"], launch["host_pid"],
                 launch["owner_token"], launch["socket_path"], launch["ipc_root"],
                 launch["staging_dir"], launch["workdir"], launch["members_json"],
                 launch["manifest_sha256"], launch["provider_version"], launch["sdk_version"],
                 launch["state"], "owned", now, now),
            )
            artifact_id = "stage:" + hashlib.sha256(launch["staging_dir"].encode()).hexdigest()
            conn.execute(
                "INSERT INTO launch_artifacts VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(artifact_id) DO UPDATE SET launch_instance_id=excluded.launch_instance_id,"
                "state='launched',retained_reason=NULL,updated_at=excluded.updated_at",
                (artifact_id, None, launch["launch_instance_id"], "runtime_snapshot",
                 launch["staging_dir"], launch["workdir"], "launched", None, now, now),
            )
        self._write(write)
        return self.launch(value["launch_instance_id"])

    def record_staged_artifact(self, *, operation_id: str | None, path: str | Path,
                               workdir: str | Path, state: str = "staged",
                               retained_reason: str | None = None,
                               launch_instance_id: str | None = None) -> dict:
        artifact_path = str(Path(path).resolve())
        artifact_id = "stage:" + hashlib.sha256(artifact_path.encode()).hexdigest()
        now = _utc_now()
        def write(conn):
            conn.execute(
                "INSERT INTO launch_artifacts VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(artifact_id) DO UPDATE SET operation_id=COALESCE("
                "excluded.operation_id,launch_artifacts.operation_id),launch_instance_id=COALESCE("
                "excluded.launch_instance_id,launch_artifacts.launch_instance_id),"
                "state=excluded.state,retained_reason=excluded.retained_reason,updated_at=excluded.updated_at",
                (artifact_id, operation_id, launch_instance_id, "runtime_snapshot", artifact_path,
                 str(Path(workdir).resolve()), state, retained_reason, now, now),
            )
            return dict(conn.execute("SELECT * FROM launch_artifacts WHERE artifact_id=?",
                                     (artifact_id,)).fetchone())
        return self._write(write)

    def mark_staged_artifact(self, *, operation_id: str | None = None,
                             launch_instance_id: str | None = None,
                             state: str, retained_reason: str) -> None:
        if operation_id is None and launch_instance_id is None:
            raise StoreError("artifact_identity_required")
        def write(conn):
            clauses, identities = [], []
            if operation_id is not None:
                clauses.append("operation_id=?"); identities.append(operation_id)
            if launch_instance_id is not None:
                clauses.append("launch_instance_id=?"); identities.append(launch_instance_id)
            conn.execute(
                f"UPDATE launch_artifacts SET state=?,retained_reason=?,updated_at=? WHERE " +
                " OR ".join(clauses),
                (state, retained_reason, _utc_now(), *identities),
            )
        self._write(write)

    def mark_unlaunched_staged_artifacts(self, operation_id: str, *, state: str,
                                         retained_reason: str) -> None:
        """Retain only pre-PID stages; never overwrite a recovered launch artifact."""
        self._write(lambda conn: conn.execute(
            "UPDATE launch_artifacts SET state=?,retained_reason=?,updated_at=? "
            "WHERE operation_id=? AND launch_instance_id IS NULL",
            (state, retained_reason, _utc_now(), operation_id)))

    def launch_artifacts(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM launch_artifacts ORDER BY created_at,artifact_id")]

    @staticmethod
    def _process_start_time(pid: int) -> str:
        try:
            import psutil
            return f"{psutil.Process(pid).create_time():.6f}"
        except Exception as exc:
            raise StoreError("process_start_time_unavailable", str(exc)) from exc

    def launch(self, launch_instance_id: str) -> dict | None:
        return self._row(self.conn.execute("SELECT * FROM launches WHERE launch_instance_id=?",
                                           (launch_instance_id,)).fetchone())

    def launches(self, *, states: Iterable[str] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM launches", []
        if states:
            values = tuple(states)
            sql += " WHERE state IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        return [dict(row) for row in self.conn.execute(sql + " ORDER BY created_at", params)]

    def mark_launch(self, launch_instance_id: str, *, state: str,
                    recovery_state: str | None = None) -> None:
        def write(conn):
            changed = conn.execute(
                "UPDATE launches SET state=?,recovery_state=COALESCE(?,recovery_state),updated_at=? "
                "WHERE launch_instance_id=?", (state, recovery_state, _utc_now(), launch_instance_id))
            if changed.rowcount != 1:
                raise StoreError("launch_not_found")
        self._write(write)

    def update_launch_members(self, launch_instance_id: str, members: list[dict] | tuple) -> None:
        """Persist the latest Supervisor-verified process identities for crash cleanup."""
        normalized = []
        for member in members:
            if not isinstance(member, dict):
                member = asdict(member) if is_dataclass(member) else dict(member)
            try:
                normalized.append({
                    "pid": int(member["pid"]), "start_time": float(member["start_time"]),
                    "uid": int(member["uid"]), "pgid": int(member["pgid"]),
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError("launch_member_identity_invalid") from exc
        payload = _canonical(sorted(normalized, key=lambda item: (item["pid"], item["start_time"])))

        def write(conn):
            changed = conn.execute(
                "UPDATE launches SET members_json=?,updated_at=? WHERE launch_instance_id=?",
                (payload, _utc_now(), launch_instance_id))
            if changed.rowcount != 1:
                raise StoreError("launch_not_found")
        self._write(write)

    def counts(self) -> dict:
        names = ("packages", "updates", "tasks", "results", "launches", "artifact_refs",
                 "task_failures", "launch_artifacts")
        return {name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in names}


class SyntheticEffectLedger:
    """Reader/helper for the SDK-owned provider effect database.

    Production fixture execution reaches this database only through Provider
    ``execute``/``verify`` RPC. Direct ``execute`` exists for repository-level
    crash tests and writes the exact SDK table schema.
    """

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS provider_effects("
            "operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,value TEXT NOT NULL,"
            "result TEXT NOT NULL,committed_at REAL NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS host_effect_receipts("
            "operation_id TEXT PRIMARY KEY,input_digest TEXT NOT NULL,result TEXT NOT NULL,"
            "recorded_at REAL NOT NULL)"
        )

    def close(self) -> None:
        self.conn.close()

    def execute(self, operation_id: str, input_value: dict) -> dict:
        input_digest = _digest(input_value)
        effect = {"operation_id": operation_id, "input_digest": input_digest,
                  "effect_state": "completed", "value": input_value}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM provider_effects WHERE operation_id=?",
                                    (operation_id,)).fetchone()
            if row is not None:
                if row["input_digest"] != input_digest:
                    raise StoreError("effect_idempotency_conflict")
                self.conn.execute("COMMIT")
                return dict(row)
            self.conn.execute("INSERT INTO provider_effects VALUES (?,?,?,?,?)",
                              (operation_id, input_digest, _canonical(input_value),
                               _canonical(effect), time.time()))
            self.conn.execute("COMMIT")
            return dict(self.conn.execute("SELECT * FROM provider_effects WHERE operation_id=?",
                                          (operation_id,)).fetchone())
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def verify(self, operation_id: str, input_value: dict) -> dict:
        row = self.conn.execute("SELECT * FROM provider_effects WHERE operation_id=?",
                                (operation_id,)).fetchone()
        if row is None:
            return {"verified": False, "state": "absent", "operation_id": operation_id}
        if row["input_digest"] != _digest(input_value):
            raise StoreError("effect_verify_identity_mismatch")
        return {"verified": True, "state": "committed", "operation_id": operation_id,
                "execute_count": 1, "effect": json.loads(row["result"])}

    def record_host_receipt(self, operation_id: str, input_digest: str, result: dict) -> None:
        self.conn.execute(
            "INSERT INTO host_effect_receipts VALUES (?,?,?,?) ON CONFLICT(operation_id) DO NOTHING",
            (operation_id, input_digest, _canonical(result), time.time()))

    def count(self, operation_id: str) -> int:
        row = self.conn.execute("SELECT 1 FROM provider_effects WHERE operation_id=?",
                                (operation_id,)).fetchone()
        return 1 if row else 0
