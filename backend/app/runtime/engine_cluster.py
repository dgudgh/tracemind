"""Single-site durable membership and fenced dispatch for real Engine Supervisors.

SQLite is a local software test ledger. A real multi-host deployment needs a
shared transactional DB and authenticated Host transport; SQLite on NFS is not
accepted as that deployment evidence.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

import psutil

from app.runtime.execution_identity import validate_identity
from app.runtime.engine_host.store import FixtureStore, Package
from app.runtime.engine_host.update import FixtureUpdater


class ClusterRefused(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class EngineCluster:
    def __init__(self, path: Path, store_path: Path, *, core_release: str = "fixture-core", lease_seconds=1.5):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path = Path(store_path).resolve()
        self.core_release = core_release
        self.lease_seconds = lease_seconds
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _init(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS cluster_meta (
              engine_id TEXT PRIMARY KEY, membership_epoch INTEGER NOT NULL,
              route_generation INTEGER NOT NULL, active_identity_json TEXT NOT NULL,
              node_snapshot_json TEXT NOT NULL, switch_pending INTEGER NOT NULL DEFAULT 0,
              switch_target_json TEXT
            );
            CREATE TABLE IF NOT EXISTS cluster_nodes (
              node_id TEXT PRIMARY KEY, engine_id TEXT NOT NULL, launch_id TEXT NOT NULL,
              host_pid INTEGER NOT NULL, provider_pid INTEGER NOT NULL,
              provider_created_at REAL NOT NULL, identity_json TEXT NOT NULL,
              generation INTEGER NOT NULL, capacity INTEGER NOT NULL,
              expires_at REAL NOT NULL, state TEXT NOT NULL,
              membership_epoch INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cluster_tasks (
              task_id TEXT PRIMARY KEY, engine_id TEXT NOT NULL, identity_json TEXT NOT NULL,
              generation INTEGER NOT NULL, method TEXT NOT NULL, params_json TEXT NOT NULL,
              operation_id TEXT NOT NULL, state TEXT NOT NULL, effect_state TEXT NOT NULL,
              node_id TEXT, launch_id TEXT, claim_token INTEGER NOT NULL DEFAULT 0,
              claim_epoch INTEGER, expires_at REAL, result_json TEXT, attempt INTEGER NOT NULL DEFAULT 0
            );
            """)

    def _identity(self, package: Package) -> dict:
        return dict(validate_identity({
            "core_release": self.core_release, "engine_id": package.engine_id,
            "package_digest": package.digest, "runtime_digest": package.runtime_digest,
            "sdk_version": package.sdk_version, "task_schema": package.task_schema,
            "result_schema": package.result_schema,
        }))

    def _identity_from_supervisor(self, signed_package, host_identity: dict) -> dict:
        verified = signed_package.verify()
        if (host_identity.get("engine_id") != signed_package.manifest["engine_id"] or
                host_identity.get("package_digest") != signed_package.digest or
                host_identity.get("payload_digest") != verified["payload_digest"] or
                host_identity.get("runtime_digest") != verified["runtime_digest"] or
                host_identity.get("sdk_version") != verified["sdk_version"] or
                not host_identity.get("launch_instance_id")):
            raise ClusterRefused("supervisor_signed_identity_mismatch")
        package = Package(signed_package.manifest["engine_id"], signed_package.digest,
                          signed_package.version, generation=host_identity["generation"],
                          payload_digest=verified["payload_digest"],
                          runtime_digest=verified["runtime_digest"],
                          sdk_version=verified["sdk_version"])
        return self._identity(package)

    def bootstrap(self, signed_package) -> dict:
        verified = signed_package.verify()
        package = Package(signed_package.manifest["engine_id"], signed_package.digest,
                          signed_package.version, generation=1, state="active",
                          payload_digest=verified["payload_digest"],
                          runtime_digest=verified["runtime_digest"],
                          sdk_version=verified["sdk_version"])
        store = FixtureStore(self.store_path)
        try:
            store.bootstrap_active(package, generation=1)
        finally:
            store.close()
        identity = self._identity(package)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (package.engine_id,)).fetchone()
            if existing and (existing["route_generation"] != 1 or
                             json.loads(existing["active_identity_json"]) != identity):
                raise ClusterRefused("cluster_already_bootstrapped_differently")
            db.execute("INSERT OR IGNORE INTO cluster_meta VALUES(?,?,?,?,?,0,NULL)",
                       (package.engine_id, 0, 1, _json(identity), "[]"))
        return identity

    @staticmethod
    def _probe(probe: Callable[[], dict], expected: dict, generation: int) -> dict:
        evidence = probe()
        if not isinstance(evidence, dict):
            raise ClusterRefused("host_probe_evidence_missing")
        required = {"host_pid", "provider_pid", "provider_created_at", "launch_id",
                    "identity", "generation", "provider_heartbeat", "host_state"}
        if (not required <= set(evidence) or not isinstance(evidence["provider_heartbeat"], dict)
                or evidence["host_state"] not in {"ready", "active"}
                or evidence["generation"] != generation
                or evidence["identity"] != expected
                or evidence["provider_heartbeat"].get("launch_instance_id") != evidence["launch_id"]):
            raise ClusterRefused("host_probe_identity_mismatch")
        try:
            host = psutil.Process(evidence["host_pid"])
            provider = psutil.Process(evidence["provider_pid"])
            if (not host.is_running() or not provider.is_running() or
                    abs(provider.create_time() - evidence["provider_created_at"]) > .05 or
                    provider.ppid() != evidence["host_pid"]):
                raise ClusterRefused("host_probe_os_identity_mismatch")
        except (psutil.Error, KeyError, TypeError) as exc:
            raise ClusterRefused("host_probe_os_identity_missing") from exc
        return evidence

    def _snapshot(self, db, engine_id: str) -> int:
        rows = db.execute("SELECT node_id,launch_id,identity_json,generation,capacity FROM cluster_nodes "
                          "WHERE engine_id=? AND state='ready' AND expires_at>? ORDER BY node_id",
                          (engine_id, time.time())).fetchall()
        current = db.execute("SELECT membership_epoch FROM cluster_meta WHERE engine_id=?",
                             (engine_id,)).fetchone()
        if current is None:
            raise ClusterRefused("cluster_not_bootstrapped")
        epoch = current[0] + 1
        snapshot = [{"node_id": row["node_id"], "launch_id": row["launch_id"],
                     "identity": json.loads(row["identity_json"]),
                     "generation": row["generation"], "capacity": row["capacity"]}
                    for row in rows]
        db.execute("UPDATE cluster_meta SET membership_epoch=?,node_snapshot_json=? WHERE engine_id=?",
                   (epoch, _json(snapshot), engine_id))
        db.execute("UPDATE cluster_nodes SET membership_epoch=? WHERE engine_id=? AND state='ready'",
                   (epoch, engine_id))
        # An in-flight result from the prior membership snapshot must be
        # reconciled. Read-only calls may be requeued with their pinned identity.
        active = db.execute("SELECT task_id,method FROM cluster_tasks WHERE engine_id=? AND state='claimed'",
                            (engine_id,)).fetchall()
        for task in active:
            if task["method"] == "invoke_read":
                db.execute("UPDATE cluster_tasks SET state='queued',node_id=NULL,launch_id=NULL,"
                           "claim_epoch=NULL,expires_at=NULL,claim_token=claim_token+1 WHERE task_id=?",
                           (task["task_id"],))
            else:
                db.execute("UPDATE cluster_tasks SET state='reconcile_required',effect_state='unknown',"
                           "claim_token=claim_token+1 WHERE task_id=?", (task["task_id"],))
        return epoch

    def join(self, node_id: str, signed_package, generation: int, capacity: int,
             probe: Callable[[], dict]) -> dict:
        if not node_id or not isinstance(capacity, int) or not 0 < capacity <= 8:
            raise ClusterRefused("invalid_node_capacity")
        verified = signed_package.verify()
        package = Package(signed_package.manifest["engine_id"], signed_package.digest,
                          signed_package.version, generation=generation, state="candidate",
                          payload_digest=verified["payload_digest"],
                          runtime_digest=verified["runtime_digest"],
                          sdk_version=verified["sdk_version"])
        identity = self._identity(package)
        evidence = self._probe(probe, identity, generation)
        store = FixtureStore(self.store_path)
        try:
            store.add_package(package)
        finally:
            store.close()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM cluster_nodes WHERE node_id=?", (node_id,)).fetchone()
            if old and old["state"] == "ready" and old["expires_at"] > time.time():
                raise ClusterRefused("node_id_already_active")
            db.execute("INSERT INTO cluster_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(node_id) DO UPDATE SET engine_id=excluded.engine_id,"
                       "launch_id=excluded.launch_id,host_pid=excluded.host_pid,"
                       "provider_pid=excluded.provider_pid,provider_created_at=excluded.provider_created_at,"
                       "identity_json=excluded.identity_json,generation=excluded.generation,"
                       "capacity=excluded.capacity,expires_at=excluded.expires_at,state='ready',"
                       "membership_epoch=excluded.membership_epoch",
                       (node_id, package.engine_id, evidence["launch_id"], evidence["host_pid"],
                        evidence["provider_pid"], evidence["provider_created_at"],
                        _json(identity), generation, capacity, time.time() + self.lease_seconds,
                        "ready", 0))
            epoch = self._snapshot(db, package.engine_id)
        return {"node_id": node_id, "launch_id": evidence["launch_id"],
                "membership_epoch": epoch, "identity": identity}

    def heartbeat(self, node_id: str, launch_id: str, probe: Callable[[], dict]) -> int:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cluster_nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None or row["launch_id"] != launch_id or row["state"] != "ready":
            raise ClusterRefused("node_launch_stale")
        self._probe(probe, json.loads(row["identity_json"]), row["generation"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE cluster_nodes SET expires_at=? WHERE node_id=? AND launch_id=? "
                                 "AND state='ready' AND expires_at>?",
                                 (time.time() + self.lease_seconds, node_id, launch_id,
                                  time.time())).rowcount
            if changed != 1:
                raise ClusterRefused("node_lease_expired")
            return db.execute("SELECT membership_epoch FROM cluster_meta WHERE engine_id=?",
                              (row["engine_id"],)).fetchone()[0]

    def expire(self, engine_id: str) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = [row[0] for row in db.execute(
                "SELECT node_id FROM cluster_nodes WHERE engine_id=? AND state='ready' AND expires_at<=?",
                (engine_id, time.time())).fetchall()]
            if expired:
                db.execute("UPDATE cluster_nodes SET state='expired' WHERE engine_id=? AND state='ready' "
                           "AND expires_at<=?", (engine_id, time.time()))
                self._snapshot(db, engine_id)
        return expired

    def leave(self, node_id: str, launch_id: str) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT engine_id FROM cluster_nodes WHERE node_id=? AND launch_id=? "
                             "AND state='ready'", (node_id, launch_id)).fetchone()
            if row is None:
                raise ClusterRefused("node_launch_stale")
            db.execute("UPDATE cluster_nodes SET state='left',expires_at=0 WHERE node_id=?", (node_id,))
            return self._snapshot(db, row[0])

    def snapshot(self, engine_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (engine_id,)).fetchone()
            if row is None:
                raise ClusterRefused("cluster_not_bootstrapped")
            return {"membership_epoch": row["membership_epoch"],
                    "route_generation": row["route_generation"],
                    "active_identity": json.loads(row["active_identity_json"]),
                    "nodes": json.loads(row["node_snapshot_json"]),
                    "switch_pending": bool(row["switch_pending"]),
                    "switch_target": json.loads(row["switch_target_json"]) if row["switch_target_json"] else None}

    def task(self, task_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cluster_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            for key in ("identity_json", "params_json", "result_json"):
                if value[key] is not None:
                    value[key[:-5] if key.endswith("_json") else key] = json.loads(value[key])
            return value

    def switch_active(self, engine_id: str, signed_package, *, expected_generation: int) -> dict:
        verified = signed_package.verify()
        package = Package(engine_id, signed_package.digest, signed_package.version,
                          generation=expected_generation + 1, state="candidate",
                          payload_digest=verified["payload_digest"],
                          runtime_digest=verified["runtime_digest"],
                          sdk_version=verified["sdk_version"])
        target_identity = self._identity(package)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (engine_id,)).fetchone()
            if meta is None or meta["route_generation"] != expected_generation or meta["switch_pending"]:
                raise ClusterRefused("stale_cluster_generation")
            ready = db.execute("SELECT COUNT(*) FROM cluster_nodes WHERE engine_id=? AND state='ready' "
                               "AND expires_at>? AND generation=? AND identity_json=?",
                               (engine_id, time.time(), expected_generation + 1,
                                _json(target_identity))).fetchone()[0]
            if ready < 1:
                raise ClusterRefused("matching_ready_node_missing")
            db.execute("UPDATE cluster_meta SET switch_pending=1,switch_target_json=? WHERE engine_id=?",
                       (_json(target_identity), engine_id))
            membership_epoch = meta["membership_epoch"]
        store = FixtureStore(self.store_path)
        try:
            route = store.route(engine_id)
            if route is None or route["generation"] != expected_generation:
                raise ClusterRefused("host_route_generation_mismatch")
            updater = FixtureUpdater(store)
            started = updater.prepare(
                engine_id=engine_id, digest=package.digest,
                expected_generation=expected_generation, expected_fence=route["fence"],
                idempotency_key=f"cluster-{engine_id}-{expected_generation}-{package.digest}",
                version=package.version, generation=expected_generation + 1,
                payload_digest=package.payload_digest, runtime_digest=package.runtime_digest,
                sdk_version=package.sdk_version, task_schema=package.task_schema,
                result_schema=package.result_schema, identity=target_identity,
            )
            op = started.operation_id
            if store.operation(op)["state"] == "prepared":
                updater.verified(op, {"package_digest": package.digest})
                updater.staged(op, {"package_digest": package.digest})
                updater.candidate_started(op, {"membership_epoch": membership_epoch})
                updater.handshake_complete(op, {"member_count": ready})
                updater.selftest_complete(op, {"ok": True, "member_count": ready})
                updater.candidate_ready(op, capacity_receipt={"admitted": True,
                                                                "membership_epoch": membership_epoch,
                                                                "ready_matching_nodes": ready,
                                                                "fixture_only": True})
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT membership_epoch,route_generation FROM cluster_meta "
                                     "WHERE engine_id=?", (engine_id,)).fetchone()
                still_ready = db.execute("SELECT COUNT(*) FROM cluster_nodes WHERE engine_id=? "
                                         "AND state='ready' AND expires_at>? AND generation=? "
                                         "AND identity_json=? AND membership_epoch=?",
                                         (engine_id, time.time(), expected_generation + 1,
                                          _json(target_identity), membership_epoch)).fetchone()[0]
                if (current["membership_epoch"] != membership_epoch or
                        current["route_generation"] != expected_generation or still_ready < 1):
                    raise ClusterRefused("matching_ready_node_lost_before_activation")
                receipt = updater.activate(op)
                if receipt.generation != expected_generation + 1:
                    raise ClusterRefused("host_route_activation_mismatch")
                changed = db.execute("UPDATE cluster_meta SET route_generation=?,active_identity_json=?,"
                                     "switch_pending=0,switch_target_json=NULL WHERE engine_id=? "
                                     "AND route_generation=? AND switch_pending=1 AND membership_epoch=?",
                                     (expected_generation + 1, _json(target_identity), engine_id,
                                      expected_generation, membership_epoch)).rowcount
                if changed != 1:
                    raise ClusterRefused("cluster_route_commit_uncertain")
        finally:
            store.close()
        return {"generation": expected_generation + 1, "identity": target_identity,
                "membership_epoch": membership_epoch}

    def recover_switch(self, engine_id: str) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (engine_id,)).fetchone()
            if meta is None or not meta["switch_pending"]:
                raise ClusterRefused("no_pending_cluster_switch")
            target = json.loads(meta["switch_target_json"])
            store = FixtureStore(self.store_path)
            try:
                route = store.route(engine_id)
            finally:
                store.close()
            if route["generation"] == meta["route_generation"]:
                db.execute("UPDATE cluster_meta SET switch_pending=0,switch_target_json=NULL "
                           "WHERE engine_id=?", (engine_id,))
                return {"state": "not_activated", "generation": route["generation"]}
            if (route["generation"] != meta["route_generation"] + 1 or
                    route["digest"] != target["package_digest"]):
                raise ClusterRefused("host_route_diverged_from_cluster_intent")
            matching = db.execute("SELECT COUNT(*) FROM cluster_nodes WHERE engine_id=? AND "
                                  "state='ready' AND expires_at>? AND generation=? AND "
                                  "identity_json=? AND membership_epoch=?",
                                  (engine_id, time.time(), route["generation"], _json(target),
                                   meta["membership_epoch"])).fetchone()[0]
            if matching < 1:
                raise ClusterRefused("recovery_matching_ready_node_missing")
            db.execute("UPDATE cluster_meta SET route_generation=?,active_identity_json=?,"
                       "switch_pending=0,switch_target_json=NULL WHERE engine_id=?",
                       (route["generation"], _json(target), engine_id))
            return {"state": "reconciled", "generation": route["generation"]}

    def enqueue(self, task_id: str, engine_id: str, method: str, params: dict,
                *, operation_id: str | None = None) -> dict:
        if method not in {"invoke_read", "execute"} or not isinstance(params, dict):
            raise ClusterRefused("task_method_invalid")
        existing = self.task(task_id)
        if existing is not None:
            if (existing["engine_id"] != engine_id or existing["method"] != method or
                    existing["params"] != params or
                    (operation_id is not None and existing["operation_id"] != operation_id)):
                raise ClusterRefused("task_idempotency_conflict")
            return existing
        operation_id = operation_id or task_id
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (engine_id,)).fetchone()
            if meta is None or meta["switch_pending"]:
                raise ClusterRefused("route_switch_pending_or_missing")
            identity = json.loads(meta["active_identity_json"])
            matching = db.execute("SELECT COUNT(*) FROM cluster_nodes WHERE engine_id=? AND "
                                  "state='ready' AND expires_at>? AND generation=? AND "
                                  "identity_json=? AND membership_epoch=? AND capacity>0",
                                  (engine_id, time.time(), meta["route_generation"],
                                   _json(identity), meta["membership_epoch"])).fetchone()[0]
            if matching < 1:
                raise ClusterRefused("matching_ready_node_missing")
            old = db.execute("SELECT * FROM cluster_tasks WHERE task_id=?", (task_id,)).fetchone()
            if old:
                if (old["engine_id"] != engine_id or old["method"] != method or
                        json.loads(old["params_json"]) != params or old["operation_id"] != operation_id):
                    raise ClusterRefused("task_idempotency_conflict")
            else:
                store = FixtureStore(self.store_path)
                try:
                    store.prepare_task(task_id=task_id, engine_id=engine_id,
                                       payload={"method": method, "params": params},
                                       operation_id=operation_id, generation=meta["route_generation"],
                                       digest=identity["package_digest"])
                finally:
                    store.close()
                db.execute("INSERT INTO cluster_tasks(task_id,engine_id,identity_json,generation,method,"
                           "params_json,operation_id,state,effect_state) VALUES(?,?,?,?,?,?,?,?,?)",
                           (task_id, engine_id, _json(identity), meta["route_generation"],
                            method, _json(params), operation_id, "queued", "none"))
        return self.task(task_id)

    def claim(self, node_id: str, launch_id: str) -> dict | None:
        claimed_id = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            node = db.execute("SELECT * FROM cluster_nodes WHERE node_id=?", (node_id,)).fetchone()
            if node is None or node["state"] != "ready" or node["launch_id"] != launch_id or node["expires_at"] <= time.time():
                raise ClusterRefused("node_lease_or_launch_stale")
            meta = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?", (node["engine_id"],)).fetchone()
            if meta["switch_pending"] or node["membership_epoch"] != meta["membership_epoch"]:
                raise ClusterRefused("membership_snapshot_stale")
            used = db.execute("SELECT COUNT(*) FROM cluster_tasks WHERE node_id=? AND state='claimed'",
                              (node_id,)).fetchone()[0]
            if used >= node["capacity"]:
                return None
            task = db.execute("SELECT * FROM cluster_tasks WHERE engine_id=? AND state='queued' "
                              "AND generation=? AND identity_json=? ORDER BY rowid LIMIT 1",
                              (node["engine_id"], node["generation"], node["identity_json"])).fetchone()
            if task is None:
                return None
            token = task["claim_token"] + 1
            db.execute("UPDATE cluster_tasks SET state='claimed',node_id=?,launch_id=?,"
                       "claim_token=?,claim_epoch=?,expires_at=?,attempt=attempt+1 WHERE task_id=?",
                       (node_id, launch_id, token, meta["membership_epoch"],
                        time.time() + self.lease_seconds, task["task_id"]))
            claimed_id = task["task_id"]
        return self.task(claimed_id)

    def _claim_current(self, db, task_id, node_id, launch_id, token, epoch):
        row = db.execute("SELECT * FROM cluster_tasks WHERE task_id=?", (task_id,)).fetchone()
        node = db.execute("SELECT * FROM cluster_nodes WHERE node_id=?", (node_id,)).fetchone()
        meta = db.execute("SELECT * FROM cluster_meta WHERE engine_id=?",
                          (row["engine_id"],)).fetchone() if row else None
        if (row is None or node is None or meta is None or row["state"] != "claimed" or
                row["node_id"] != node_id or row["launch_id"] != launch_id or
                row["claim_token"] != token or row["claim_epoch"] != epoch or
                row["expires_at"] <= time.time() or node["state"] != "ready" or
                node["launch_id"] != launch_id or node["expires_at"] <= time.time() or
                meta["membership_epoch"] != epoch or node["membership_epoch"] != epoch or
                row["identity_json"] != node["identity_json"] or
                row["generation"] != node["generation"]):
            raise ClusterRefused("claim_fence_stale")
        return row

    def renew_claim(self, task_id, node_id, launch_id, token, epoch):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._claim_current(db, task_id, node_id, launch_id, token, epoch)
            db.execute("UPDATE cluster_tasks SET expires_at=? WHERE task_id=?",
                       (time.time() + self.lease_seconds, task_id))

    def commit_result(self, task_id, node_id, launch_id, token, epoch,
                      *, result: dict | None, effect_state: str, error_code: str = "") -> dict:
        uncertain = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._claim_current(db, task_id, node_id, launch_id, token, epoch)
            if effect_state not in {"none", "completed", "unknown"}:
                raise ClusterRefused("effect_state_invalid")
            if task["method"] == "execute" and effect_state != "completed":
                db.execute("UPDATE cluster_tasks SET state='reconcile_required',effect_state='unknown',"
                           "result_json=? WHERE task_id=?",
                           (_json({"error_code": error_code or "effect_unknown"}), task_id))
                uncertain = True
            else:
                if result is None or not isinstance(result, dict):
                    raise ClusterRefused("result_missing")
                db.execute("UPDATE cluster_tasks SET state='receipt_pending',effect_state=?,result_json=? "
                           "WHERE task_id=?", (effect_state, _json(result), task_id))
        if uncertain:
            return self.task(task_id)
        self.finalize_receipt(task_id)
        return self.task(task_id)

    def finalize_receipt(self, task_id: str) -> dict:
        task = self.task(task_id)
        if task is None or task["state"] != "receipt_pending":
            raise ClusterRefused("receipt_not_pending")
        store = FixtureStore(self.store_path)
        try:
            store.set_task_result(task_id, status="succeeded", effect_state=task["effect_state"],
                                  result=task["result"])
            readback = store.read_result(task_id)
            if readback != task["result"]:
                raise ClusterRefused("store_receipt_readback_mismatch")
        finally:
            store.close()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE cluster_tasks SET state='succeeded' WHERE task_id=? "
                       "AND state='receipt_pending' AND result_json=?",
                       (task_id, _json(task["result"])))
        return self.task(task_id)

    def reconcile_receipts(self):
        with self._connect() as db:
            pending = [row[0] for row in db.execute("SELECT task_id FROM cluster_tasks "
                                                    "WHERE state='receipt_pending'")]
        return [self.finalize_receipt(task_id) for task_id in pending]
