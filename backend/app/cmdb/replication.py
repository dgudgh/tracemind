"""Replication evidence at the discovery boundary; roles are never endpoints."""

import ipaddress
from typing import Any


def host_key(value: Any) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return text


def manual_relation(payload: Any) -> bool:
    return isinstance(payload, dict) and (
        payload.get("discovery_source") == "manual" or payload.get("provenance") == "manual"
    )


def role_derived(payload: Any) -> bool:
    return isinstance(payload, dict) and not manual_relation(payload) and (
        payload.get("discovery_method") == "database_role" or payload.get("provenance") == "derived"
    )


def normalized_role_derived(row: Any) -> bool:
    payload = row.attributes or {}
    return row.discovery_source != "manual" and not manual_relation(payload) and (
        role_derived(payload) or (
            row.relation_key == f"instance:{row.source_id}:replicates_to:instance:{row.target_id}"
            and isinstance(payload, dict) and set(payload) == {"cluster_id"}
        )
    )


def upstream_evidence(profile: dict) -> dict:
    """Keep only endpoint facts, never raw conninfo (which may contain secrets)."""
    if profile.get("probe_failed") or profile.get("discovery_status") == "failed":
        return {"state": "unknown", "reason": "probe_failed"}
    upstream = {}
    for field, aliases in {
        "host": ("upstream_host", "source_host", "master_host", "sender_host"),
        "port": ("upstream_port", "source_port", "master_port", "sender_port"),
        "server_uuid": ("upstream_uuid", "source_uuid", "master_uuid"),
        "server_id": ("upstream_server_id", "source_server_id", "master_server_id"),
    }.items():
        for alias in aliases:
            if profile.get(alias) not in (None, ""):
                upstream[field] = profile[alias]
                break
    if upstream:
        return {"state": "verified", "upstream": upstream, "evidence_source": "upstream_metadata"}
    if (profile.get("architecture_type") in {"data_guard", "dataguard", "adg"}
            and profile.get("upstream_db_unique_name")):
        return {"state": "verified", "upstream": {"db_unique_name": profile["upstream_db_unique_name"]},
                "evidence_source": "v$dataguard_config"}
    if (profile.get("architecture_type") in {"always_on", "alwayson"}
            and profile.get("upstream_server_name") and profile.get("availability_group_id")):
        return {"state": "verified", "upstream": {"server_name": profile["upstream_server_name"],
                "availability_group_id": profile["availability_group_id"]},
                "evidence_source": "sys.dm_hadr_availability_group_states"}
    # The existing PG driver retains slot_name only on pg_stat_wal_receiver rows.
    # MySQL/AG/DG member roles alone do not retain query direction.
    members = profile.get("members")
    members = members if isinstance(members, list) else []
    receivers = [member for member in members if isinstance(member, dict)
                 and "slot_name" in member and profile.get("in_recovery") is True]
    if len(receivers) == 1:
        member = receivers[0]
        return {"state": "verified", "upstream": {
            "host": member.get("host"), "port": member.get("port"),
        }, "evidence_source": "pg_stat_wal_receiver"}
    return {"state": "derived" if profile.get("members") else "unknown",
            "reason": "member_direction_not_collected" if profile.get("members") else "upstream_not_collected"}


def resolve_upstream(target: dict, assets: list[dict]) -> tuple[dict | None, dict]:
    attrs = target.get("attributes") or {}
    if attrs.get("probe_failed") or attrs.get("discovery_status") == "failed":
        return None, {"state": "unknown", "reason": "probe_failed"}
    raw_evidence = attrs.get("replication")
    evidence = dict(raw_evidence) if isinstance(raw_evidence, dict) else {}
    if not evidence and (attrs.get("architecture_verified") or attrs.get("discovery_method") == "live_probe"):
        profile = attrs.get("architecture_evidence") or attrs
        evidence = upstream_evidence(profile) if isinstance(profile, dict) else {}
    if evidence.get("state") != "verified":
        return None, evidence or {"state": "unknown", "reason": "upstream_not_collected"}
    upstream = evidence.get("upstream")
    if not isinstance(upstream, dict):
        return None, {"state": "unknown", "reason": "invalid_upstream"}
    if attrs.get("architecture_type") in {"rac", "oracle_rac", "single", "standalone"}:
        return None, {**evidence, "state": "unknown", "reason": "non_replication_architecture"}
    candidates = {}
    for asset in assets:
        peer = asset.get("attributes") or {}
        if (asset.get("asset_type") != "database_instance"
                or asset.get("status", "active") != "active"
                or asset.get("source_id") != target.get("source_id")
                or str(peer.get("db_type")).lower() != str(attrs.get("db_type")).lower()
                or any(peer.get(key) != attrs.get(key) for key in ("tenant_id", "organization_id"))):
            continue
        identity_fields = ("server_uuid", "server_id")
        if str(attrs.get("db_type") or "").lower() == "oracle":
            identity_fields += ("db_unique_name",)
        elif str(attrs.get("db_type") or "").lower() == "sqlserver":
            identity_fields += ("server_name", "availability_group_id")
        ids = [(key, upstream[key]) for key in identity_fields if upstream.get(key) not in (None, "")]
        observed_identity = {key: peer.get(key) or (peer.get("architecture_evidence") or {}).get(key)
                             for key in identity_fields}
        if ids and any(str(observed_identity.get(key) or "").lower() != str(value).lower() for key, value in ids):
            continue
        host = host_key(upstream.get("host"))
        if host and host not in {host_key(peer.get(key)) for key in ("host", "host_ip", "hostname", "reported_host")}:
            continue
        try:
            port = int(upstream.get("port") or 0)
            if upstream.get("port") not in (None, "") and (not 0 < port < 65536 or port != int(peer.get("port") or 0)):
                continue
        except (ValueError, TypeError):
            continue
        if not ids and not (host and port):
            continue
        candidates[asset.get("id", asset.get("external_id"))] = asset
    if len(candidates) != 1:
        return None, {**evidence, "state": "unknown", "reason": "ambiguous_source" if candidates else "missing_source"}
    candidate = next(iter(candidates.values()))
    if candidate is target or candidate.get("id", candidate.get("external_id")) == target.get("id", target.get("external_id")):
        return None, {**evidence, "state": "unknown", "reason": "self_source"}
    return candidate, evidence
