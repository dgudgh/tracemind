from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.cmdb.models import (
    AssetCluster,
    AssetCorrectionAudit,
    AssetDiscoveryBatch,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetRelation,
    AssetSite,
    AssetZone,
    CmdbAsset,
    CmdbBinding,
    CmdbRelation,
)
from app.cmdb.discovery import friendly_hostname, normalize_host_address

DB_TYPES = {"Oracle", "MySQL", "PostgreSQL", "SQLServer", "DomesticDB"}
ARCHITECTURES = {
    "single",
    "primary_replica",
    "mgr",
    "rac",
    "adg",
    "alwayson",
    "streaming_replication",
    "sharding",
}
ROLES = {
    "primary",
    "replica",
    "standby",
    "read_only",
    "candidate",
    "arbiter",
    "data_node",
    "control_node",
    "coordinator",
    "rac_node",
    "adg_primary",
    "adg_standby",
    "alwayson_primary",
    "alwayson_secondary",
    "unknown",
}

DEFAULT_REGION = "未配置"
DEFAULT_SITE = "未配置"
DEFAULT_ZONE = "未配置"
DEFAULT_ENVIRONMENT = "未配置"
LOCATION_FIELDS = {"region_id", "site_id", "zone_id", "env_id"}
ENVIRONMENT_ALIASES = {
    "production": {"生产", "生产环境", "prod", "production"},
    "test": {"测试", "测试环境", "test", "testing", "uat"},
    "staging": {"预发", "预发布", "预生产", "预生产环境", "staging", "preprod", "preproduction", "pre-production"},
    "development": {"开发", "开发环境", "dev", "development"},
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _key(value: Any) -> str:
    raw = _text(value).lower()
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", raw).strip("-")
    if normalized:
        return normalized
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _scope_suffix(attributes: dict[str, Any]) -> str:
    tenant_id = attributes.get("tenant_id")
    organization_id = attributes.get("organization_id")
    if tenant_id is None and organization_id is None:
        return ""
    return f":scope:{tenant_id!s}:{organization_id!s}"


def _scoped_host_lookup_key(attributes: dict[str, Any], ip: str) -> Any:
    normalized = normalize_host_address(ip).lower()
    tenant_id = attributes.get("tenant_id")
    organization_id = attributes.get("organization_id")
    if tenant_id is None and organization_id is None:
        return normalized
    return tenant_id, organization_id, normalized


def normalize_db_type(value: Any) -> str:
    raw = _text(value).lower().replace(" ", "")
    if raw == "oracle":
        return "Oracle"
    if raw in {"mysql", "mariadb"}:
        return "MySQL"
    if raw in {"postgresql", "postgres", "pgsql", "pg"}:
        return "PostgreSQL"
    if raw in {"sqlserver", "mssql", "microsoftsqlserver"}:
        return "SQLServer"
    return "DomesticDB"


def normalize_architecture(value: Any) -> str:
    raw = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "single",
        "standalone": "single",
        "single": "single",
        "replication": "primary_replica",
        "primary_standby": "primary_replica",
        "primary_replica": "primary_replica",
        "master_slave": "primary_replica",
        "master_replica": "primary_replica",
        "group_replication": "mgr",
        "mgr": "mgr",
        "rac": "rac",
        "oracle_rac": "rac",
        "data_guard": "adg",
        "dataguard": "adg",
        "adg": "adg",
        "always_on": "alwayson",
        "alwayson": "alwayson",
        "streaming": "streaming_replication",
        "streaming_replication": "streaming_replication",
        "sharding": "sharding",
        "cluster": "sharding",
    }
    return aliases.get(raw, raw if raw in ARCHITECTURES else "single")


def normalize_instance_role(value: Any, architecture: str) -> str:
    raw = _text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "unknown",
        "master": "primary",
        "leader": "primary",
        "primary": "primary",
        "slave": "replica",
        "secondary": "replica",
        "replica": "replica",
        "standby": "standby",
        "physical_standby": "standby",
        "logical_standby": "standby",
        "readonly": "read_only",
        "read_only": "read_only",
        "report": "read_only",
        "dr": "standby",
    }
    role = aliases.get(raw, raw if raw in ROLES else "unknown")
    if architecture == "rac" and role not in {"unknown", "arbiter"}:
        return "rac_node"
    if architecture == "adg":
        if role == "primary":
            return "adg_primary"
        if role in {"replica", "standby", "read_only"}:
            return "adg_standby"
    if architecture == "alwayson":
        if role == "primary":
            return "alwayson_primary"
        if role in {"replica", "standby", "read_only"}:
            return "alwayson_secondary"
    return role


def _environment(value: Any) -> tuple[str, str]:
    raw = _text(value)
    if raw:
        digest = hashlib.sha256(raw.casefold().encode("utf-8")).hexdigest()[:16]
        return f"env-{digest}", raw
    return "env-unconfigured", DEFAULT_ENVIRONMENT


def _env_type(code: str, name: str | None = None) -> str:
    if code == "env-unconfigured":
        return "unknown"
    value = _text(name).lower() if name is not None else code.removeprefix("env-").removeprefix("mes-")
    return next((kind for kind, aliases in ENVIRONMENT_ALIASES.items() if value in aliases), "custom")


def _manual_fields(row: Any) -> set[str]:
    return set(row.manual_fields or [])


def _assign(row: Any, field: str, value: Any) -> None:
    if field in LOCATION_FIELDS and _manual_fields(row).intersection(LOCATION_FIELDS):
        return
    if field not in _manual_fields(row):
        setattr(row, field, value)


def _aggregate_status(rows: list[Any]) -> str:
    statuses = {row.status for row in rows}
    if statuses & {"critical", "unhealthy"}:
        return "critical"
    if "warning" in statuses:
        return "warning"
    if "pending_confirmation" in statuses:
        return "pending_confirmation"
    if statuses == {"healthy"}:
        return "healthy"
    return "unknown"


def _apply_cluster_discovery_summary(cluster: AssetCluster, members: list[AssetInstance]) -> None:
    discovery_members = [
        row
        for row in members
        if (row.attributes or {}).get("discovery_status") in {"success", "failed"}
    ]
    if not discovery_members:
        return

    successful = [
        row for row in discovery_members
        if (row.attributes or {}).get("discovery_status") == "success"
    ]
    failed = [
        row for row in discovery_members
        if (row.attributes or {}).get("discovery_status") == "failed"
    ]
    primary_roles = {"primary", "adg_primary", "alwayson_primary", "coordinator", "control_node"}
    canonical = next((row for row in successful if row.role in primary_roles), None)
    canonical = canonical or (successful[0] if successful else discovery_members[0])
    canonical_attributes = dict(canonical.attributes or {})
    attributes = dict(cluster.attributes or {})

    for key in (
        "topology_group",
        "cluster_name",
        "architecture_type",
        "db_type",
        "environment",
        "region",
        "site",
        "zone",
        "system_name",
        "factory_name",
        "line_name",
        "vip",
        "source_kind",
    ):
        value = canonical_attributes.get(key)
        if value not in (None, "", [], {}):
            attributes[key] = value

    errors = [
        {
            "instance_id": row.id,
            "name": row.name,
            "endpoint": f"{row.ip}:{row.port}",
            "error": str((row.attributes or {}).get("discovery_error") or "实时架构探测失败"),
        }
        for row in failed
    ]
    attributes.update({
        "discovery_status": (
            "partial"
            if successful and failed
            else "success"
            if successful
            else "failed"
        ),
        "discovery_member_count": len(discovery_members),
        "verified_member_count": len(successful),
        "failed_member_count": len(failed),
        "architecture_verified": bool(successful),
        "last_discovery_at": max(row.last_seen_at for row in discovery_members).isoformat(),
    })
    if errors:
        attributes["discovery_errors"] = errors
        attributes["discovery_error"] = (
            f"{len(failed)}/{len(discovery_members)} 个节点实时架构探测失败"
            if successful
            else errors[0]["error"]
        )
    else:
        attributes.pop("discovery_errors", None)
        attributes.pop("discovery_error", None)
    cluster.attributes = attributes


def record_native_connection_test(
    db: Session,
    native_instance_id: int,
    *,
    ok: bool,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    """Record connection validation without changing discovered topology."""
    now = datetime.now().isoformat()
    rows = db.query(AssetInstance).filter(
        AssetInstance.native_instance_id == native_instance_id,
        AssetInstance.status.notin_(["merged", "inactive"]),
    ).all()
    architecture = (detail or {}).get("architecture")
    for row in rows:
        attributes = dict(row.attributes or {})
        attributes["connection_test_status"] = "success" if ok else "failed"
        attributes["last_connection_test_at"] = now
        if ok:
            attributes.pop("connection_test_error", None)
            if (detail or {}).get("version"):
                attributes["connection_test_version"] = (detail or {}).get("version")
            if isinstance(architecture, dict):
                attributes["connection_test_architecture"] = architecture.get("architecture_type")
                attributes["connection_test_role"] = architecture.get("role_type")
        else:
            attributes["connection_test_error"] = str(error or "连接失败")[:500]
            attributes.pop("connection_test_version", None)
            attributes.pop("connection_test_architecture", None)
            attributes.pop("connection_test_role", None)
        row.attributes = attributes
    db.commit()
    return len(rows)


def deactivate_native_instance(
    db: Session,
    native_instance_id: int,
    operator: str = "admin",
) -> dict[str, int]:
    now = datetime.now()
    instances = db.query(AssetInstance).filter(
        AssetInstance.native_instance_id == native_instance_id,
        AssetInstance.status != "merged",
    ).all()
    instance_ids = {row.id for row in instances}
    cluster_ids = {row.cluster_id for row in instances if row.cluster_id}
    host_ids = {row.host_id for row in instances if row.host_id}

    for row in instances:
        before = {
            "name": row.name,
            "status": row.status,
            "native_instance_id": row.native_instance_id,
        }
        row.status = "inactive"
        row.native_instance_id = None
        row.manual_fields = sorted(_manual_fields(row) | {"status"})
        attributes = dict(row.attributes) if isinstance(row.attributes, dict) else {}
        attributes.update({
            "connection_deleted": True,
            "deleted_native_instance_id": native_instance_id,
            "connection_deleted_at": now.isoformat(),
        })
        row.attributes = attributes
        db.add(AssetCorrectionAudit(
            action="delete_connection",
            entity_type="instance",
            entity_id=row.id,
            before_value=before,
            after_value={"status": "inactive", "native_instance_id": None},
            operator=operator,
            reason="删除本地数据库连接配置",
        ))

    if instance_ids:
        for relation in db.query(AssetRelation).all():
            if (
                relation.source_type == "instance" and relation.source_id in instance_ids
            ) or (
                relation.target_type == "instance" and relation.target_id in instance_ids
            ):
                relation.status = "inactive"

    source_assets = db.query(CmdbAsset).filter(
        CmdbAsset.external_id == f"db-instance:{native_instance_id}",
    ).all()
    source_asset_ids = {row.id for row in source_assets}
    for row in source_assets:
        row.status = "inactive"
        attributes = dict(row.attributes) if isinstance(row.attributes, dict) else {}
        attributes["health_status"] = "inactive"
        attributes["connection_deleted_at"] = now.isoformat()
        row.attributes = attributes
    if source_asset_ids:
        for relation in db.query(CmdbRelation).all():
            if relation.source_asset_id in source_asset_ids or relation.target_asset_id in source_asset_ids:
                relation.status = "inactive"

    bindings = db.query(CmdbBinding).filter(CmdbBinding.instance_id == native_instance_id).all()
    for row in bindings:
        row.binding_status = "inactive"

    db.flush()
    active_statuses = ["inactive", "merged"]
    for cluster_id in cluster_ids:
        remaining = db.query(AssetInstance).filter(
            AssetInstance.cluster_id == cluster_id,
            AssetInstance.status.notin_(active_statuses),
        ).count()
        cluster = db.get(AssetCluster, cluster_id)
        if not remaining and cluster and cluster.discovery_source != "manual":
            cluster.status = "inactive"
            cluster.manual_fields = sorted(_manual_fields(cluster) | {"status"})
    for host_id in host_ids:
        remaining = db.query(AssetInstance).filter(
            AssetInstance.host_id == host_id,
            AssetInstance.status.notin_(active_statuses),
        ).count()
        host = db.get(AssetHost, host_id)
        if not remaining and host and host.discovery_source != "manual":
            host.status = "inactive"
            host.manual_fields = sorted(_manual_fields(host) | {"status"})

    return {
        "instance_count": len(instances),
        "source_asset_count": len(source_assets),
        "binding_count": len(bindings),
    }


def _ensure_location(db: Session, attributes: dict[str, Any]) -> tuple[AssetRegion, AssetSite, AssetZone, AssetEnvironment]:
    raw_region = _text(attributes.get("region_name") or attributes.get("region") or attributes.get("region_code"))
    raw_site = _text(attributes.get("site_name") or attributes.get("site") or attributes.get("datacenter") or attributes.get("site_code"))
    raw_zone = _text(attributes.get("zone_name") or attributes.get("zone") or attributes.get("availability_zone") or attributes.get("zone_code"))
    region_name, site_name, zone_name = raw_region or DEFAULT_REGION, raw_site or DEFAULT_SITE, raw_zone or DEFAULT_ZONE
    region_code = _text(attributes.get("region_code")) or (f"region-{_key(raw_region)}" if raw_region else "region-unconfigured")
    site_code = _text(attributes.get("site_code")) or (f"site-{_key(raw_site)}" if raw_site else "site-unconfigured")
    zone_code = _text(attributes.get("zone_code")) or (f"zone-{_key(raw_zone)}" if raw_zone else "zone-unconfigured")

    region = db.query(AssetRegion).filter(AssetRegion.code == region_code).first()
    if not region:
        region = AssetRegion(code=region_code, name=region_name)
        db.add(region)
        db.flush()
    site = db.query(AssetSite).filter(AssetSite.region_id == region.id, AssetSite.code == site_code).first()
    if not site:
        site = AssetSite(region_id=region.id, code=site_code, name=site_name)
        db.add(site)
        db.flush()
    zone = db.query(AssetZone).filter(AssetZone.site_id == site.id, AssetZone.code == zone_code).first()
    if not zone:
        zone = AssetZone(region_id=region.id, site_id=site.id, code=zone_code, name=zone_name)
        db.add(zone)
        db.flush()

    env_code, env_name = _environment(attributes.get("environment_name") or attributes.get("environment"))
    environment = db.query(AssetEnvironment).filter(
        AssetEnvironment.zone_id == zone.id,
        AssetEnvironment.code == env_code,
    ).first()
    if not environment:
        environment = AssetEnvironment(
            region_id=region.id,
            site_id=site.id,
            zone_id=zone.id,
            code=env_code,
            name=env_name,
            env_type=_env_type(env_code, env_name),
        )
        db.add(environment)
        db.flush()
    for row in (region, site, zone, environment):
        if row.source_type == "automatic" and row.status == "inactive":
            row.status = "active"
    return region, site, zone, environment


def _retire_unused_legacy_location_defaults(db: Session, inventory: list[dict] | tuple = ()) -> int:
    """Retain historical rows; retire only unused, unconfigured old defaults."""
    models = {"region_id": AssetRegion, "site_id": AssetSite, "zone_id": AssetZone, "env_id": AssetEnvironment}
    legacy = {"region_id": {(f"region-{_key('华东一区')}", "华东一区")},
              "site_id": {("site-idc", "上海 IDC")}, "zone_id": {("zone-a", "A 区")},
              "env_id": {(f"mes-{kind}", f"MES {label}环境") for kind, label in (
                  ("production", "生产"), ("test", "测试"), ("staging", "预生产"), ("development", "开发"))}}
    protected = {key: set() for key in models}
    for model in (AssetHost, AssetCluster, AssetInstance):
        for row in db.query(model).all():
            if row.status not in {"inactive", "merged"} or row.discovery_source == "manual" or _manual_fields(row).intersection(LOCATION_FIELDS):
                for key in models:
                    protected[key].add(getattr(row, key))
    source_values = {key: set() for key in models}
    aliases = {"region_id": ("region", "region_name", "region_code"),
               "site_id": ("site", "site_name", "datacenter", "site_code"),
               "zone_id": ("zone", "zone_name", "availability_zone", "zone_code"),
               "env_id": ("environment", "environment_name")}
    for asset in inventory:
        attributes = asset.get("attributes") or {}
        for key, names in aliases.items():
            source_values[key].update(_text(attributes[name]) for name in names if attributes.get(name))
    rows = {key: db.query(model).all() for key, model in models.items()}
    for key, items in rows.items():
        for row in items:
            if (row.source_type != "automatic" or row.name in source_values[key] or row.code in source_values[key]
                    or row.status == "active" and (row.code, row.name) not in legacy[key]):
                protected[key].add(row.id)
    for env in rows["env_id"]:
        if env.id in protected["env_id"]:
            protected["zone_id"].add(env.zone_id)
            protected["site_id"].add(env.site_id)
            protected["region_id"].add(env.region_id)
    for zone in rows["zone_id"]:
        if zone.id in protected["zone_id"]:
            protected["site_id"].add(zone.site_id)
            protected["region_id"].add(zone.region_id)
    for site in rows["site_id"]:
        if site.id in protected["site_id"]:
            protected["region_id"].add(site.region_id)
    retired = 0
    for key, items in rows.items():
        for row in items:
            if row.id not in protected[key] and (row.code, row.name) in legacy[key] and row.status != "inactive":
                row.status = "inactive"
                retired += 1
    return retired


def _location_ids(location: tuple[AssetRegion, AssetSite, AssetZone, AssetEnvironment]) -> dict[str, int]:
    region, site, zone, environment = location
    return {"region_id": region.id, "site_id": site.id, "zone_id": zone.id, "env_id": environment.id}


def _synthetic_host_address(value: Any) -> bool:
    text = _text(value)
    return text.startswith("legacy-host:") or bool(re.fullmatch(r"主机-\d+", text))


def _upsert_host(db: Session, asset: dict[str, Any]) -> AssetHost:
    attributes = asset.get("attributes") or {}
    explicit_endpoint_state = attributes.get("endpoint_resolved")
    address = attributes.get("ip") or attributes.get("ip_address") or attributes.get("host")
    if not address and explicit_endpoint_state is not False:
        address = asset.get("name")
    ip = normalize_host_address(address)
    if explicit_endpoint_state is False or _synthetic_host_address(ip):
        ip = ""
    stable_key = (
        f"host:{ip.lower()}{_scope_suffix(attributes)}"
        if ip else f"legacy-host:{asset['id']}"
    )
    location = _location_ids(_ensure_location(db, attributes))
    row = db.query(AssetHost).filter(AssetHost.stable_key == stable_key).first()
    if not row:
        row = AssetHost(
            stable_key=stable_key,
            name=_text(asset.get("name")) or ip or stable_key,
            ip=ip,
        )
        db.add(row)
    candidate_hostname = friendly_hostname(attributes.get("hostname"), ip)
    if ip:
        candidate_hostname = candidate_hostname or friendly_hostname(asset.get("name"), ip)
    current_hostname = friendly_hostname(row.hostname, ip)
    if ip:
        current_hostname = current_hostname or friendly_hostname(row.name, ip)
    resolved_hostname = candidate_hostname or current_hostname
    merged_attributes = dict(row.attributes) if isinstance(row.attributes, dict) else {}
    merged_attributes.update({
        key: value
        for key, value in attributes.items()
        if value not in (None, "", [], {})
    })
    merged_attributes["host"] = ip
    merged_attributes["endpoint_resolved"] = bool(ip)
    if resolved_hostname:
        merged_attributes["hostname"] = resolved_hostname
    else:
        merged_attributes.pop("hostname", None)

    def preserve(existing: Any, incoming: Any) -> Any:
        return incoming if incoming not in (None, "") else existing

    values = {
        "name": candidate_hostname or row.name or _text(asset.get("name")) or ip or stable_key,
        "hostname": resolved_hostname or None,
        "ip": ip,
        "os_type": preserve(row.os_type, _text(attributes.get("os_type") or attributes.get("os_name"))),
        "os_version": preserve(row.os_version, _text(attributes.get("os_version"))),
        "cpu_cores": preserve(row.cpu_cores, attributes.get("cpu_cores") or attributes.get("cpu_count")),
        "cpu_model": preserve(row.cpu_model, _text(attributes.get("cpu_model"))),
        "memory_mb": preserve(row.memory_mb, attributes.get("memory_mb") or attributes.get("memory_total_mb")),
        "tenant_id": attributes.get("tenant_id"),
        "organization_id": attributes.get("organization_id"),
        "status": asset.get("health_status") or asset.get("status") or "unknown",
        "source_asset_id": asset.get("id") if candidate_hostname or row.source_asset_id is None else row.source_asset_id,
        "discovery_source": asset.get("source_kind") or "automatic",
        "attributes": merged_attributes,
        "last_seen_at": asset.get("last_seen_at") or datetime.now(),
        **location,
    }
    for field, value in values.items():
        _assign(row, field, value)
    db.flush()
    return row


def _instance_stable_key(asset: dict[str, Any]) -> str:
    attributes = asset.get("attributes") or {}
    native_id = attributes.get("native_instance_id")
    if native_id is not None:
        return f"native-instance:{native_id}"
    if asset.get("id") is not None:
        return f"source-asset:{asset['id']}"
    identity = attributes.get("service_name") or attributes.get("database_name") or asset.get("name") or "database"
    return "endpoint:{}:{}:{}:{}{}".format(
        normalize_db_type(attributes.get("db_type")).lower(),
        normalize_host_address(attributes.get("host")).lower(),
        attributes.get("port") or 0,
        _key(identity),
        _scope_suffix(attributes),
    )


def _upsert_cluster(
    db: Session,
    asset: dict[str, Any],
    instance_stable_key: str,
) -> AssetCluster:
    attributes = asset.get("attributes") or {}
    db_type = normalize_db_type(attributes.get("db_type"))
    architecture = normalize_architecture(attributes.get("architecture_type"))
    topology_group = _text(attributes.get("topology_group") or attributes.get("cluster_name"))
    if architecture == "single":
        stable_key = f"cluster:single:{instance_stable_key}{_scope_suffix(attributes)}"
        name = f"{asset.get('name') or db_type} 单机集群"
        cluster_type = "standalone"
    else:
        group_identity = topology_group or f"{db_type}:{architecture}:{asset.get('name')}"
        stable_key = f"cluster:{db_type.lower()}:{_key(group_identity)}{_scope_suffix(attributes)}"
        name = _text(attributes.get("cluster_name") or attributes.get("system_name")) or f"{asset.get('name')} 集群"
        cluster_type = "database_cluster"
    location = _location_ids(_ensure_location(db, attributes))
    row = db.query(AssetCluster).filter(AssetCluster.stable_key == stable_key).first()
    if not row:
        row = AssetCluster(stable_key=stable_key, name=name, db_type=db_type)
        db.add(row)
    cluster_attribute_keys = {
        "topology_group",
        "cluster_name",
        "architecture_type",
        "db_type",
        "environment",
        "region",
        "site",
        "zone",
        "system_name",
        "factory_name",
        "line_name",
        "vip",
        "source_kind",
        "discovery_method",
        "architecture_verified",
        "architecture_source",
        "has_verified_history",
        "last_verified_at",
        "tenant_id",
        "organization_id",
    }
    cluster_attributes = {
        key: deepcopy(value)
        for key, value in attributes.items()
        if key in cluster_attribute_keys and value not in (None, "", [], {})
    }
    if topology_group:
        cluster_attributes["topology_group"] = topology_group
    values = {
        "name": name,
        "db_type": db_type,
        "cluster_type": cluster_type,
        "architecture": architecture,
        "tenant_id": attributes.get("tenant_id"),
        "organization_id": attributes.get("organization_id"),
        "status": asset.get("health_status") or asset.get("status") or "unknown",
        "discovery_source": asset.get("source_kind") or "automatic",
        "attributes": cluster_attributes,
        "last_seen_at": asset.get("last_seen_at") or datetime.now(),
        **location,
    }
    for field, value in values.items():
        _assign(row, field, value)
    db.flush()
    return row


def _upsert_instance(db: Session, asset: dict[str, Any], host: AssetHost, cluster: AssetCluster) -> AssetInstance:
    attributes = asset.get("attributes") or {}
    stable_key = _instance_stable_key(asset)
    row = db.query(AssetInstance).filter(AssetInstance.stable_key == stable_key).first()
    if not row and asset.get("id") is not None:
        row = db.query(AssetInstance).filter(AssetInstance.source_asset_id == asset["id"]).first()
        if row:
            row.stable_key = stable_key
    automatic_peer = (
        attributes.get("native_instance_id") is None
        and attributes.get("native_type") == "discovered_database_instance"
    )
    configured_name = _text(asset.get("name"))
    discovered_name = host.hostname or (host.name if not _synthetic_host_address(host.name) else "") or host.ip
    generated_name = not friendly_hostname(configured_name, attributes.get("host"))
    desired_name = (discovered_name or configured_name or stable_key) if automatic_peer or generated_name else (configured_name or stable_key)
    instance_ip = normalize_host_address(attributes.get("host"))
    host_attributes = host.attributes if isinstance(host.attributes, dict) else {}
    manual_host_ip = (
        "ip" in _manual_fields(host)
        and bool(host.ip)
        and not _synthetic_host_address(host.ip)
    )
    if manual_host_ip:
        instance_ip = normalize_host_address(host.ip)
    elif (
        attributes.get("endpoint_resolved") is False
        or host_attributes.get("endpoint_resolved") is False
        or _synthetic_host_address(instance_ip)
    ):
        instance_ip = ""
    if not instance_ip and host_attributes.get("endpoint_resolved") is not False:
        instance_ip = "" if _synthetic_host_address(host.ip) else normalize_host_address(host.ip)
    if not row:
        row = AssetInstance(
            stable_key=stable_key,
            name=desired_name,
            db_type=normalize_db_type(attributes.get("db_type")),
            ip=instance_ip,
            port=int(attributes.get("port") or 0),
            host_id=host.id,
        )
        db.add(row)
    persisted_attributes = dict(row.attributes or {})
    connection_test_attributes = {
        key: value
        for key, value in persisted_attributes.items()
        if key.startswith("connection_test_") or key == "last_connection_test_at"
    }
    architecture = cluster.architecture
    values = {
        "name": desired_name or row.name,
        "db_type": normalize_db_type(attributes.get("db_type")),
        "ip": instance_ip,
        "port": int(attributes.get("port") or 0),
        "version": _text(attributes.get("db_version")) or None,
        "role": normalize_instance_role(attributes.get("role_type"), architecture),
        "host_id": host.id,
        "cluster_id": cluster.id,
        "region_id": host.region_id,
        "site_id": host.site_id,
        "zone_id": host.zone_id,
        "env_id": host.env_id,
        "tenant_id": attributes.get("tenant_id") or host.tenant_id,
        "organization_id": attributes.get("organization_id") or host.organization_id,
        "status": asset.get("health_status") or asset.get("status") or "unknown",
        "native_instance_id": attributes.get("native_instance_id"),
        "source_asset_id": asset.get("id"),
        "discovery_source": asset.get("source_kind") or "automatic",
        "attributes": {**connection_test_attributes, **attributes},
        "last_seen_at": asset.get("last_seen_at") or datetime.now(),
    }
    for field, value in values.items():
        _assign(row, field, value)
    db.flush()
    return row


def _upsert_relation(
    db: Session,
    relation_key: str,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    relation_type: str,
    attributes: dict[str, Any] | None = None,
) -> AssetRelation:
    row = next(
        (
            item
            for item in db.new
            if isinstance(item, AssetRelation) and item.relation_key == relation_key
        ),
        None,
    )
    if not row:
        row = db.query(AssetRelation).filter(AssetRelation.relation_key == relation_key).first()
    if not row:
        row = AssetRelation(
            relation_key=relation_key,
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation_type=relation_type,
        )
        db.add(row)
    row.source_type = source_type
    row.source_id = source_id
    row.target_type = target_type
    row.target_id = target_id
    row.relation_type = relation_type
    row.status = "active"
    row.attributes = attributes or {}
    return row


def sync_from_inventory(
    db: Session,
    inventory: list[dict[str, Any]],
    source_id: int | None,
    source_name: str,
    source_type: str,
    summary: dict[str, Any] | None = None,
    record_batch: bool = True,
) -> dict[str, Any]:
    started_at = datetime.now()
    database_assets = [
        item for item in inventory
        if item.get("asset_type") == "database_instance" and item.get("status") == "active"
    ]
    host_assets = [
        item for item in inventory
        if item.get("asset_type") == "host" and item.get("status") == "active"
    ]
    hosts_by_ip: dict[Any, AssetHost] = {}
    seen_host_ids: set[int] = set()
    seen_cluster_ids: set[int] = set()
    seen_instance_ids: set[int] = set()
    seen_relation_keys: set[str] = set()

    for asset in host_assets:
        host = _upsert_host(db, asset)
        if host.ip:
            hosts_by_ip[_scoped_host_lookup_key(asset.get("attributes") or {}, host.ip)] = host
        seen_host_ids.add(host.id)

    cluster_instances: dict[int, dict[int, AssetInstance]] = defaultdict(dict)
    for asset in database_assets:
        attributes = asset.get("attributes") or {}
        host_ip = normalize_host_address(attributes.get("host"))
        if _synthetic_host_address(host_ip):
            host_ip = ""
        attributes["endpoint_resolved"] = bool(host_ip)
        host = hosts_by_ip.get(_scoped_host_lookup_key(attributes, host_ip)) if host_ip else None
        if not host:
            host = _upsert_host(db, {
                **asset,
                "asset_type": "host",
                "name": _text(attributes.get("hostname")) or host_ip or f"主机-{asset['id']}",
                "attributes": {
                    **{key: attributes[key] for key in (
                        "region_name", "region_code", "site_name", "site_code", "datacenter",
                        "zone_name", "zone_code", "availability_zone", "environment_name",
                    ) if key in attributes},
                    "host": host_ip,
                    "hostname": attributes.get("hostname"),
                    "endpoint_resolved": bool(host_ip),
                    "environment": attributes.get("environment"),
                    "region": attributes.get("region"),
                    "site": attributes.get("site"),
                    "zone": attributes.get("zone"),
                    "tenant_id": attributes.get("tenant_id"),
                    "organization_id": attributes.get("organization_id"),
                },
            })
            if host.ip:
                hosts_by_ip[_scoped_host_lookup_key(attributes, host.ip)] = host
        seen_host_ids.add(host.id)
        instance_key = _instance_stable_key(asset)
        cluster = _upsert_cluster(db, asset, instance_key)
        instance = _upsert_instance(db, asset, host, cluster)
        seen_cluster_ids.add(cluster.id)
        seen_instance_ids.add(instance.id)
        cluster_instances[cluster.id][instance.id] = instance

        host_relation_key = f"instance:{instance.id}:runs_on:host:{host.id}"
        cluster_relation_key = f"instance:{instance.id}:member_of:cluster:{cluster.id}"
        _upsert_relation(db, host_relation_key, "instance", instance.id, "host", host.id, "runs_on")
        _upsert_relation(db, cluster_relation_key, "instance", instance.id, "cluster", cluster.id, "member_of")
        seen_relation_keys.update({host_relation_key, cluster_relation_key})

    primary_roles = {"primary", "adg_primary", "alwayson_primary", "coordinator", "control_node"}
    secondary_roles = {"replica", "standby", "read_only", "adg_standby", "alwayson_secondary", "candidate", "data_node"}
    conflicts: list[str] = []
    for cluster_id, member_map in cluster_instances.items():
        members = list(member_map.values())
        cluster = db.get(AssetCluster, cluster_id)
        primaries = [item for item in members if item.role in primary_roles]
        secondaries = [item for item in members if item.role in secondary_roles]
        if cluster and cluster.architecture not in {"rac", "mgr", "sharding"} and len(primaries) > 1:
            conflicts.append(f"{cluster.name} 识别到多个主实例")
        for primary in primaries:
            for secondary in secondaries:
                relation_key = f"instance:{primary.id}:replicates_to:instance:{secondary.id}"
                _upsert_relation(
                    db,
                    relation_key,
                    "instance",
                    primary.id,
                    "instance",
                    secondary.id,
                    "replicates_to",
                    {"cluster_id": cluster_id},
                )
                seen_relation_keys.add(relation_key)

        if cluster and "status" not in _manual_fields(cluster):
            cluster.status = _aggregate_status(members)
        if cluster:
            _apply_cluster_discovery_summary(cluster, members)

    host_instances: dict[int, dict[int, AssetInstance]] = defaultdict(dict)
    for member_map in cluster_instances.values():
        for instance in member_map.values():
            host_instances[instance.host_id][instance.id] = instance
    for host_id, member_map in host_instances.items():
        host = db.get(AssetHost, host_id)
        if host and "status" not in _manual_fields(host):
            host.status = _aggregate_status(list(member_map.values()))

    current_host_ids = {
        row.host_id
        for row in db.query(AssetInstance).filter(AssetInstance.id.in_(seen_instance_ids)).all()
    } if seen_instance_ids else set()
    for row in db.query(AssetHost).filter(AssetHost.discovery_source != "manual").all():
        if row.id not in seen_host_ids:
            attributes = row.attributes if isinstance(row.attributes, dict) else {}
            unresolved_placeholder = (
                attributes.get("endpoint_resolved") is False
                or _synthetic_host_address(row.ip)
            )
            if unresolved_placeholder and "status" not in _manual_fields(row):
                row.status = "inactive"
            elif "status" not in _manual_fields(row):
                row.status = "inactive" if source_type == "internal" and row.id not in current_host_ids else "pending_confirmation"
    for row in db.query(AssetCluster).filter(AssetCluster.discovery_source != "manual").all():
        if row.id not in seen_cluster_ids and "status" not in _manual_fields(row):
            # Clusters are derived from their active members. When every member has
            # moved to another detected topology, retaining the old cluster as
            # pending creates an empty duplicate in the topology selector.
            row.status = "inactive"
    for row in db.query(AssetInstance).filter(AssetInstance.discovery_source != "manual").all():
        if row.id not in seen_instance_ids and "status" not in _manual_fields(row):
            row.status = "pending_confirmation"
    for row in db.query(AssetRelation).filter(AssetRelation.discovery_source == "automatic").all():
        if row.relation_key not in seen_relation_keys:
            row.status = "inactive"

    pending = sum(1 for row in database_assets if (
        (row.get("attributes") or {}).get("discovery_status") == "failed"
        or normalize_instance_role(
            (row.get("attributes") or {}).get("role_type"),
            normalize_architecture((row.get("attributes") or {}).get("architecture_type")),
        ) == "unknown"
    ))
    warnings = list((summary or {}).get("warnings") or [])
    batch = None
    if record_batch:
        batch = AssetDiscoveryBatch(
            source_id=source_id,
            source_name=source_name,
            source_type=source_type,
            status="warning" if conflicts or warnings else "success",
            discovered_count=len(database_assets) + len(seen_host_ids),
            suggested_cluster_count=len(seen_cluster_ids),
            pending_instance_count=pending,
            conflict_count=len(conflicts),
            summary={**(summary or {}), "conflicts": conflicts, "warnings": warnings},
            started_at=started_at,
            finished_at=datetime.now(),
        )
        db.add(batch)
    db.flush()
    _retire_unused_legacy_location_defaults(db, inventory)
    db.commit()
    return {
        "batch_id": batch.id if batch else None,
        "host_count": len(seen_host_ids),
        "cluster_count": len(seen_cluster_ids),
        "instance_count": len(seen_instance_ids),
        "pending_instance_count": pending,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def _location_maps(db: Session) -> dict[str, dict[int, Any]]:
    return {
        "regions": {row.id: row for row in db.query(AssetRegion).all()},
        "sites": {row.id: row for row in db.query(AssetSite).all()},
        "zones": {row.id: row for row in db.query(AssetZone).all()},
        "environments": {row.id: row for row in db.query(AssetEnvironment).all()},
    }


def _location_payload(row: Any, maps: dict[str, dict[int, Any]]) -> dict[str, Any]:
    region = maps["regions"].get(row.region_id)
    site = maps["sites"].get(row.site_id)
    zone = maps["zones"].get(row.zone_id)
    environment = maps["environments"].get(row.env_id)
    return {
        "region_id": row.region_id,
        "region": region.name if region else None,
        "site_id": row.site_id,
        "site": site.name if site else None,
        "zone_id": row.zone_id,
        "zone": zone.name if zone else None,
        "env_id": row.env_id,
        "environment": environment.name if environment else None,
        "env_type": environment.env_type if environment else None,
    }


def serialize_host(row: AssetHost, maps: dict[str, dict[int, Any]], instance_count: int = 0, db_types: list[str] | None = None) -> dict[str, Any]:
    attributes = row.attributes or {}
    endpoint_resolved = (
        (attributes.get("endpoint_resolved") is not False or "ip" in _manual_fields(row))
        and bool(row.ip)
        and not _synthetic_host_address(row.ip)
    )
    return {
        "id": row.id,
        "name": row.name,
        "hostname": row.hostname,
        "ip": row.ip if endpoint_resolved else "",
        "os_type": row.os_type,
        "os_version": row.os_version,
        "cpu_cores": row.cpu_cores,
        "cpu_model": row.cpu_model,
        "memory_mb": row.memory_mb,
        "status": row.status,
        "instance_count": instance_count,
        "db_types": db_types or [],
        "source_asset_id": row.source_asset_id,
        "discovery_source": row.discovery_source,
        "manually_corrected": bool(row.manual_fields),
        "attributes": attributes,
        "last_seen_at": row.last_seen_at,
        **_location_payload(row, maps),
    }


def serialize_cluster(row: AssetCluster, maps: dict[str, dict[int, Any]], members: list[AssetInstance] | None = None) -> dict[str, Any]:
    members = members or []
    host_count = len({item.host_id for item in members})
    return {
        "id": row.id,
        "name": row.name,
        "db_type": row.db_type,
        "cluster_type": row.cluster_type,
        "architecture": row.architecture,
        "status": row.status,
        "instance_count": len(members),
        "host_count": host_count,
        "source_asset_id": row.source_asset_id,
        "discovery_source": row.discovery_source,
        "manually_corrected": bool(row.manual_fields),
        "attributes": row.attributes or {},
        "last_seen_at": row.last_seen_at,
        **_location_payload(row, maps),
    }


def serialize_instance(
    row: AssetInstance,
    maps: dict[str, dict[int, Any]],
    host: AssetHost | None = None,
    cluster: AssetCluster | None = None,
) -> dict[str, Any]:
    attributes = row.attributes or {}
    endpoint_resolved = (
        (attributes.get("endpoint_resolved") is not False or "ip" in _manual_fields(row))
        and bool(row.ip)
        and not _synthetic_host_address(row.ip)
    )
    endpoint_ip = row.ip if endpoint_resolved else ""
    host_ip = host.ip if host and not _synthetic_host_address(host.ip) else ""
    return {
        "id": row.id,
        "name": row.name,
        "db_type": row.db_type,
        "ip": endpoint_ip,
        "port": row.port,
        "endpoint": f"{endpoint_ip}:{row.port}" if endpoint_ip else "",
        "version": row.version,
        "role": row.role,
        "status": row.status,
        "host_id": row.host_id,
        "host_name": host.name if host and not _synthetic_host_address(host.name) else None,
        "host_ip": host_ip if host else None,
        "os_type": host.os_type if host else None,
        "os_version": host.os_version if host else None,
        "cluster_id": row.cluster_id,
        "cluster_name": cluster.name if cluster else None,
        "architecture": cluster.architecture if cluster else None,
        "native_instance_id": row.native_instance_id,
        "source_asset_id": row.source_asset_id,
        "discovery_source": row.discovery_source,
        "manually_corrected": bool(row.manual_fields),
        "name_customized": "name" in _manual_fields(row),
        "replication_delay_seconds": attributes.get("replication_delay_seconds"),
        "transport_lag_seconds": attributes.get("transport_lag_seconds"),
        "apply_lag_seconds": attributes.get("apply_lag_seconds"),
        "transport_lag": attributes.get("transport_lag"),
        "apply_lag": attributes.get("apply_lag"),
        "apply_finish_time": attributes.get("apply_finish_time"),
        "lag_time_computed": attributes.get("lag_time_computed"),
        "lag_datum_time": attributes.get("lag_datum_time"),
        "last_verified_at": attributes.get("last_verified_at"),
        "attributes": attributes,
        "last_seen_at": row.last_seen_at,
        **_location_payload(row, maps),
    }


def list_hosts(
    db: Session,
    include_inactive: bool = False,
    allowed_instance_ids: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    maps = _location_maps(db)
    instances = db.query(AssetInstance).filter(
        AssetInstance.status.notin_(["merged", "inactive"]),
    ).all()
    if allowed_instance_ids is not None:
        instances = [row for row in instances if row.id in allowed_instance_ids]
    by_host: dict[int, list[AssetInstance]] = defaultdict(list)
    for instance in instances:
        by_host[instance.host_id].append(instance)
    query = db.query(AssetHost)
    if not include_inactive:
        query = query.filter(AssetHost.status.notin_(["merged", "inactive"]))
    allowed_host_ids = set(by_host) if allowed_instance_ids is not None else None
    return [
        serialize_host(row, maps, len(by_host[row.id]), sorted({item.db_type for item in by_host[row.id]}))
        for row in query.order_by(AssetHost.ip.asc()).all()
        if allowed_host_ids is None or row.id in allowed_host_ids
    ]


def list_clusters(
    db: Session,
    include_inactive: bool = False,
    allowed_instance_ids: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    maps = _location_maps(db)
    instances = db.query(AssetInstance).filter(
        AssetInstance.status.notin_(["merged", "inactive"]),
    ).all()
    if allowed_instance_ids is not None:
        instances = [row for row in instances if row.id in allowed_instance_ids]
    by_cluster: dict[int, list[AssetInstance]] = defaultdict(list)
    for instance in instances:
        if instance.cluster_id:
            by_cluster[instance.cluster_id].append(instance)
    instance_clusters = {item.id: item.cluster_id for item in instances if item.cluster_id}
    relations_by_cluster: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for relation in db.query(AssetRelation).filter(
        AssetRelation.status == "active",
        AssetRelation.source_type == "instance",
        AssetRelation.relation_type.in_(["replicates_to", "member_of"]),
    ).order_by(AssetRelation.id.asc()).all():
        cluster_id = instance_clusters.get(relation.source_id)
        if cluster_id is None:
            continue
        is_replication = (
            relation.relation_type == "replicates_to"
            and relation.target_type == "instance"
            and relation.source_id != relation.target_id
            and instance_clusters.get(relation.target_id) == cluster_id
        )
        is_membership = (
            relation.relation_type == "member_of"
            and relation.target_type == "cluster"
            and relation.target_id == cluster_id
        )
        if is_replication or is_membership:
            relations_by_cluster[cluster_id].append({
                "id": relation.id,
                "source_type": relation.source_type,
                "source_id": relation.source_id,
                "target_type": relation.target_type,
                "target_id": relation.target_id,
                "relation_type": relation.relation_type,
                "status": relation.status,
            })
    query = db.query(AssetCluster)
    if not include_inactive:
        query = query.filter(AssetCluster.status.notin_(["merged", "inactive"]))
    rows = query.order_by(AssetCluster.db_type.asc(), AssetCluster.name.asc()).all()
    return [
        {**serialize_cluster(row, maps, by_cluster[row.id]), "relations": relations_by_cluster[row.id]}
        for row in rows
        if (include_inactive or by_cluster[row.id] or row.discovery_source == "manual")
        and (allowed_instance_ids is None or bool(by_cluster[row.id]))
    ]


def list_instances(
    db: Session,
    include_inactive: bool = False,
    allowed_instance_ids: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    maps = _location_maps(db)
    hosts = {row.id: row for row in db.query(AssetHost).all()}
    clusters = {row.id: row for row in db.query(AssetCluster).all()}
    query = db.query(AssetInstance)
    if not include_inactive:
        query = query.filter(AssetInstance.status.notin_(["merged", "inactive"]))
    if allowed_instance_ids is not None:
        query = query.filter(AssetInstance.id.in_(allowed_instance_ids))
    return [
        serialize_instance(row, maps, hosts.get(row.host_id), clusters.get(row.cluster_id))
        for row in query.order_by(AssetInstance.db_type.asc(), AssetInstance.ip.asc(), AssetInstance.port.asc()).all()
    ]


def overview(db: Session, allowed_instance_ids: set[int] | frozenset[int] | None = None) -> dict[str, Any]:
    hosts = list_hosts(db, allowed_instance_ids=allowed_instance_ids)
    clusters = list_clusters(db, allowed_instance_ids=allowed_instance_ids)
    instances = list_instances(db, allowed_instance_ids=allowed_instance_ids)
    db_counts = Counter(item["db_type"] for item in instances)
    region_counts = Counter(item["region"] or "未分配" for item in instances)
    environment_counts = Counter(item["environment"] or "未分配" for item in instances)
    configured_regions = {row.id for row in db.query(AssetRegion).filter(AssetRegion.code != "region-unconfigured").all()}
    configured_environments = {row.id for row in db.query(AssetEnvironment).filter(AssetEnvironment.code != "env-unconfigured").all()}
    return {
        "host_count": len(hosts),
        "cluster_count": len(clusters),
        "instance_count": len(instances),
        "db_type_counts": {name: db_counts.get(name, 0) for name in sorted(DB_TYPES)},
        "region_distribution": [{"name": name, "count": count} for name, count in region_counts.most_common()],
        "environment_distribution": [{"name": name, "count": count} for name, count in environment_counts.most_common()],
        "configured_region_count": len({item["region_id"] for item in instances if item["region_id"] in configured_regions}),
        "configured_environment_count": len({item["env_id"] for item in instances if item["env_id"] in configured_environments}),
        "healthy_count": sum(1 for item in instances if item["status"] == "healthy"),
        "warning_count": sum(1 for item in instances if item["status"] in {"warning", "critical", "unhealthy"}),
        "pending_count": sum(1 for item in instances if item["status"] == "pending_confirmation" or item["role"] == "unknown"),
    }


def asset_tree(db: Session, allowed_instance_ids: set[int] | frozenset[int] | None = None) -> dict[str, Any]:
    maps = _location_maps(db)
    hosts = {item["id"]: item for item in list_hosts(db, allowed_instance_ids=allowed_instance_ids)}
    clusters = {item["id"]: item for item in list_clusters(db, allowed_instance_ids=allowed_instance_ids)}
    instances = list_instances(db, allowed_instance_ids=allowed_instance_ids)
    cluster_members_by_host: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for instance in instances:
        if instance["host_id"] in hosts and instance["cluster_id"] in clusters:
            cluster_members_by_host[(instance["host_id"], instance["cluster_id"])].append(instance)

    host_nodes: dict[int, dict[str, Any]] = {}
    for host_id, host in hosts.items():
        cluster_nodes = []
        for (member_host_id, cluster_id), members in cluster_members_by_host.items():
            if member_host_id == host_id:
                cluster_nodes.append({**clusters[cluster_id], "instances": members})
        host_nodes[host_id] = {**host, "clusters": cluster_nodes}

    regions: list[dict[str, Any]] = []
    for region in sorted(maps["regions"].values(), key=lambda item: item.name):
        site_nodes = []
        for site in sorted((item for item in maps["sites"].values() if item.region_id == region.id), key=lambda item: item.name):
            zone_nodes = []
            for zone in sorted((item for item in maps["zones"].values() if item.site_id == site.id), key=lambda item: item.name):
                env_nodes = []
                for env in sorted((item for item in maps["environments"].values() if item.zone_id == zone.id), key=lambda item: item.name):
                    scoped_hosts = [node for node in host_nodes.values() if node["env_id"] == env.id]
                    if scoped_hosts:
                        env_nodes.append({"id": env.id, "name": env.name, "env_type": env.env_type, "hosts": scoped_hosts})
                if env_nodes:
                    zone_nodes.append({"id": zone.id, "name": zone.name, "environments": env_nodes})
            if zone_nodes:
                site_nodes.append({"id": site.id, "name": site.name, "zones": zone_nodes})
        if site_nodes:
            regions.append({"id": region.id, "name": region.name, "sites": site_nodes})
    return {"regions": regions, "summary": overview(db, allowed_instance_ids=allowed_instance_ids)}


def get_host(db: Session, host_id: int) -> dict[str, Any] | None:
    row = db.get(AssetHost, host_id)
    if not row:
        return None
    host = next((item for item in list_hosts(db, include_inactive=True) if item["id"] == host_id), None)
    if host:
        host["instances"] = [item for item in list_instances(db, include_inactive=True) if item["host_id"] == host_id]
    return host


def get_cluster(db: Session, cluster_id: int) -> dict[str, Any] | None:
    row = db.get(AssetCluster, cluster_id)
    if not row:
        return None
    cluster = next((item for item in list_clusters(db, include_inactive=True) if item["id"] == cluster_id), None)
    if cluster:
        cluster["instances"] = [item for item in list_instances(db, include_inactive=True) if item["cluster_id"] == cluster_id]
        cluster["relations"] = [
            {
                "id": relation.id,
                "source_id": relation.source_id,
                "target_id": relation.target_id,
                "relation_type": relation.relation_type,
            }
            for relation in db.query(AssetRelation).filter(
                AssetRelation.status == "active",
                AssetRelation.relation_type == "replicates_to",
            ).all()
            if relation.source_id in {item["id"] for item in cluster["instances"]}
            and relation.target_id in {item["id"] for item in cluster["instances"]}
        ]
    return cluster


def get_instance(db: Session, instance_id: int) -> dict[str, Any] | None:
    row = db.get(AssetInstance, instance_id)
    if not row:
        return None
    return next((item for item in list_instances(db, include_inactive=True) if item["id"] == instance_id), None)


def list_discovery_batches(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "source_id": row.source_id,
            "source_name": row.source_name,
            "source_type": row.source_type,
            "status": row.status,
            "discovered_count": row.discovered_count,
            "suggested_cluster_count": row.suggested_cluster_count,
            "pending_instance_count": row.pending_instance_count,
            "conflict_count": row.conflict_count,
            "summary": row.summary or {},
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in db.query(AssetDiscoveryBatch).order_by(AssetDiscoveryBatch.id.desc()).limit(limit).all()
    ]


def list_corrections(db: Session, limit: int = 200) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "before": row.before_value,
            "after": row.after_value,
            "operator": row.operator,
            "reason": row.reason,
            "create_time": row.create_time,
        }
        for row in db.query(AssetCorrectionAudit).order_by(AssetCorrectionAudit.id.desc()).limit(limit).all()
    ]


MODEL_BY_TYPE = {"host": AssetHost, "cluster": AssetCluster, "instance": AssetInstance}
EDITABLE_FIELDS = {
    "host": {"name", "hostname", "ip", "os_type", "os_version", "cpu_cores", "cpu_model", "memory_mb", "region_id", "site_id", "zone_id", "env_id", "status"},
    "cluster": {"name", "db_type", "cluster_type", "architecture", "region_id", "site_id", "zone_id", "env_id", "status"},
    "instance": {"name", "db_type", "ip", "port", "version", "role", "host_id", "cluster_id", "region_id", "site_id", "zone_id", "env_id", "status"},
}


def _snapshot(row: Any) -> dict[str, Any]:
    fields = EDITABLE_FIELDS[type_name(row)] | {"id"}
    return {field: getattr(row, field) for field in fields}


def type_name(row: Any) -> str:
    if isinstance(row, AssetHost):
        return "host"
    if isinstance(row, AssetCluster):
        return "cluster"
    return "instance"


def _validate_changes(entity_type: str, changes: dict[str, Any]) -> dict[str, Any]:
    unsupported = set(changes) - EDITABLE_FIELDS[entity_type]
    if unsupported:
        raise ValueError(f"Unsupported correction fields: {', '.join(sorted(unsupported))}")
    normalized = dict(changes)
    if "db_type" in normalized:
        normalized["db_type"] = normalize_db_type(normalized["db_type"])
    if "architecture" in normalized:
        architecture = normalize_architecture(normalized["architecture"])
        if architecture not in ARCHITECTURES:
            raise ValueError("Unsupported architecture")
        normalized["architecture"] = architecture
    if "role" in normalized:
        role = _text(normalized["role"]).lower()
        if role not in ROLES:
            raise ValueError("Unsupported instance role")
        normalized["role"] = role
    return normalized


def apply_correction(db: Session, payload: dict[str, Any], operator: str) -> dict[str, Any]:
    action = _text(payload.get("action") or "update")
    entity_type = _text(payload.get("entity_type"))
    reason = _text(payload.get("reason"))
    if entity_type not in MODEL_BY_TYPE:
        raise ValueError("entity_type must be host, cluster or instance")
    if not reason:
        raise ValueError("Correction reason is required")
    model = MODEL_BY_TYPE[entity_type]
    entity_id = int(payload.get("entity_id") or 0)
    row = db.get(model, entity_id)
    if not row:
        raise ValueError("Asset entity not found")
    before: dict[str, Any] = _snapshot(row)

    if action in {"update", "change_role", "move_instance"}:
        changes = dict(payload.get("changes") or {})
        if action == "change_role" and payload.get("target_role"):
            changes["role"] = payload["target_role"]
        if action == "move_instance" and payload.get("target_id"):
            changes["cluster_id"] = payload["target_id"]
        changes = _validate_changes(entity_type, changes)
        if entity_type == "instance" and "host_id" in changes and not db.get(AssetHost, int(changes["host_id"])):
            raise ValueError("Target host not found")
        if entity_type == "instance" and "cluster_id" in changes and changes["cluster_id"] is not None and not db.get(AssetCluster, int(changes["cluster_id"])):
            raise ValueError("Target cluster not found")
        for field, value in changes.items():
            setattr(row, field, value)
        row.manual_fields = sorted(_manual_fields(row) | set(changes))
        after = _snapshot(row)
    elif action == "split_cluster":
        if entity_type != "cluster":
            raise ValueError("split_cluster requires a cluster entity")
        instance_ids = [int(item) for item in payload.get("entity_ids") or []]
        members = db.query(AssetInstance).filter(
            AssetInstance.cluster_id == row.id,
            AssetInstance.id.in_(instance_ids),
        ).all() if instance_ids else []
        if not members:
            raise ValueError("No cluster members selected")
        changes = _validate_changes("cluster", dict(payload.get("changes") or {}))
        new_cluster = AssetCluster(
            stable_key=f"manual-cluster:{uuid4().hex}",
            name=_text(changes.pop("name", "")) or f"{row.name} 拆分组",
            db_type=changes.pop("db_type", row.db_type),
            cluster_type=changes.pop("cluster_type", row.cluster_type),
            architecture=changes.pop("architecture", row.architecture),
            region_id=changes.pop("region_id", row.region_id),
            site_id=changes.pop("site_id", row.site_id),
            zone_id=changes.pop("zone_id", row.zone_id),
            env_id=changes.pop("env_id", row.env_id),
            status=changes.pop("status", "healthy"),
            discovery_source="manual",
            manual_fields=["name", "db_type", "architecture"],
            attributes={"split_from_cluster_id": row.id},
        )
        db.add(new_cluster)
        db.flush()
        for member in members:
            member.cluster_id = new_cluster.id
            member.manual_fields = sorted(_manual_fields(member) | {"cluster_id"})
        after = {"source_cluster": _snapshot(row), "new_cluster": _snapshot(new_cluster), "instance_ids": instance_ids}
    elif action == "merge":
        source_ids = [int(item) for item in payload.get("entity_ids") or [] if int(item) != row.id]
        sources = db.query(model).filter(model.id.in_(source_ids)).all() if source_ids else []
        if not sources:
            raise ValueError("No duplicate assets selected")
        if entity_type == "host":
            members = db.query(AssetInstance).filter(AssetInstance.host_id.in_(source_ids)).all()
            for member in members:
                member.host_id = row.id
                member.region_id = row.region_id
                member.site_id = row.site_id
                member.zone_id = row.zone_id
                member.env_id = row.env_id
                member.manual_fields = sorted(_manual_fields(member) | {
                    "host_id", "region_id", "site_id", "zone_id", "env_id",
                })
        elif entity_type == "cluster":
            members = db.query(AssetInstance).filter(AssetInstance.cluster_id.in_(source_ids)).all()
            for member in members:
                member.cluster_id = row.id
                member.manual_fields = sorted(_manual_fields(member) | {"cluster_id"})
        for source in sources:
            source.status = "merged"
            source.manual_fields = sorted(_manual_fields(source) | {"status"})
        after = {"target": _snapshot(row), "merged_ids": source_ids}
    else:
        raise ValueError("Unsupported correction action")

    db.add(AssetCorrectionAudit(
        action=action,
        entity_type=entity_type,
        entity_id=row.id,
        before_value=before,
        after_value=after,
        operator=operator,
        reason=reason,
    ))
    db.commit()
    if entity_type == "host":
        return get_host(db, row.id) or after
    if entity_type == "cluster":
        return get_cluster(db, row.id) or after
    return get_instance(db, row.id) or after
