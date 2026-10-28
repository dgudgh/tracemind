"""Read-only projection of a server-owned immutable engine catalog.

This module does not install, verify, activate or execute providers. The host must
publish a complete Catalog atomically as app.state.engine_catalog after its own
verification and runtime validation. In its absence we report an empty catalog.
"""
from app.engine_registry import Catalog


def catalog_view(catalog: Catalog, authorized_capabilities: dict[str, set[str]], boot_id: str) -> dict:
    if not isinstance(catalog, Catalog):
        raise ValueError("catalog_unavailable")
    entries = []
    for entry in catalog.entries:
        entries.append({
            "engine_id": entry.engine_id,
            "family": entry.family,
            "protocol": entry.protocol,
            "provider_version": entry.version,
            "available": entry.available and not entry.fixture_only,
            "reason": "fixture_only" if entry.fixture_only else entry.reason,
            "fixture_only": entry.fixture_only,
            # A host descriptor and the caller's platform grants must both contain
            # the exact capability ID. This endpoint never grants execution.
            "capabilities": [cap for cap in entry.capabilities if not entry.fixture_only and cap in authorized_capabilities.get(entry.engine_id, set())],
            # The present registry has no connection schema; never invent one.
            "connection_schema": None,
        })
    return {"schema": "engine.catalog_view/v1", "catalog_generation": catalog.generation, "catalog_boot_id": boot_id, "engines": entries}
