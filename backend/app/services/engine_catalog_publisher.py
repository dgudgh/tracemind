"""Immutable read-only projection from a trusted manager and its live hosts."""
from dataclasses import replace
from threading import RLock
from uuid import uuid4

from app.engine_registry import Catalog
from app.services.engine_execution import READ_OPERATIONS


class CatalogPublisher:
    def __init__(self, manager):
        self.boot_id = uuid4().hex
        self._manager = manager
        self._lock = RLock()
        self._snapshot = Catalog.empty()

    @staticmethod
    def _mark(catalog, engine_id, reason):
        if catalog.get(engine_id) is None:
            return catalog
        return Catalog(catalog.generation, tuple(replace(e, available=False, reason=reason, capabilities=())
                       if e.engine_id == engine_id else e for e in catalog.entries))

    def refresh(self):
        with self._lock:
            manager = self._manager
            candidate = Catalog.empty()
            # A request thread must never reuse the manager's SQLite connection.
            conn = manager.store._connect()
            try:
                sql = ("SELECT a.engine_id,a.digest,a.generation,p.state,p.revoked,p.quarantined "
                       "FROM active a JOIN packages p ON p.engine_id=a.engine_id AND p.digest=a.digest ")
                routes = [dict(row) for row in conn.execute(sql + "ORDER BY a.engine_id").fetchall()]
                for route in routes:
                    engine_id, digest = route["engine_id"], route["digest"]
                    if route["revoked"] or route["quarantined"]:
                        candidate = self._mark(candidate, engine_id, "isolated")
                        continue
                    try:
                        loaded = candidate.register_installed(manager.repo, digest)
                        entry = loaded.get(engine_id)
                        if entry is None or entry.package_digest != digest:
                            continue
                        hosts = (*manager._hosts.values(), *manager._retained_hosts.values())
                        host = next((h for h in hosts if h.identity
                                     and h.identity.get("engine_id") == engine_id
                                     and h.identity.get("package_digest") == digest
                                     and h.identity.get("runtime_digest") == entry.runtime_digest
                                     and h.identity.get("sdk_version") == entry.sdk_version
                                     and h.identity.get("generation") == route["generation"]
                                     and h.state.value in {"ready", "active"}), None)
                        fixture = entry.fixture_only
                        reason = ("fixture_only" if fixture else "disabled" if route["state"] == "disabled"
                                  else None if host else "requires_runtime_validation")
                        mapped = tuple(cap for cap, operation in READ_OPERATIONS.items()
                                       if operation in entry.capabilities) if reason is None else ()
                        updated = replace(entry, capabilities=mapped, available=reason is None, reason=reason)
                        current = conn.execute(sql + "WHERE a.engine_id=?", (engine_id,)).fetchone()
                        if current is None or dict(current) != route:
                            continue
                        candidate = loaded.register(updated, verified=True)
                    except Exception:
                        candidate = self._mark(candidate, engine_id, "isolated")
            finally:
                conn.close()
            if candidate.entries != self._snapshot.entries:
                self._snapshot = Catalog(self._snapshot.generation + 1, candidate.entries)
            return self._snapshot
