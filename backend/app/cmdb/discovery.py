from __future__ import annotations

import hashlib
import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance

INTERNAL_SOURCE_NAME = "TraceMind 自动发现"
INTERNAL_SOURCE_TYPE = "internal"

CLUSTERED_ARCHITECTURES = {
    "rac",
    "data_guard",
    "primary_standby",
    "replication",
    "mgr",
    "always_on",
    "streaming_replication",
    "cluster",
}

ARCHITECTURE_ALIASES = {
    "": "standalone",
    "single": "standalone",
    "standalone": "standalone",
    "rac": "rac",
    "oracle_rac": "rac",
    "dataguard": "data_guard",
    "data guard": "data_guard",
    "data_guard": "data_guard",
    "primary-standby": "primary_standby",
    "primary_standby": "primary_standby",
    "master-slave": "replication",
    "master-replica": "replication",
    "replication": "replication",
    "mgr": "mgr",
    "group_replication": "mgr",
    "alwayson": "always_on",
    "always on": "always_on",
    "always_on": "always_on",
    "streaming": "streaming_replication",
    "streaming_replication": "streaming_replication",
    "cluster": "cluster",
}

ROLE_ALIASES = {
    "": "unknown",
    "master": "primary",
    "primary": "primary",
    "leader": "primary",
    "standby": "standby",
    "physical standby": "standby",
    "logical standby": "standby",
    "slave": "replica",
    "replica": "replica",
    "secondary": "replica",
    "readonly": "readonly",
    "read_only": "readonly",
    "dr": "dr",
    "report": "report",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class DiscoveryBundle:
    assets: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    instance_bindings: list[dict[str, Any]]
    warnings: list[str]
    collected_at: datetime


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_host_address(value: Any) -> str:
    text = _normalized_text(value)
    if not text:
        return ""
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return text


def connection_endpoint_key(db_type: str, host: str, port: int, connect_type: str = "",
                            service_name: str = "", sid: str = "") -> tuple:
    endpoint = (str(db_type).lower(), normalize_host_address(host).lower(), int(port))
    if endpoint[0] == "oracle":
        kind = "sid" if connect_type == "sid" else "service_name"
        return (*endpoint, kind, str((sid if kind == "sid" else service_name) or "").casefold())
    return endpoint


def friendly_hostname(value: Any, host_address: Any = "") -> str:
    text = _normalized_text(value)
    if not text:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text):
        return ""
    try:
        ipaddress.ip_address(normalize_host_address(text))
        return ""
    except ValueError:
        pass
    if text.lower() == normalize_host_address(host_address).lower():
        return ""
    return text


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9._:-]+", "-", _normalized_text(value).lower()).strip("-")


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _scope_identity(tenant_id: Any, organization_id: Any) -> tuple[Any, Any]:
    return tenant_id, organization_id


def _scoped_identity(value: str, tenant_id: Any, organization_id: Any) -> str:
    """Keep legacy identifiers for the global scope; isolate overlapping estates."""
    if tenant_id is None and organization_id is None:
        return value
    return f"tenant={tenant_id!s}|organization={organization_id!s}|{value}"


def _scoped_host_key(host: str, tenant_id: Any, organization_id: Any) -> Any:
    normalized = normalize_host_address(host).lower()
    if tenant_id is None and organization_id is None:
        return normalized
    return tenant_id, organization_id, normalized


def normalize_role(value: Any, db_type: str = "") -> str:
    raw = _normalized_text(value).lower()
    role = ROLE_ALIASES.get(raw, raw or "unknown")
    if role == "replica" and db_type == "oracle":
        return "standby"
    return role


def normalize_architecture(value: Any, role: str = "unknown", db_type: str = "") -> str:
    raw = _normalized_text(value).lower()
    architecture = ARCHITECTURE_ALIASES.get(raw, raw or "standalone")
    if architecture == "standalone" and role in {"standby", "replica", "readonly", "dr"}:
        if db_type == "postgresql":
            return "streaming_replication"
        if db_type == "oracle":
            return "data_guard"
        if db_type == "sqlserver":
            return "always_on"
        return "replication"
    return architecture


def _group_identity(
    instance: DBInstance,
    architecture: str,
    detected_profile: dict[str, Any] | None = None,
) -> str:
    detected_group = _normalized_key((detected_profile or {}).get("topology_group"))
    if detected_group:
        return detected_group
    configured = next((
        _normalized_text(value)
        for value in (
            instance.system_name,
            instance.database_name,
            instance.service_name,
        )
        if _normalized_text(value)
    ), "")
    fallback = configured or _normalized_text(instance.instance_name) or f"instance-{instance.id}"
    return f"{instance.db_type}:{architecture}:{fallback.lower()}"


def _cluster_external_id(
    group_identity: str,
    tenant_id: Any = None,
    organization_id: Any = None,
) -> str:
    return f"db-cluster:{_digest(_scoped_identity(group_identity, tenant_id, organization_id))}"


def _relation_external_id(relation_type: str, source_external_id: str, target_external_id: str) -> str:
    return f"relation:{relation_type}:{_digest(f'{source_external_id}>{target_external_id}') }"


def _instance_attributes(
    instance: DBInstance,
    architecture: str,
    role: str,
    group_identity: str,
    detected_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    discovery_status = _normalized_text((detected_profile or {}).get("discovery_status"))
    probe_failed = discovery_status == "failed" or bool((detected_profile or {}).get("probe_failed"))
    discovery_method = "live_probe_failed" if probe_failed else ("live_probe" if detected_profile else "configured_metadata")
    configured_host = normalize_host_address(instance.host)
    detected_host = normalize_host_address((detected_profile or {}).get("host_ip"))
    runtime_host = detected_host if detected_host and not probe_failed else configured_host
    attributes = {
        "native_instance_id": instance.id,
        "native_type": "db_instance",
        "db_type": _normalized_text(instance.db_type).lower(),
        "db_version": _normalized_text((detected_profile or {}).get("db_version") or instance.db_version),
        "host": runtime_host,
        "port": instance.port,
        "database_name": _normalized_text(instance.database_name),
        "service_name": _normalized_text(instance.service_name),
        "environment": _normalized_text(instance.env_type),
        "system_name": _normalized_text(instance.system_name),
        "factory_name": _normalized_text(instance.factory_name),
        "line_name": _normalized_text(instance.line_name),
        "tenant_id": instance.tenant_id,
        "organization_id": instance.organization_id,
        "architecture_type": architecture,
        "role_type": role,
        "topology_group": group_identity,
        "enabled": bool(instance.enabled),
        "health_status": "warning" if probe_failed else ("healthy" if instance.enabled else "disabled"),
        "ssh_configured": bool(instance.ssh_host and instance.ssh_username),
        "source_kind": "automatic",
        "discovery_method": discovery_method,
        "discovery_status": discovery_status or ("success" if detected_profile else "configured"),
        "architecture_verified": bool(detected_profile) and not probe_failed,
    }
    if runtime_host != configured_host:
        attributes["connection_host"] = configured_host
        attributes["connection_endpoint"] = f"{configured_host}:{instance.port}"
    if probe_failed:
        attributes["discovery_error"] = _normalized_text((detected_profile or {}).get("discovery_error"))
    if detected_profile:
        for key in (
            "cluster_name",
            "replication_delay_seconds",
            "transport_lag_seconds",
            "apply_lag_seconds",
            "transport_lag",
            "apply_lag",
            "apply_finish_time",
            "lag_time_computed",
            "lag_datum_time",
            "hostname",
            "instance_name",
            "host_ip",
            "server_id",
            "server_uuid",
        ):
            if detected_profile.get(key) is not None:
                attributes[key] = detected_profile[key]
        attributes["architecture_evidence"] = {
            key: value
            for key, value in detected_profile.items()
            if key not in {"architecture_type", "role_type", "members", "topology_group"}
        }
    return attributes


def _canonical_live_instance_ids(
    instances: list[DBInstance],
    profiles: dict[int, dict[str, Any]],
) -> dict[int, int]:
    """Collapse connection entries that reached the same physical database member."""
    identity_groups: dict[tuple[Any, ...], list[DBInstance]] = defaultdict(list)
    canonical_ids = {instance.id: instance.id for instance in instances}

    for instance in instances:
        profile = profiles.get(instance.id) or {}
        if profile.get("discovery_status") != "success":
            continue
        member_key = _normalized_key(profile.get("member_key"))
        topology_group = _normalized_key(profile.get("topology_group"))
        if not member_key or not topology_group:
            continue
        identity_groups[
            (
                _normalized_text(instance.db_type).lower(),
                instance.tenant_id,
                instance.organization_id,
                topology_group,
                member_key,
            )
        ].append(instance)

    def canonical_score(instance: DBInstance) -> tuple[int, int]:
        profile = profiles.get(instance.id) or {}
        detected_host = normalize_host_address(profile.get("host_ip")).lower()
        configured_host = normalize_host_address(instance.host).lower()
        ssh_host = normalize_host_address(instance.ssh_host).lower()
        detected_instance = _normalized_text(profile.get("instance_name")).lower()
        detected_hostname = _normalized_text(profile.get("hostname")).lower()
        configured_name = _normalized_text(instance.instance_name).lower()
        score = 0
        if detected_host and configured_host == detected_host:
            score += 100
        if detected_host and ssh_host == detected_host:
            score += 70
        if configured_name and configured_name == detected_instance:
            score += 20
        if configured_name and configured_name == detected_hostname:
            score += 10
        if configured_host and configured_host == ssh_host:
            score += 2
        return score, -int(instance.id)

    for group in identity_groups.values():
        if len(group) < 2:
            continue
        canonical = max(group, key=canonical_score)
        for instance in group:
            canonical_ids[instance.id] = canonical.id
    return canonical_ids


def build_internal_discovery(
    db: Session,
    detected_profiles: dict[int, dict[str, Any]] | None = None,
    host_profiles: dict[str, dict[str, Any]] | None = None,
    extra_warnings: list[str] | None = None,
) -> DiscoveryBundle:
    instances = db.query(DBInstance).order_by(DBInstance.id.asc()).all()
    profiles = detected_profiles or {}
    discovered_hosts = host_profiles or {}
    collected_at = datetime.now()
    assets: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    warnings: list[str] = list(extra_warnings or [])
    hosts: dict[Any, dict[str, Any]] = {}
    database_assets: dict[str, dict[str, Any]] = {}
    configured_by_endpoint: dict[tuple, str] = {}
    configured_by_member_key: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    configured_by_hostname: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    host_relation_ids: set[str] = set()
    cluster_members: dict[tuple[Any, Any, str], dict[str, dict[str, str]]] = defaultdict(dict)
    cluster_metadata: dict[tuple[Any, Any, str], dict[str, Any]] = {}

    def attach_host(
        database_asset: dict[str, Any],
        host_address: str,
        db_type: str,
        environment: str,
        system_name: str,
        native_instance_id: int | None = None,
        ssh_host: str = "",
        ssh_port: int = 22,
        hostname: str = "",
        confidence: int = 100,
    ) -> None:
        host_address = normalize_host_address(host_address)
        if not host_address:
            return
        attributes = database_asset.get("attributes") or {}
        tenant_id = attributes.get("tenant_id")
        organization_id = attributes.get("organization_id")
        candidate_hostname = friendly_hostname(hostname, host_address)
        host_key = _scoped_host_key(host_address, tenant_id, organization_id)
        host_external_id = f"host:{_digest(_scoped_identity(host_address.lower(), tenant_id, organization_id))}"
        host = hosts.setdefault(host_key, {
            "external_id": host_external_id,
            "asset_type": "host",
            "name": candidate_hostname or host_address,
            "attributes": {
                "host": host_address,
                "hostname": candidate_hostname,
                "ssh_host": ssh_host,
                "ssh_port": ssh_port,
                "environment": environment,
                "system_name": system_name,
                "tenant_id": tenant_id,
                "organization_id": organization_id,
                "database_instance_ids": [],
                "database_asset_external_ids": [],
                "database_types": [],
                "health_status": "unknown",
                "source_kind": "automatic",
            },
        })
        host_attributes = host["attributes"]
        current_hostname = friendly_hostname(host_attributes.get("hostname"), host_address)
        if candidate_hostname and not current_hostname:
            host_attributes["hostname"] = candidate_hostname
            host["name"] = candidate_hostname
        if ssh_host:
            host_attributes["ssh_host"] = ssh_host
            host_attributes["ssh_port"] = ssh_port
        host_profile = discovered_hosts.get(host_key) or {}
        for field in ("hostname", "os_type", "os_version", "cpu_cores", "cpu_model", "memory_total_mb"):
            value = host_profile.get(field)
            if value not in (None, ""):
                host_attributes[field] = value
        profile_hostname = friendly_hostname(host_profile.get("hostname"), host_address)
        if profile_hostname:
            host_attributes["hostname"] = profile_hostname
            host["name"] = profile_hostname
        if native_instance_id is not None and native_instance_id not in host_attributes["database_instance_ids"]:
            host_attributes["database_instance_ids"].append(native_instance_id)
        database_external_id = database_asset["external_id"]
        if database_external_id not in host_attributes["database_asset_external_ids"]:
            host_attributes["database_asset_external_ids"].append(database_external_id)
        if db_type not in host_attributes["database_types"]:
            host_attributes["database_types"].append(db_type)
        relation_external_id = _relation_external_id("runs_on", database_external_id, host_external_id)
        if relation_external_id in host_relation_ids:
            return
        host_relation_ids.add(relation_external_id)
        relations.append({
            "external_id": relation_external_id,
            "source_external_id": database_external_id,
            "target_external_id": host_external_id,
            "relation_type": "runs_on",
            "raw_payload": {
                "discovery_method": "db_instance_ssh_mapping" if native_instance_id is not None else "database_topology",
                "confidence": confidence,
            },
        })

    canonical_instance_ids = _canonical_live_instance_ids(instances, profiles)
    instances_by_id = {instance.id: instance for instance in instances}
    connection_ids_by_canonical: dict[int, list[int]] = defaultdict(list)
    for instance in instances:
        connection_ids_by_canonical[canonical_instance_ids[instance.id]].append(instance.id)

    for canonical_id in sorted(connection_ids_by_canonical):
        instance = instances_by_id[canonical_id]
        db_type = _normalized_text(instance.db_type).lower()
        detected_profile = profiles.get(instance.id)
        role = normalize_role(
            (detected_profile or {}).get("role_type") or instance.role_type,
            db_type,
        )
        architecture = normalize_architecture(
            (detected_profile or {}).get("architecture_type") or instance.architecture_type,
            role,
            db_type,
        )
        group_identity = _group_identity(instance, architecture, detected_profile)
        instance_external_id = f"db-instance:{instance.id}"
        instance_attributes = _instance_attributes(
            instance,
            architecture,
            role,
            group_identity,
            detected_profile=detected_profile,
        )
        instance_attributes["connection_instance_ids"] = sorted(
            connection_ids_by_canonical[canonical_id]
        )
        database_asset = {
            "external_id": instance_external_id,
            "asset_type": "database_instance",
            "name": _normalized_text(instance.instance_name) or instance_external_id,
            "attributes": instance_attributes,
        }
        assets.append(database_asset)
        database_assets[instance_external_id] = database_asset

        host_address = normalize_host_address(instance_attributes.get("host"))
        attach_host(
            database_asset,
            host_address,
            db_type,
            _normalized_text(instance.env_type),
            _normalized_text(instance.system_name),
            native_instance_id=instance.id,
            ssh_host=_normalized_text(instance.ssh_host),
            ssh_port=instance.ssh_port or 22,
            hostname=_normalized_text((detected_profile or {}).get("hostname")),
        )

    for instance in instances:
        canonical_id = canonical_instance_ids[instance.id]
        instance_external_id = f"db-instance:{canonical_id}"
        database_asset = database_assets[instance_external_id]
        db_type = _normalized_text(instance.db_type).lower()
        scope = _scope_identity(instance.tenant_id, instance.organization_id)
        detected_profile = profiles.get(instance.id)
        topology_group = _normalized_key(
            (detected_profile or {}).get("topology_group")
            or (database_asset.get("attributes") or {}).get("topology_group")
        )
        bindings.append({
            "instance_id": instance.id,
            "asset_external_id": instance_external_id,
            "match_rule": (
                "live_database_member_identity"
                if canonical_id != instance.id
                else "native_instance_id"
            ),
            "confidence": 100 if canonical_id == instance.id else 98,
        })
        configured_by_endpoint[(*scope, *connection_endpoint_key(
            db_type, instance.host, instance.port,
            instance.oracle_connect_type, instance.service_name, instance.sid,
        ))] = instance_external_id
        runtime_host = normalize_host_address(
            (detected_profile or {}).get("host_ip")
        ).lower()
        if runtime_host:
            configured_by_endpoint[(*scope, *connection_endpoint_key(
                db_type, runtime_host, instance.port,
                instance.oracle_connect_type, instance.service_name, instance.sid,
            ))] = instance_external_id
        for configured_alias in (
            instance.instance_name,
            instance.database_name,
            instance.service_name,
            instance.sid,
        ):
            alias_key = _normalized_key(configured_alias)
            if alias_key:
                configured_by_member_key[
                    (*scope, db_type, topology_group, alias_key)
                ].add(instance_external_id)
                configured_by_member_key[
                    (*scope, db_type, "", alias_key)
                ].add(instance_external_id)
        member_key = _normalized_key(
            (detected_profile or {}).get("member_key")
            or (detected_profile or {}).get("server_uuid")
            or (detected_profile or {}).get("server_id")
        )
        if member_key:
            configured_by_member_key[
                (*scope, db_type, topology_group, member_key)
            ].add(instance_external_id)
        hostname = _normalized_text((detected_profile or {}).get("hostname"))
        if hostname:
            configured_by_hostname[
                (*scope, db_type, topology_group, hostname.lower())
            ].add(instance_external_id)
            configured_by_hostname[
                (*scope, db_type, "", hostname.lower())
            ].add(instance_external_id)

    def configured_identity_match(
        index: dict[tuple[Any, ...], set[str]],
        scope: tuple[Any, Any],
        db_type: str,
        topology_group: str,
        identity: str,
    ) -> str | None:
        exact = index.get((*scope, db_type, topology_group, identity), set())
        candidates = exact or index.get((*scope, db_type, "", identity), set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    for instance in instances:
        detected_profile = profiles.get(instance.id) or {}
        members = detected_profile.get("members")
        if not isinstance(members, list) or not members:
            continue
        parent_external_id = f"db-instance:{canonical_instance_ids[instance.id]}"
        parent_asset = database_assets[parent_external_id]
        parent_attributes = parent_asset["attributes"]
        db_type = parent_attributes["db_type"]
        architecture = parent_attributes["architecture_type"]
        group_identity = parent_attributes["topology_group"]
        tenant_id = parent_attributes.get("tenant_id")
        organization_id = parent_attributes.get("organization_id")
        scope = _scope_identity(tenant_id, organization_id)
        topology_group = _normalized_key(group_identity)
        for member in members:
            if not isinstance(member, dict):
                continue
            host = normalize_host_address(member.get("host") or member.get("reported_host"))
            try:
                port = int(member.get("port") or instance.port)
            except (TypeError, ValueError):
                port = int(instance.port)
            member_key = _normalized_key(
                member.get("member_key")
                or member.get("server_uuid")
                or member.get("server_id")
                or f"{host}:{port}"
            )
            peer_external_id = configured_identity_match(
                configured_by_member_key,
                scope,
                db_type,
                topology_group,
                member_key,
            )
            if not peer_external_id and host:
                peer_external_id = configured_by_endpoint.get((*scope, *connection_endpoint_key(
                    db_type, host, port,
                    member.get("oracle_connect_type") or instance.oracle_connect_type,
                    member.get("service_name") or instance.service_name,
                    member.get("sid") or (
                        member.get("instance_name")
                        if instance.oracle_connect_type == "sid"
                        else instance.sid
                    ),
                )))
            reported_host = friendly_hostname(member.get("reported_host"), host)
            if not peer_external_id and reported_host:
                peer_external_id = configured_identity_match(
                    configured_by_hostname,
                    scope,
                    db_type,
                    topology_group,
                    reported_host.lower(),
                )
            if member.get("db_probe_status") == "failed" and member.get("direct_probe") is not True:
                peer_external_id = None
            if peer_external_id == parent_external_id:
                continue

            role = normalize_role(member.get("role_type"), db_type)
            health_status = _normalized_text(member.get("health_status")).lower() or "unknown"
            # A seed-side transport/member state is still useful evidence, but
            # it is not proof that the peer database itself is healthy.  Every
            # engine therefore requires a successful direct identity probe
            # before a discovered peer may project a healthy state.
            projected_health_status = (
                "unknown"
                if member.get("direct_probe") is not True
                or member.get("db_probe_status") == "failed"
                else health_status
            )
            if peer_external_id:
                peer_asset = database_assets[peer_external_id]
                peer_attributes = peer_asset["attributes"]
                peer_attributes["architecture_type"] = architecture
                peer_attributes["topology_group"] = group_identity
                if role != "unknown":
                    peer_attributes["role_type"] = role
                if member.get("db_version") and not peer_attributes.get("db_version"):
                    peer_attributes["db_version"] = member["db_version"]
                if member.get("replication_delay_seconds") is not None:
                    peer_attributes["replication_delay_seconds"] = member["replication_delay_seconds"]
                for lag_key in (
                    "transport_lag_seconds",
                    "apply_lag_seconds",
                    "transport_lag",
                    "apply_lag",
                    "apply_finish_time",
                    "lag_time_computed",
                    "lag_datum_time",
                ):
                    if member.get(lag_key) is not None:
                        peer_attributes[lag_key] = member[lag_key]
                if peer_attributes.get("native_type") == "discovered_database_instance":
                    peer_attributes["health_status"] = projected_health_status
                elif member.get("direct_probe") is True and health_status != "unknown":
                    peer_attributes["health_status"] = health_status
                peer_attributes["cluster_name"] = parent_attributes.get("cluster_name")
                continue

            peer_identity = member_key or _normalized_key(f"{host}:{port}")
            # Preserve existing global-scope IDs: users and normalized rows may
            # already refer to them.  Scoped estates include both scope and
            # topology so repeated private identities cannot bind together.
            peer_id_material = (
                peer_identity
                if tenant_id is None and organization_id is None
                else _scoped_identity(f"{topology_group}|{peer_identity}", tenant_id, organization_id)
            )
            peer_external_id = f"db-discovered:{db_type}:{_digest(peer_id_material)}"
            peer_asset = database_assets.get(peer_external_id)
            if not peer_asset:
                peer_asset = {
                    "external_id": peer_external_id,
                    "asset_type": "database_instance",
                    "name": (
                        reported_host
                        or host
                        or _normalized_text(member.get("name") or member.get("db_unique_name"))
                        or peer_external_id
                    ),
                    "attributes": {
                        "native_instance_id": None,
                        "native_type": "discovered_database_instance",
                        "db_type": db_type,
                        "db_version": _normalized_text(member.get("db_version")),
                        "host": host,
                        "port": port,
                        "database_name": "",
                        "service_name": "",
                        "environment": parent_attributes.get("environment") or "",
                        "system_name": parent_attributes.get("system_name") or "",
                        "tenant_id": tenant_id,
                        "organization_id": organization_id,
                        "architecture_type": architecture,
                        "role_type": role,
                        "topology_group": group_identity,
                        "cluster_name": parent_attributes.get("cluster_name"),
                        "replication_delay_seconds": member.get("replication_delay_seconds"),
                        "transport_lag_seconds": member.get("transport_lag_seconds"),
                        "apply_lag_seconds": member.get("apply_lag_seconds"),
                        "transport_lag": member.get("transport_lag"),
                        "apply_lag": member.get("apply_lag"),
                        "apply_finish_time": member.get("apply_finish_time"),
                        "lag_time_computed": member.get("lag_time_computed"),
                        "lag_datum_time": member.get("lag_datum_time"),
                        "enabled": True,
                        "health_status": projected_health_status,
                        "ssh_configured": False,
                        "connection_managed": False,
                        "source_kind": "automatic",
                        "discovery_method": "topology_probe",
                        "discovery_status": "success" if member.get("direct_probe") is True else (
                            "failed" if member.get("probe_error") or member.get("db_probe_status") == "failed"
                            else "unverified"
                        ),
                        "architecture_verified": member.get("direct_probe") is True,
                        "discovered_from_instance_id": instance.id,
                        "db_probe_status": member.get("db_probe_status") or "not_attempted",
                        "db_probe_reason": member.get("db_probe_reason"),
                        "db_probe_source_instance_id": member.get("db_probe_source_instance_id"),
                        "ssh_probe_status": member.get("ssh_probe_status") or "not_attempted",
                        "ssh_probe_source_instance_id": member.get("ssh_probe_source_instance_id"),
                        "reported_host": reported_host,
                        "server_id": member.get("server_id"),
                        "server_uuid": member.get("server_uuid"),
                        "architecture_evidence": {
                            key: value
                            for key, value in member.items()
                            if key not in {"member_key", "name", "host", "port", "role_type", "health_status"}
                        },
                    },
                }
                assets.append(peer_asset)
                database_assets[peer_external_id] = peer_asset
            if member_key:
                configured_by_member_key[
                    (*scope, db_type, topology_group, member_key)
                ].add(peer_external_id)
            if host:
                configured_by_endpoint[(*scope, *connection_endpoint_key(
                    db_type, host, port,
                    member.get("oracle_connect_type") or instance.oracle_connect_type,
                    member.get("service_name") or instance.service_name,
                    member.get("sid") or (
                        member.get("instance_name")
                        if instance.oracle_connect_type == "sid"
                        else instance.sid
                    ),
                ))] = peer_external_id
            if reported_host:
                configured_by_hostname[
                    (*scope, db_type, topology_group, reported_host.lower())
                ].add(peer_external_id)
            attach_host(
                peer_asset,
                host,
                db_type,
                parent_attributes.get("environment") or "",
                parent_attributes.get("system_name") or "",
                hostname=reported_host,
                confidence=90,
            )

    for database_asset in database_assets.values():
        attributes = database_asset.get("attributes") or {}
        if attributes.get("native_type") != "discovered_database_instance":
            continue
        host_address = normalize_host_address(attributes.get("host"))
        host_asset = hosts.get(_scoped_host_key(
            host_address,
            attributes.get("tenant_id"),
            attributes.get("organization_id"),
        )) if host_address else None
        host_attributes = (host_asset or {}).get("attributes") or {}
        hostname = friendly_hostname(host_attributes.get("hostname"), host_address)
        database_asset["name"] = hostname or host_address or database_asset["name"]
        attributes["host"] = host_address
        attributes["reported_host"] = hostname
        attributes["display_name_source"] = "hostname" if hostname else "host_address"

    for database_asset in database_assets.values():
        attributes = database_asset["attributes"]
        architecture = attributes.get("architecture_type") or "standalone"
        if architecture not in CLUSTERED_ARCHITECTURES:
            continue
        group_identity = attributes["topology_group"]
        tenant_id = attributes.get("tenant_id")
        organization_id = attributes.get("organization_id")
        cluster_key = (tenant_id, organization_id, group_identity)
        cluster_external_id = _cluster_external_id(
            group_identity,
            tenant_id,
            organization_id,
        )
        cluster_metadata.setdefault(cluster_key, {
            "external_id": cluster_external_id,
            "asset_type": "database_cluster",
            "name": attributes.get("cluster_name") or attributes.get("system_name") or f"{database_asset['name']} 集群",
            "attributes": {
                "db_type": attributes.get("db_type"),
                "architecture_type": architecture,
                "environment": attributes.get("environment") or "",
                "system_name": attributes.get("system_name") or "",
                "tenant_id": tenant_id,
                "organization_id": organization_id,
                "topology_group": group_identity,
                "health_status": "unknown",
                "source_kind": "automatic",
            },
        })
        cluster_members[cluster_key][database_asset["external_id"]] = {
            "external_id": database_asset["external_id"],
            "role": attributes.get("role_type") or "unknown",
        }
        relations.append({
            "external_id": _relation_external_id("member_of", database_asset["external_id"], cluster_external_id),
            "source_external_id": database_asset["external_id"],
            "target_external_id": cluster_external_id,
            "relation_type": "member_of",
            "raw_payload": {"discovery_method": "database_architecture", "confidence": 90},
        })

    assets.extend(hosts.values())
    assets.extend(cluster_metadata.values())

    for cluster_key, member_map in cluster_members.items():
        _, _, group_identity = cluster_key
        members = list(member_map.values())
        primaries = [member for member in members if member["role"] == "primary"]
        replicas = [member for member in members if member["role"] in {"standby", "replica", "readonly", "dr"}]
        if replicas and not primaries:
            warnings.append(f"{cluster_metadata[cluster_key]['name']} 未识别到主节点，复制关系待确认")
        for primary in primaries:
            for replica in replicas:
                relations.append({
                    "external_id": _relation_external_id("replicates_to", primary["external_id"], replica["external_id"]),
                    "source_external_id": primary["external_id"],
                    "target_external_id": replica["external_id"],
                    "relation_type": "replicates_to",
                    "raw_payload": {"discovery_method": "database_role", "confidence": 85},
                })

    return DiscoveryBundle(
        assets=assets,
        relations=relations,
        instance_bindings=bindings,
        warnings=warnings,
        collected_at=collected_at,
    )
