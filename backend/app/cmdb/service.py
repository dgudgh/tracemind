import ipaddress
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cmdb import normalized_service
from app.cmdb.replication import manual_relation, upstream_evidence
from app.cmdb.peer_connection import oracle_peer_options, verify_peer_identity
from app.cmdb.oracle_network import resolve_oracle_aliases
from app.cmdb.topology_relations import (
    prepare_discovery_relations, reconcile_normalized_relations, saved_topology_edges,
    snapshot_normalized_relations, topology_scope,
)
from app.cmdb.discovery import (
    INTERNAL_SOURCE_NAME,
    INTERNAL_SOURCE_TYPE,
    build_internal_discovery,
    normalize_architecture,
    normalize_host_address,
    normalize_role,
    connection_endpoint_key,
)
from app.cmdb.models import (
    CmdbAsset,
    CmdbAssetOverride,
    CmdbBinding,
    CmdbChangeEvent,
    CmdbRelation,
    CmdbSource,
    CmdbSyncJob,
)
from app.models.db_instance import DBInstance
from app.services import host_overview, instances as instance_service

SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|credential|(?:api|access|private|ssh|encryption)[_-]?key",
    re.IGNORECASE,
)
MAX_SYNC_ATTEMPTS = 3
MAX_TOPOLOGY_PEER_PROBES = 32
ALLOWED_OVERRIDE_FIELDS = {
    "name",
    "status",
    "attributes.architecture_type",
    "attributes.role_type",
    "attributes.topology_group",
    "attributes.cluster_name",
    "attributes.vip",
    "attributes.environment",
    "attributes.system_name",
    "attributes.factory_name",
    "attributes.line_name",
    "attributes.health_status",
    "attributes.owner",
}


def _instance_scope(instance: DBInstance) -> tuple[Any, Any]:
    return instance.tenant_id, instance.organization_id


def _scoped_endpoint(instance: DBInstance, endpoint: tuple) -> tuple:
    scope = _instance_scope(instance)
    return endpoint if scope == (None, None) else (*endpoint, ("scope", *scope))


def _scoped_host_key(instance: DBInstance, host: Any) -> Any:
    normalized = normalize_host_address(host).lower()
    tenant_id, organization_id = _instance_scope(instance)
    if tenant_id is None and organization_id is None:
        return normalized
    return tenant_id, organization_id, normalized


@dataclass(frozen=True)
class SyncResult:
    created_count: int
    updated_count: int
    pending_confirmation_count: int
    sync_job_id: int


def mask_sensitive_payload(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY_PATTERN.search(key):
        return "***"
    if isinstance(value, dict):
        return {str(item_key): mask_sensitive_payload(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [mask_sensitive_payload(item) for item in value]
    return value


def _normalize_asset_items(items: list[dict]) -> list[dict[str, Any]]:
    normalized_items: list[dict[str, Any]] = []
    seen_external_ids: set[str] = set()
    for item in items:
        external_id = str(item.get("external_id") or "").strip()
        asset_type = str(item.get("asset_type") or "").strip()
        name = str(item.get("name") or "").strip()
        if not external_id or not asset_type or not name:
            raise ValueError("external_id, asset_type and name are required")
        if external_id in seen_external_ids:
            continue
        seen_external_ids.add(external_id)
        normalized_items.append({
            "external_id": external_id,
            "asset_type": asset_type,
            "name": name,
            "raw_payload": mask_sensitive_payload(item),
            "attributes": mask_sensitive_payload(item.get("attributes")),
        })
    return normalized_items


def _persist_failed_sync_job(db: Session, source_id: int, error: Exception) -> None:
    now = datetime.now()
    db.add(CmdbSyncJob(
        source_id=source_id,
        status="failed",
        error_message=str(error),
        finished_at=now,
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _run_asset_sync(
    db: Session,
    source_id: int,
    items: list[dict[str, Any]],
    authoritative_snapshot: bool,
) -> SyncResult:
    existing = {
        asset.external_id: asset
        for asset in db.query(CmdbAsset).filter(CmdbAsset.source_id == source_id).all()
    }
    seen_external_ids = {item["external_id"] for item in items}
    created_count = 0
    updated_count = 0
    now = datetime.now()

    for item in items:
        external_id = item["external_id"]
        asset = existing.get(external_id)
        if asset:
            asset.asset_type = item["asset_type"]
            asset.name = item["name"]
            asset.status = "active"
            asset.raw_payload = item["raw_payload"]
            asset.attributes = item["attributes"]
            asset.last_seen_at = now
            updated_count += 1
        else:
            db.add(CmdbAsset(
                source_id=source_id,
                external_id=external_id,
                asset_type=item["asset_type"],
                name=item["name"],
                status="active",
                raw_payload=item["raw_payload"],
                attributes=item["attributes"],
                last_seen_at=now,
            ))
            created_count += 1

    pending_confirmation_count = 0
    if authoritative_snapshot and items:
        for external_id, asset in existing.items():
            if external_id not in seen_external_ids and asset.status not in {"pending_confirmation", "inactive"}:
                asset.status = "pending_confirmation"
                pending_confirmation_count += 1

    sync_job = CmdbSyncJob(
        source_id=source_id,
        status="success",
        created_count=created_count,
        updated_count=updated_count,
        pending_confirmation_count=pending_confirmation_count,
        finished_at=now,
    )
    db.add(sync_job)
    db.flush()
    sync_job_id = sync_job.id
    db.commit()
    return SyncResult(created_count, updated_count, pending_confirmation_count, sync_job_id)


def upsert_assets(
    db: Session,
    source_id: int,
    items: list[dict],
    authoritative_snapshot: bool = False,
) -> SyncResult:
    source = db.get(CmdbSource, source_id)
    if not source:
        error = ValueError("CMDB source not found")
        db.rollback()
        _persist_failed_sync_job(db, source_id, error)
        raise error
    if not source.enabled:
        error = ValueError("CMDB source must be enabled before synchronization")
        db.rollback()
        _persist_failed_sync_job(db, source_id, error)
        raise error

    try:
        normalized_items = _normalize_asset_items(items)
    except Exception as exc:
        db.rollback()
        _persist_failed_sync_job(db, source_id, exc)
        raise

    for attempt in range(MAX_SYNC_ATTEMPTS):
        try:
            return _run_asset_sync(db, source_id, normalized_items, authoritative_snapshot)
        except IntegrityError as exc:
            db.rollback()
            if attempt + 1 < MAX_SYNC_ATTEMPTS:
                continue
            _persist_failed_sync_job(db, source_id, exc)
            raise
        except Exception as exc:
            db.rollback()
            _persist_failed_sync_job(db, source_id, exc)
            raise

    raise RuntimeError("CMDB sync retry loop exhausted")


def serialize_asset(asset: CmdbAsset) -> dict:
    return {
        "id": asset.id,
        "source_id": asset.source_id,
        "external_id": asset.external_id,
        "asset_type": asset.asset_type,
        "name": asset.name,
        "status": asset.status,
        "attributes": asset.attributes,
        "last_seen_at": asset.last_seen_at,
    }


def serialize_source(source: CmdbSource) -> dict:
    return {
        "id": source.id,
        "source_name": source.source_name,
        "source_type": source.source_type,
        "endpoint": source.endpoint,
        "enabled": source.enabled,
        "remark": source.remark,
        "create_time": source.create_time,
        "update_time": source.update_time,
    }


def list_sources(db: Session) -> list[dict]:
    return [serialize_source(row) for row in db.query(CmdbSource).order_by(CmdbSource.id.desc()).all()]


def create_source(db: Session, data: dict) -> dict:
    source = CmdbSource(**data)
    db.add(source)
    db.commit()
    db.refresh(source)
    return serialize_source(source)


def update_source(db: Session, source_id: int, data: dict) -> dict | None:
    source = db.get(CmdbSource, source_id)
    if not source:
        return None
    for field in ("source_name", "source_type", "endpoint", "enabled", "remark"):
        if field in data:
            setattr(source, field, data[field])
    db.commit()
    db.refresh(source)
    return serialize_source(source)


def _asset_ids_for_native_scope(db: Session, allowed_native_instance_ids: set[int] | None) -> set[int] | None:
    if allowed_native_instance_ids is None:
        return None
    if not allowed_native_instance_ids:
        return set()
    assets = db.query(CmdbAsset).all()
    asset_types = {row.id: row.asset_type for row in assets}
    allowed_asset_ids: set[int] = set()
    for row in assets:
        attributes = row.attributes or {}
        native_id = attributes.get("native_instance_id")
        connection_ids = set(attributes.get("connection_instance_ids") or [])
        host_instance_ids = set(attributes.get("database_instance_ids") or [])
        if native_id in allowed_native_instance_ids or connection_ids.intersection(allowed_native_instance_ids):
            allowed_asset_ids.add(row.id)
        elif row.asset_type == "host" and host_instance_ids.intersection(allowed_native_instance_ids):
            allowed_asset_ids.add(row.id)

    allowed_database_ids = {
        asset_id for asset_id in allowed_asset_ids if asset_types.get(asset_id) == "database_instance"
    }
    if allowed_database_ids:
        relations = db.query(CmdbRelation).filter(CmdbRelation.status == "active").all()
        for relation in relations:
            if relation.source_asset_id in allowed_database_ids and asset_types.get(relation.target_asset_id) != "database_instance":
                allowed_asset_ids.add(relation.target_asset_id)
            if relation.target_asset_id in allowed_database_ids and asset_types.get(relation.source_asset_id) != "database_instance":
                allowed_asset_ids.add(relation.source_asset_id)
    return allowed_asset_ids


def list_assets(
    db: Session,
    source_id: int | None = None,
    status: str | None = None,
    allowed_native_instance_ids: set[int] | None = None,
) -> list[dict]:
    query = db.query(CmdbAsset)
    allowed_asset_ids = _asset_ids_for_native_scope(db, allowed_native_instance_ids)
    if allowed_asset_ids is not None:
        if not allowed_asset_ids:
            return []
        query = query.filter(CmdbAsset.id.in_(allowed_asset_ids))
    if source_id is not None:
        query = query.filter(CmdbAsset.source_id == source_id)
    if status:
        query = query.filter(CmdbAsset.status == status)
    return [serialize_asset(row) for row in query.order_by(CmdbAsset.id.desc()).all()]


def serialize_relation(relation: CmdbRelation) -> dict:
    return {
        "id": relation.id,
        "source_id": relation.source_id,
        "external_id": relation.external_id,
        "source_asset_id": relation.source_asset_id,
        "target_asset_id": relation.target_asset_id,
        "relation_type": relation.relation_type,
        "status": relation.status,
    }


def list_relations(
    db: Session,
    source_id: int | None = None,
    allowed_native_instance_ids: set[int] | None = None,
) -> list[dict]:
    query = db.query(CmdbRelation)
    allowed_asset_ids = _asset_ids_for_native_scope(db, allowed_native_instance_ids)
    if allowed_asset_ids is not None:
        if not allowed_asset_ids:
            return []
        query = query.filter(
            CmdbRelation.source_asset_id.in_(allowed_asset_ids),
            CmdbRelation.target_asset_id.in_(allowed_asset_ids),
        )
    if source_id is not None:
        query = query.filter(CmdbRelation.source_id == source_id)
    return [serialize_relation(row) for row in query.order_by(CmdbRelation.id.desc()).all()]


def upsert_relation(db: Session, data: dict) -> dict:
    source_id = data["source_id"]
    source = db.get(CmdbSource, source_id)
    if not source:
        raise ValueError("CMDB source not found")
    if not source.enabled:
        raise ValueError("CMDB source must be enabled")

    source_asset = db.get(CmdbAsset, data["source_asset_id"])
    target_asset = db.get(CmdbAsset, data["target_asset_id"])
    if not source_asset or not target_asset:
        raise ValueError("CMDB relation asset not found")
    if source_asset.source_id != source_id or target_asset.source_id != source_id:
        raise ValueError("CMDB relation assets must belong to the same CMDB source")
    if source_asset.status != "active" or target_asset.status != "active":
        raise ValueError("CMDB relation assets must be active")

    external_id = (data.get("external_id") or "").strip() or None
    relation = None
    if external_id:
        relation = (
            db.query(CmdbRelation)
            .filter(CmdbRelation.source_id == source_id, CmdbRelation.external_id == external_id)
            .first()
        )
    if not relation:
        relation = (
            db.query(CmdbRelation)
            .filter(
                CmdbRelation.source_id == source_id,
                CmdbRelation.source_asset_id == data["source_asset_id"],
                CmdbRelation.target_asset_id == data["target_asset_id"],
                CmdbRelation.relation_type == data["relation_type"],
            )
            .first()
        )
    if not relation:
        relation = CmdbRelation(
            source_id=source_id,
            source_asset_id=data["source_asset_id"],
            target_asset_id=data["target_asset_id"],
            relation_type=data["relation_type"],
        )
        db.add(relation)
    relation.external_id = external_id
    relation.status = data.get("status", "active")
    relation.raw_payload = mask_sensitive_payload(data.get("raw_payload"))
    db.commit()
    db.refresh(relation)
    return serialize_relation(relation)


def serialize_binding(binding: CmdbBinding) -> dict:
    return {
        "id": binding.id,
        "instance_id": binding.instance_id,
        "asset_id": binding.asset_id,
        "binding_status": binding.binding_status,
        "match_rule": binding.match_rule,
        "confidence": binding.confidence,
    }


def list_bindings(db: Session, instance_id: int | None = None) -> list[dict]:
    query = db.query(CmdbBinding)
    if instance_id is not None:
        query = query.filter(CmdbBinding.instance_id == instance_id)
    return [serialize_binding(row) for row in query.order_by(CmdbBinding.id.desc()).all()]


def upsert_binding(db: Session, data: dict) -> dict:
    asset = db.get(CmdbAsset, data["asset_id"])
    if not asset:
        raise ValueError("CMDB asset not found")
    if asset.status != "active":
        raise ValueError("CMDB binding asset must be active")

    binding = (
        db.query(CmdbBinding)
        .filter(CmdbBinding.instance_id == data["instance_id"], CmdbBinding.asset_id == data["asset_id"])
        .first()
    )
    if not binding:
        binding = CmdbBinding(instance_id=data["instance_id"], asset_id=data["asset_id"])
        db.add(binding)
    for field in ("binding_status", "match_rule", "confidence"):
        if field in data:
            setattr(binding, field, data[field])
    db.commit()
    db.refresh(binding)
    return serialize_binding(binding)


def list_sync_jobs(db: Session, source_id: int | None = None) -> list[dict]:
    query = db.query(CmdbSyncJob)
    if source_id is not None:
        query = query.filter(CmdbSyncJob.source_id == source_id)
    return [
        {
            "id": job.id,
            "source_id": job.source_id,
            "status": job.status,
            "created_count": job.created_count,
            "updated_count": job.updated_count,
            "pending_confirmation_count": job.pending_confirmation_count,
            "error_message": job.error_message,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        for job in query.order_by(CmdbSyncJob.id.desc()).all()
    ]


def topology(
    db: Session,
    source_id: int | None = None,
    allowed_native_instance_ids: set[int] | None = None,
) -> dict:
    nodes = list_assets(db, source_id=source_id, allowed_native_instance_ids=allowed_native_instance_ids)
    relations = list_relations(db, source_id=source_id, allowed_native_instance_ids=allowed_native_instance_ids)
    return {"nodes": nodes, "relations": relations, "node_count": len(nodes), "relation_count": len(relations)}


def resolve_instance_context(db: Session, instance_id: int) -> dict:
    row = (
        db.query(CmdbBinding, CmdbAsset)
        .join(CmdbAsset, CmdbAsset.id == CmdbBinding.asset_id)
        .filter(CmdbBinding.instance_id == instance_id)
        .filter(CmdbAsset.status == "active")
        .order_by((CmdbBinding.binding_status == "active").desc(), CmdbBinding.confidence.desc(), CmdbBinding.id.desc())
        .first()
    )
    if not row:
        return {"instance_id": instance_id, "binding_status": "unbound"}

    binding, asset = row
    return {
        "instance_id": instance_id,
        "binding_id": binding.id,
        "binding_status": binding.binding_status,
        "match_rule": binding.match_rule,
        "confidence": binding.confidence,
        "asset": serialize_asset(asset),
    }


def _ensure_internal_source(db: Session) -> CmdbSource:
    source = db.query(CmdbSource).filter(CmdbSource.source_name == INTERNAL_SOURCE_NAME).first()
    if source:
        if source.source_type != INTERNAL_SOURCE_TYPE or not source.enabled:
            source.source_type = INTERNAL_SOURCE_TYPE
            source.enabled = 1
            db.commit()
            db.refresh(source)
        return source
    source = CmdbSource(
        source_name=INTERNAL_SOURCE_NAME,
        source_type=INTERNAL_SOURCE_TYPE,
        enabled=1,
        remark="由数据库实例、SSH 配置和数据库架构信息自动生成",
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _discovery_diff(db: Session, source_id: int, bundle) -> dict[str, Any]:
    existing_assets = {
        row.external_id: row
        for row in db.query(CmdbAsset).filter(CmdbAsset.source_id == source_id).all()
    }
    candidate_assets = {item["external_id"]: item for item in bundle.assets}
    created = sorted(set(candidate_assets) - set(existing_assets))
    missing = sorted(set(existing_assets) - set(candidate_assets))
    updated: list[str] = []
    unchanged: list[str] = []
    for external_id in sorted(set(candidate_assets) & set(existing_assets)):
        candidate = candidate_assets[external_id]
        existing = existing_assets[external_id]
        if (
            existing.name != candidate["name"]
            or existing.asset_type != candidate["asset_type"]
            or (existing.attributes or {}) != (candidate.get("attributes") or {})
            or existing.status != "active"
        ):
            updated.append(external_id)
        else:
            unchanged.append(external_id)
    return {
        "created_count": len(created),
        "updated_count": len(updated),
        "missing_count": len(missing),
        "unchanged_count": len(unchanged),
        "relation_count": len(bundle.relations),
        "binding_count": len(bundle.instance_bindings),
        "created": created,
        "updated": updated,
        "missing": missing,
        "warnings": bundle.warnings,
        "collected_at": bundle.collected_at,
    }


def preview_internal_discovery(db: Session) -> dict[str, Any]:
    source = _ensure_internal_source(db)
    bundle = prepare_discovery_relations(build_internal_discovery(db))
    result = _discovery_diff(db, source.id, bundle)
    result["source_id"] = source.id
    result["source_name"] = source.source_name
    return result


def _sync_internal_relations(db: Session, source_id: int, relation_items: list[dict[str, Any]]) -> int:
    assets_by_external_id = {
        row.external_id: row
        for row in db.query(CmdbAsset).filter(CmdbAsset.source_id == source_id).all()
    }
    existing = {
        row.external_id: row
        for row in db.query(CmdbRelation)
        .filter(CmdbRelation.source_id == source_id, CmdbRelation.external_id.is_not(None))
        .all()
    }
    seen: set[str] = set()
    count = 0
    for item in relation_items:
        external_id = item["external_id"]
        source_asset = assets_by_external_id.get(item["source_external_id"])
        target_asset = assets_by_external_id.get(item["target_external_id"])
        if not source_asset or not target_asset:
            continue
        relation = existing.get(external_id)
        if relation and manual_relation(relation.raw_payload):
            seen.add(external_id)
            continue
        if not relation:
            relation = CmdbRelation(source_id=source_id, external_id=external_id)
            db.add(relation)
            existing[external_id] = relation
        relation.source_asset_id = source_asset.id
        relation.target_asset_id = target_asset.id
        relation.relation_type = item["relation_type"]
        relation.status = "active"
        relation.raw_payload = mask_sensitive_payload(item.get("raw_payload"))
        seen.add(external_id)
        count += 1
    for external_id, relation in existing.items():
        if external_id not in seen and relation.status == "active" and not manual_relation(relation.raw_payload):
            relation.status = "pending_confirmation"
    db.commit()
    return count


def _sync_internal_bindings(db: Session, source_id: int, binding_items: list[dict[str, Any]]) -> int:
    assets_by_external_id = {
        row.external_id: row
        for row in db.query(CmdbAsset).filter(CmdbAsset.source_id == source_id).all()
    }
    count = 0
    for item in binding_items:
        asset = assets_by_external_id.get(item["asset_external_id"])
        if not asset:
            continue
        binding = (
            db.query(CmdbBinding)
            .filter(CmdbBinding.instance_id == item["instance_id"], CmdbBinding.asset_id == asset.id)
            .first()
        )
        if not binding:
            binding = CmdbBinding(instance_id=item["instance_id"], asset_id=asset.id)
            db.add(binding)
        binding.binding_status = "active"
        binding.match_rule = item["match_rule"]
        binding.confidence = item["confidence"]
        count += 1
    db.commit()
    return count


def _probe_internal_instances(db: Session) -> tuple[dict[int, dict[str, Any]], list[str], int]:
    instances = db.query(DBInstance).filter(DBInstance.enabled == 1).order_by(DBInstance.id.asc()).all()
    profiles: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    changed = False
    if not instances:
        return profiles, warnings, 0

    max_workers = min(4, len(instances))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="asset-discovery") as executor:
        futures = {executor.submit(instance_service.test_connection, instance): instance for instance in instances}
        for future in as_completed(futures):
            instance = futures[future]
            try:
                detail = future.result() or {}
                architecture = detail.get("architecture")
                if not isinstance(architecture, dict):
                    warnings.append(f"{instance.instance_name} 连接正常，但当前数据库驱动未返回架构信息")
                    continue
                architecture_type = normalize_architecture(
                    architecture.get("architecture_type"),
                    normalize_role(architecture.get("role_type"), instance.db_type),
                    instance.db_type,
                )
                role_type = normalize_role(architecture.get("role_type"), instance.db_type)
                profile = {
                    **mask_sensitive_payload(architecture),
                    "architecture_type": architecture_type,
                    "role_type": role_type,
                    "discovery_status": "success",
                }
                profiles[instance.id] = profile
                if instance.architecture_type != architecture_type:
                    instance.architecture_type = architecture_type
                    changed = True
                if instance.role_type != role_type:
                    instance.role_type = role_type
                    changed = True
            except Exception as exc:
                error_text = re.sub(r"\s+", " ", str(exc)).strip()[:180]
                profiles[instance.id] = {
                    "architecture_type": normalize_architecture(
                        instance.architecture_type,
                        normalize_role(instance.role_type, instance.db_type),
                        instance.db_type,
                    ),
                    "role_type": normalize_role(instance.role_type, instance.db_type),
                    "discovery_status": "failed",
                    "discovery_error": error_text,
                    "probe_failed": True,
                }
                warnings.append(f"{instance.instance_name} 探测失败，已沿用现有元数据：{error_text}")

    configured_endpoints = {
        _scoped_endpoint(instance, connection_endpoint_key(
            instance.db_type, instance.host, instance.port,
            instance.oracle_connect_type, instance.service_name, instance.sid,
        ))
        for instance in instances
    }
    def member_endpoint(
        db_type: str,
        member: dict[str, Any],
        fallback_port: int,
        source: DBInstance | None = None,
    ) -> tuple | None:
        if member.get("endpoint_unresolved") is True:
            return None
        host = normalize_host_address(member.get("host") or member.get("reported_host"))
        if not host:
            return None
        member["host"] = host
        try:
            port = int(member.get("port") or fallback_port)
        except (TypeError, ValueError):
            port = int(fallback_port)
        member["port"] = port
        options = oracle_peer_options(source, member) if source is not None and db_type == "oracle" else member
        endpoint = connection_endpoint_key(db_type, host, port,
            options.get("oracle_connect_type") or (source.oracle_connect_type if source else ""),
            options.get("service_name") or (source.service_name if source else ""),
            options.get("sid") or (source.sid if source else ""))
        return _scoped_endpoint(source, endpoint) if source is not None else endpoint

    def member_identity_tokens(member: dict[str, Any], fallback_port: int) -> set[str]:
        tokens = {
            f"{field}:{str(member.get(field)).strip().lower()}"
            for field in ("member_key", "server_uuid", "server_id")
            if member.get(field) not in (None, "")
        }
        for host_field in ("host", "reported_host"):
            host = normalize_host_address(member.get(host_field))
            if not host:
                continue
            try:
                port = int(member.get("port") or fallback_port)
            except (TypeError, ValueError):
                port = int(fallback_port)
            tokens.add(f"endpoint:{host.lower()}:{port}")
        return tokens

    def merge_member_data(target: dict[str, Any], incoming: dict[str, Any]) -> None:
        authoritative_fields = {
            "role_type",
            "health_status",
            "db_version",
            "replication_delay_seconds",
            "transport_lag_seconds",
            "apply_lag_seconds",
            "transport_lag",
            "apply_lag",
            "apply_finish_time",
            "lag_time_computed",
            "lag_datum_time",
            "member_state",
        }
        for key, value in incoming.items():
            if value in (None, ""):
                continue
            if key in {"role_type", "health_status"} and str(value).strip().lower() == "unknown":
                if target.get(key) not in (None, "", "unknown"):
                    continue
            if key in authoritative_fields or target.get(key) in (None, ""):
                target[key] = deepcopy(value)

    def merge_peer_architecture(
        source: DBInstance,
        source_profile: dict[str, Any],
        peer_member: dict[str, Any],
        peer_architecture: dict[str, Any],
        endpoint: tuple,
    ) -> list[dict[str, Any]]:
        peer_role = normalize_role(peer_architecture.get("role_type"), endpoint[0])
        peer_snapshot = {
            "member_key": peer_architecture.get("member_key"),
            "server_uuid": peer_architecture.get("server_uuid"),
            "server_id": peer_architecture.get("server_id"),
            "name": peer_architecture.get("hostname"),
            "reported_host": peer_architecture.get("hostname"),
            "host": peer_member.get("host") or endpoint[1],
            "port": endpoint[2],
            "role_type": peer_role if peer_role != "unknown" else None,
            "db_version": peer_architecture.get("db_version"),
            "replication_delay_seconds": peer_architecture.get("replication_delay_seconds"),
            "transport_lag_seconds": peer_architecture.get("transport_lag_seconds"),
            "apply_lag_seconds": peer_architecture.get("apply_lag_seconds"),
            "transport_lag": peer_architecture.get("transport_lag"),
            "apply_lag": peer_architecture.get("apply_lag"),
            "apply_finish_time": peer_architecture.get("apply_finish_time"),
            "lag_time_computed": peer_architecture.get("lag_time_computed"),
            "lag_datum_time": peer_architecture.get("lag_datum_time"),
            "health_status": peer_architecture.get("health_status"),
            "direct_probe": True,
        }
        merge_member_data(peer_member, peer_snapshot)
        # Preserve the peer's own endpoint observation, not its parent's role list.
        peer_member["replication"] = upstream_evidence(peer_architecture)
        transport_source = str(peer_member.get("transport_source_db_unique_name") or "").casefold()
        source_unique_name = str(source_profile.get("db_unique_name") or "")
        if (endpoint[0] == "oracle" and source_unique_name and transport_source == source_unique_name.casefold()
                and peer_member["replication"].get("state") != "verified"):
            peer_member["replication"] = {
                "state": "verified", "upstream": {"db_unique_name": source_unique_name},
                "evidence_source": "v$archive_dest_status",
                "destination_id": peer_member.get("transport_destination_id"),
            }

        peer_architecture_type = normalize_architecture(
            peer_architecture.get("architecture_type"),
            peer_role,
            endpoint[0],
        )
        if peer_architecture_type != "standalone":
            source_profile["architecture_type"] = peer_architecture_type
            # Keep the seed's observed group stable while concurrently probing
            # several peers.  A peer may report only its local/cascaded view;
            # completion order must not rename the seed's topology.
            if not source_profile.get("topology_group") and peer_architecture.get("topology_group"):
                source_profile["topology_group"] = peer_architecture["topology_group"]
        if not source_profile.get("cluster_name") and peer_architecture.get("cluster_name"):
            source_profile["cluster_name"] = peer_architecture["cluster_name"]

        members = source_profile.setdefault("members", [])
        if not isinstance(members, list):
            members = []
            source_profile["members"] = members

        source_identity = {
            **source_profile,
            "host": normalize_host_address(source.host),
            "port": int(source.port),
        }
        source_tokens = member_identity_tokens(source_identity, int(source.port))
        member_index: dict[str, dict[str, Any]] = {}
        for existing in members:
            if not isinstance(existing, dict):
                continue
            for token in member_identity_tokens(existing, int(source.port)):
                member_index[token] = existing

        peer_members = peer_architecture.get("members")
        if not isinstance(peer_members, list):
            return [item for item in members if isinstance(item, dict)]

        for nested_member in peer_members:
            if not isinstance(nested_member, dict):
                continue
            nested = deepcopy(nested_member)
            nested_endpoint = member_endpoint(endpoint[0], nested, endpoint[2], source)
            nested_tokens = member_identity_tokens(nested, endpoint[2])
            if source_tokens.intersection(nested_tokens):
                continue

            existing = next(
                (member_index[token] for token in nested_tokens if token in member_index),
                None,
            )
            if existing is None:
                nested["discovered_via_peer"] = True
                members.append(nested)
                existing = nested
            else:
                merge_member_data(existing, nested)
            if nested_endpoint and not existing.get("host"):
                existing["host"] = nested_endpoint[1]
                existing["port"] = nested_endpoint[2]
            for token in member_identity_tokens(existing, endpoint[2]):
                member_index[token] = existing

        return [item for item in members if isinstance(item, dict)]

    configured_identity_index: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    configured_instances = {instance.id: instance for instance in instances}

    def strong_identities(payload: dict[str, Any]) -> set[str]:
        keys = {f"{key}:{str(payload[key]).strip().lower()}"
                for key in ("member_key", "server_uuid", "server_id", "replica_id", "server_name")
                if payload.get(key) not in (None, "")}
        unique_name = str(payload.get("db_unique_name") or "").strip().lower()
        instance_name = str(payload.get("instance_name") or "").strip().lower()
        if unique_name:
            keys.add(f"oracle:{unique_name}:{instance_name}")
            keys.add(f"oracle-db:{unique_name}")
        return keys

    for instance in instances:
        db_type = str(instance.db_type or "").lower()
        profile = profiles.get(instance.id) or {}
        tenant_id, organization_id = _instance_scope(instance)
        identities = strong_identities(profile)
        for host in (instance.host, profile.get("host_ip"), profile.get("hostname")):
            if host:
                identities.add("endpoint:" + repr(connection_endpoint_key(db_type, host, instance.port,
                    instance.oracle_connect_type, instance.service_name, instance.sid)))
        for identity in identities:
            configured_identity_index[
                (tenant_id, organization_id, db_type, identity)
            ].add(instance.id)

    def resolve_configured_member(
        db_type: str,
        source: DBInstance,
        member: dict[str, Any],
    ) -> DBInstance | None:
        scores: Counter[int] = Counter()
        tenant_id, organization_id = _instance_scope(source)
        identities = strong_identities(member)
        # RAC nodes share db_unique_name; instance identity must disambiguate them.
        if member.get("instance_name"):
            identities.discard(f"oracle-db:{str(member.get('db_unique_name') or '').lower()}")
        if not member.get("endpoint_unresolved"):
            for host in (member.get("host"), member.get("reported_host")):
                if host:
                    options = oracle_peer_options(source, member) if db_type == "oracle" else member
                    identities.add("endpoint:" + repr(connection_endpoint_key(db_type, host, member.get('port') or source.port,
                        options.get("oracle_connect_type") or source.oracle_connect_type,
                        options.get("service_name") or source.service_name, options.get("sid") or source.sid)))
        for identity in identities:
            for instance_id in configured_identity_index.get(
                (tenant_id, organization_id, db_type, identity),
                set(),
            ):
                if instance_id != source.id:
                    scores[instance_id] += 1 if identity.startswith("endpoint:") else 10
        if not scores:
            return None
        best_score = max(scores.values())
        best_matches = [
            instance_id
            for instance_id, score in scores.items()
            if score == best_score
        ]
        if len(best_matches) != 1:
            return None
        return configured_instances.get(best_matches[0])

    peer_targets: dict[tuple, tuple[DBInstance, dict[str, Any]]] = {}
    alias_originals: dict[int, dict[str, Any]] = {}
    for instance in instances:
        profile = profiles.get(instance.id) or {}
        members = profile.get("members")
        db_type = str(instance.db_type or "").lower()
        if db_type not in {"mysql", "postgresql", "oracle", "sqlserver"} or not isinstance(members, list):
            continue
        resolved_aliases = None
        for member in members:
            if not isinstance(member, dict):
                continue
            managed_peer = resolve_configured_member(db_type, instance, member)
            if managed_peer:
                managed_profile = profiles.get(managed_peer.id) or {}
                if managed_profile.get("discovery_status") != "success":
                    member.update(direct_probe=False, db_probe_status="failed",
                        probe_error="已登记成员本轮连接验证失败，未确认成员身份")
                    if db_type == "oracle":
                        candidate_host = normalize_host_address(member.get("host") or member.get("reported_host"))
                        if candidate_host:
                            member["endpoint_candidates"] = [{"host": candidate_host, "port": member.get("port") or instance.port}]
                        member.update(host="", reported_host="", endpoint_unresolved=True)
                    warnings.append(f"{member.get('name') or member.get('member_key') or '已发现成员'} 本轮数据库身份未验证，未绑定原生连接")
                    continue
                if managed_profile.get("discovery_status") == "success":
                    try:
                        verify_peer_identity(db_type, member, managed_profile, {
                            **profile, "connection_host": instance.host, "connection_port": instance.port,
                        })
                    except ValueError as exc:
                        member.update(direct_probe=False, db_probe_status="failed", probe_error=str(exc))
                        warnings.append(str(exc))
                        continue
                member["host"] = normalize_host_address(managed_peer.host)
                member["port"] = int(managed_peer.port)
                member["endpoint_unresolved"] = False
                member["managed_instance_id"] = managed_peer.id
                if db_type == "oracle":
                    member.update(oracle_connect_type="sid" if managed_peer.oracle_connect_type == "sid" else "service_name",
                        service_name=managed_peer.service_name, sid=managed_peer.sid)
                if managed_profile.get("discovery_status") == "success":
                    merge_member_data(member, {
                        "role_type": managed_profile.get("role_type"),
                        "db_version": managed_profile.get("db_version"),
                        "replication_delay_seconds": managed_profile.get("replication_delay_seconds"),
                        "transport_lag_seconds": managed_profile.get("transport_lag_seconds"),
                        "apply_lag_seconds": managed_profile.get("apply_lag_seconds"),
                        "transport_lag": managed_profile.get("transport_lag"),
                        "apply_lag": managed_profile.get("apply_lag"),
                        "apply_finish_time": managed_profile.get("apply_finish_time"),
                        "lag_time_computed": managed_profile.get("lag_time_computed"),
                        "lag_datum_time": managed_profile.get("lag_datum_time"),
                        "health_status": managed_profile.get("health_status"),
                        "direct_probe": True,
                        "db_probe_status": "verified",
                    })
            if (db_type == "oracle" and member.get("endpoint_unresolved") is True
                    and member.get("db_unique_name") and member.get("connect_identifier")
                    and profile.get("discovery_status") == "success"):
                if resolved_aliases is None:
                    resolved_aliases = resolve_oracle_aliases(instance, [
                        str(item["connect_identifier"]) for item in members
                        if isinstance(item, dict) and item.get("endpoint_unresolved") is True
                        and item.get("db_unique_name") and item.get("connect_identifier")
                    ])
                resolved = resolved_aliases.get(str(member["connect_identifier"]))
                if resolved and resolved.get("endpoint_unresolved") is False:
                    fields = ("host", "reported_host", "port", "oracle_connect_type", "service_name", "sid", "endpoint_unresolved")
                    alias_originals[id(member)] = {key: member.get(key) for key in fields}
                    member.update({key: resolved[key] for key in fields if key in resolved})
                    member["reported_host"] = resolved["host"]
                elif resolved and resolved.get("endpoint_candidates"):
                    member["endpoint_candidates"] = resolved["endpoint_candidates"]
            endpoint = member_endpoint(db_type, member, int(instance.port), instance)
            if endpoint is None:
                member["db_probe_status"] = "needs_endpoint"
                member["db_probe_reason"] = "成员连接地址、监听端口或服务标识尚未解析，未尝试数据库或 SSH 认证"
                if not profile.get("peer_endpoint_candidates"):
                    warnings.append(f"{member.get('name') or member.get('member_key') or '已发现成员'} 连接端点尚未解析，请补充该节点连接信息；未重复要求数据库密码")
                continue
            if endpoint not in configured_endpoints:
                peer_targets.setdefault(endpoint, (instance, member))

        # SHOW REPLICAS may omit report_host. Replication socket addresses are
        # candidates only: a successful login must still prove the member UUID
        # (or server_id when no UUID was reported) before binding that address.
        if db_type == "mysql" and profile.get("discovery_status") == "success":
            candidates = profile.get("peer_endpoint_candidates") or []
            for candidate in candidates[:MAX_TOPOLOGY_PEER_PROBES]:
                if not isinstance(candidate, dict):
                    continue
                try:
                    host = str(ipaddress.ip_address(str(candidate.get("host") or "")))
                    port = int(candidate.get("port"))
                    if not 1 <= port <= 65535:
                        continue
                except (ValueError, TypeError):
                    continue
                endpoint = _scoped_endpoint(instance, (db_type, host.lower(), port))
                if endpoint not in configured_endpoints:
                    peer_targets.setdefault(endpoint, (instance, {
                        "host": host, "port": port, "endpoint_candidate": True,
                    }))

    certificate_profiles = {
        instance.id: instance.certificate_profile
        for instance in instances if instance.certificate_profile_id
    }

    def match_candidate_member(source: DBInstance, architecture: dict[str, Any], port: int):
        profile = profiles.get(source.id) or {}
        for candidate in profile.get("members") or []:
            if not isinstance(candidate, dict) or int(candidate.get("port") or source.port) != port:
                continue
            if normalize_host_address(candidate.get("host") or candidate.get("reported_host")):
                continue
            expected_uuid = str(candidate.get("server_uuid") or "").strip().lower()
            actual_uuid = str(architecture.get("server_uuid") or "").strip().lower()
            expected_id = str(candidate.get("server_id") or "").strip()
            actual_id = str(architecture.get("server_id") or "").strip()
            if expected_uuid:
                matches = actual_uuid == expected_uuid
            else:
                matches = bool(expected_id and expected_id == actual_id)
            if matches:
                return candidate
        return None

    def probe_database_peer(source: DBInstance, member: dict[str, Any]) -> dict[str, Any]:
        peer = DBInstance(
            instance_name=str(member.get("name") or member.get("host") or "自动发现数据库节点"),
            db_type=source.db_type,
            host=normalize_host_address(member.get("host")),
            port=int(member.get("port") or source.port),
            username=source.username,
            password_cipher=source.password_cipher,
            database_name=source.database_name,
            schema_name=source.schema_name,
            account_type=source.account_type,
            oracle_connect_type=source.oracle_connect_type,
            service_name=source.service_name,
            sid=source.sid,
            sslmode=source.sslmode,
            encrypt=source.encrypt,
            trust_server_certificate=source.trust_server_certificate,
            driver_name=source.driver_name,
            certificate_profile_id=source.certificate_profile_id,
            tls_server_name=source.tls_server_name,
            tls_verify=source.tls_verify,
            enabled=1,
        )
        if source.id in certificate_profiles:
            peer.certificate_profile = certificate_profiles[source.id]
        for key, value in oracle_peer_options(source, member).items():
            setattr(peer, key, value)
        return instance_service.test_connection(peer)

    if peer_targets:
        peer_workers = min(4, MAX_TOPOLOGY_PEER_PROBES)
        with ThreadPoolExecutor(max_workers=peer_workers, thread_name_prefix="asset-peer-probe") as executor:
            futures: dict[Any, tuple[tuple, DBInstance, dict[str, Any]]] = {}
            attempted_endpoints: set[tuple] = set()

            def submit_peer(
                endpoint: tuple,
                source: DBInstance,
                member: dict[str, Any],
            ) -> None:
                if (
                    endpoint in configured_endpoints
                    or endpoint in attempted_endpoints
                ):
                    return
                if len(attempted_endpoints) >= MAX_TOPOLOGY_PEER_PROBES:
                    member["direct_probe"] = False
                    member["db_probe_status"] = "not_attempted"
                    member["db_probe_reason"] = (
                        f"本轮成员直连探测已达到上限 {MAX_TOPOLOGY_PEER_PROBES}，未尝试该端点"
                    )
                    return
                attempted_endpoints.add(endpoint)
                future = executor.submit(probe_database_peer, source, member)
                futures[future] = (endpoint, source, member)

            for endpoint, (source, member) in peer_targets.items():
                submit_peer(endpoint, source, member)

            while futures:
                future = next(as_completed(tuple(futures)))
                endpoint, source, member = futures.pop(future)
                try:
                    detail = future.result() or {}
                    architecture = detail.get("architecture") or {}
                    if member.get("endpoint_candidate"):
                        resolved = match_candidate_member(source, architecture, endpoint[2])
                        if resolved is None:
                            warnings.append(f"{endpoint[1]}:{endpoint[2]} 连接可用，但身份与已发现复制成员不符，未绑定节点")
                            continue
                        member = resolved
                        member["host"] = endpoint[1]
                        member["port"] = endpoint[2]
                        member["endpoint_source"] = "replication_connection_identity"
                    verify_peer_identity(endpoint[0], member, architecture, {
                        **(profiles.get(source.id) or {}),
                        "connection_host": source.host, "connection_port": source.port,
                    })
                    if (
                        endpoint[0] == "mysql"
                        and member.get("server_uuid") in (None, "")
                        and member.get("server_id") in (None, "")
                    ):
                        member["direct_probe"] = False
                        member["db_probe_status"] = "unverified"
                        member["db_probe_reason"] = (
                            "种子未提供 server_uuid/server_id，端点认证成功但成员身份未确认"
                        )
                        member["db_probe_source_instance_id"] = source.id
                        warnings.append(
                            f"{member.get('name') or endpoint[1]} 端点认证成功，但种子未提供 MySQL 成员身份；未绑定为已验证节点"
                        )
                        continue
                    if id(member) in alias_originals:
                        alias_originals.pop(id(member))
                        member["endpoint_source"] = "seed_tns_alias_identity"
                    member["db_version"] = detail.get("version") or architecture.get("db_version") or member.get("db_version")
                    if architecture.get("health_status"):
                        member["health_status"] = architecture["health_status"]
                    peer_role = normalize_role(architecture.get("role_type"), endpoint[0])
                    if peer_role != "unknown":
                        member["role_type"] = peer_role
                    if architecture.get("replication_delay_seconds") is not None:
                        member["replication_delay_seconds"] = architecture["replication_delay_seconds"]
                    member["direct_probe"] = True
                    member["db_probe_status"] = "verified"
                    member.pop("db_probe_reason", None)
                    member["db_probe_source_instance_id"] = source.id

                    source_profile = profiles.get(source.id)
                    if isinstance(source_profile, dict) and isinstance(architecture, dict):
                        merged_members = merge_peer_architecture(
                            source,
                            source_profile,
                            member,
                            architecture,
                            endpoint,
                        )
                        for nested_member in merged_members:
                            nested_endpoint = member_endpoint(endpoint[0], nested_member, endpoint[2], source)
                            if nested_endpoint is not None:
                                submit_peer(nested_endpoint, source, nested_member)
                except Exception as exc:
                    if id(member) in alias_originals:
                        member.update(alias_originals.pop(id(member)))
                    error_text = re.sub(r"\s+", " ", str(exc)).strip()[:160]
                    member["direct_probe"] = False
                    member["db_probe_status"] = "failed"
                    member["replication"] = {"state": "unknown", "reason": "probe_failed"}
                    member["probe_error"] = error_text
                    warnings.append(f"{member.get('name') or endpoint[1]} 已识别为复制成员，但直连补采失败：{error_text}")
    for profile in profiles.values():
        for member in profile.get("members") or []:
            if isinstance(member, dict) and id(member) in alias_originals:
                member.update(alias_originals.pop(id(member)))
                member["direct_probe"] = False
                member["db_probe_status"] = "needs_endpoint"
                member["db_probe_reason"] = "别名候选尚未通过独立数据库身份验证，未绑定连接端点"
    if changed:
        db.commit()
    return profiles, warnings, len(instances)


def _probe_internal_hosts(
    db: Session,
    detected_profiles: dict[int, dict[str, Any]] | None = None,
) -> tuple[dict[Any, dict[str, Any]], list[str]]:
    instances = db.query(DBInstance).filter(DBInstance.enabled == 1).order_by(DBInstance.id.asc()).all()
    targets: dict[tuple[Any, ...], list[DBInstance]] = defaultdict(list)
    for instance in instances:
        if instance.ssh_host and instance.ssh_username:
            targets[(*_instance_scope(instance), instance.ssh_host, instance.ssh_port or 22, instance.ssh_username)].append(instance)
    peer_targets: dict[Any, tuple[str, DBInstance, list[dict[str, Any]]]] = {}
    configured_hosts = {_scoped_host_key(item, item.ssh_host) for item in instances
                        if item.ssh_host and item.ssh_username}
    for source in instances:
        if not (source.ssh_username and source.ssh_password_cipher):
            continue
        profile = (detected_profiles or {}).get(source.id) or {}
        if profile.get("discovery_status") != "success":
            continue
        for member in profile.get("members") or []:
            if not isinstance(member, dict) or member.get("direct_probe") is not True:
                continue
            host = normalize_host_address(member.get("host"))
            key = _scoped_host_key(source, host)
            if not host or key in configured_hosts:
                continue
            if key not in peer_targets:
                if len(peer_targets) >= MAX_TOPOLOGY_PEER_PROBES:
                    continue
                peer_targets[key] = (host, source, [])
            peer_targets[key][2].append(member)
    if not targets and not peer_targets:
        return {}, []

    profiles: dict[Any, dict[str, Any]] = {}
    warnings: list[str] = []
    max_workers = min(4, len(targets) + len(peer_targets))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="asset-host-discovery") as executor:
        futures = {
            executor.submit(host_overview.get_instance_host_overview, group[0], discovery_only=True): group
            for group in targets.values()
        }
        for future in as_completed(futures):
            group = futures[future]
            sample = group[0]
            try:
                detail = future.result() or {}
                basic = detail.get("host_basic") if isinstance(detail, dict) else None
                if not isinstance(basic, dict):
                    warning = detail.get("error") if isinstance(detail, dict) else "未返回主机信息"
                    warnings.append(f"{sample.instance_name} 主机信息采集失败：{warning}")
                    continue
                profile = {
                    "hostname": basic.get("hostname"),
                    "os_type": basic.get("os_name"),
                    "os_version": basic.get("os_version"),
                    "cpu_cores": basic.get("cpu_count"),
                    "cpu_model": basic.get("cpu_model"),
                    "memory_total_mb": basic.get("memory_total_mb"),
                }
                for instance in group:
                    profiles[_scoped_host_key(instance, instance.host)] = profile
            except Exception as exc:
                error_text = re.sub(r"\s+", " ", str(exc)).strip()[:180]
                warnings.append(f"{sample.instance_name} 主机信息采集失败：{error_text}")

    # Only database-verified discovered members receive one attempt with the
    # seed's explicitly configured SSH account. Keep this connection transient;
    # neither its password nor its secret reference enters the asset snapshot.
    if peer_targets:
        with ThreadPoolExecutor(max_workers=min(4, len(peer_targets)), thread_name_prefix="asset-peer-host") as executor:
            futures = {}
            for profile_key, (host, source, members) in peer_targets.items():
                peer = DBInstance(instance_name=host, db_type=source.db_type, host=host,
                                  port=source.port, username=source.username,
                                  password_cipher=source.password_cipher, ssh_host=host,
                                  ssh_port=source.ssh_port or 22, ssh_username=source.ssh_username,
                                  ssh_password_cipher=source.ssh_password_cipher)
                future = executor.submit(host_overview.get_instance_host_overview, peer,
                                         discovery_only=True, require_known_host=True)
                futures[future] = (profile_key, host, source, members)
            for future in as_completed(futures):
                profile_key, host, source, members = futures[future]
                try:
                    detail = future.result() or {}
                    basic = detail.get("host_basic")
                    if not isinstance(basic, dict) or not basic.get("hostname"):
                        raise ValueError("SSH 认证或主机信息采集未通过")
                    profiles[profile_key] = {
                        "hostname": basic.get("hostname"), "os_type": basic.get("os_name"),
                        "os_version": basic.get("os_version"), "cpu_cores": basic.get("cpu_count"),
                        "cpu_model": basic.get("cpu_model"), "memory_total_mb": basic.get("memory_total_mb"),
                    }
                    for member in members:
                        member["ssh_probe_status"] = "verified"
                        member["ssh_probe_source_instance_id"] = source.id
                except Exception:
                    for member in members:
                        member["ssh_probe_status"] = "failed"
                        member["ssh_probe_source_instance_id"] = source.id
                    warnings.append(f"{host} 已尝试复用所属集群 SSH 配置，验证未通过；数据库验证结果保留，请单独配置该主机 SSH")
    return profiles, warnings


DISCOVERY_HISTORY_FIELDS = (
    "db_version",
    "architecture_type",
    "role_type",
    "topology_group",
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
    "server_id",
    "server_uuid",
    "architecture_evidence",
)


def _preserve_successful_discovery_history(db: Session, source_id: int, bundle) -> None:
    existing = {
        row.external_id: row
        for row in db.query(CmdbAsset).filter(CmdbAsset.source_id == source_id).all()
    }
    verified_at = bundle.collected_at.isoformat()
    for item in bundle.assets:
        if item.get("asset_type") != "database_instance":
            continue
        attributes = item.get("attributes") or {}
        status = attributes.get("discovery_status")
        if status == "success":
            snapshot = {
                field: attributes.get(field)
                for field in DISCOVERY_HISTORY_FIELDS
                if attributes.get(field) not in (None, "", [], {})
            }
            attributes["last_successful_discovery"] = snapshot
            attributes["last_verified_at"] = verified_at
            attributes["has_verified_history"] = True
            attributes["architecture_source"] = "live_probe"
            continue
        if status != "failed":
            continue

        previous = existing.get(item["external_id"])
        previous_attributes = (previous.attributes or {}) if previous else {}
        snapshot = previous_attributes.get("last_successful_discovery")
        if not isinstance(snapshot, dict) and previous_attributes.get("architecture_verified"):
            snapshot = {
                field: previous_attributes.get(field)
                for field in DISCOVERY_HISTORY_FIELDS
                if previous_attributes.get(field) not in (None, "", [], {})
            }
        if not snapshot:
            attributes["has_verified_history"] = False
            continue
        for field in DISCOVERY_HISTORY_FIELDS:
            if snapshot.get(field) not in (None, "", [], {}):
                attributes[field] = snapshot[field]
        attributes["last_successful_discovery"] = snapshot
        attributes["last_verified_at"] = previous_attributes.get("last_verified_at") or (
            previous.last_seen_at.isoformat() if previous and previous.last_seen_at else None
        )
        attributes["has_verified_history"] = True
        attributes["architecture_source"] = "last_successful_discovery"


def apply_internal_discovery(db: Session, probe_live: bool = False) -> dict[str, Any]:
    source = _ensure_internal_source(db)
    detected_profiles: dict[int, dict[str, Any]] = {}
    probe_warnings: list[str] = []
    probed_count = 0
    host_profiles: dict[str, dict[str, Any]] = {}
    if probe_live:
        detected_profiles, probe_warnings, probed_count = _probe_internal_instances(db)
        host_profiles, host_warnings = _probe_internal_hosts(db, detected_profiles)
        probe_warnings.extend(host_warnings)
    bundle = build_internal_discovery(
        db,
        detected_profiles=detected_profiles,
        host_profiles=host_profiles,
        extra_warnings=probe_warnings,
    )
    if probe_live:
        _preserve_successful_discovery_history(db, source.id, bundle)
    bundle = prepare_discovery_relations(bundle, detected_profiles)
    preview = _discovery_diff(db, source.id, bundle)
    result = upsert_assets(db, source.id, bundle.assets, authoritative_snapshot=True)
    relation_count = _sync_internal_relations(db, source.id, bundle.relations)
    binding_count = _sync_internal_bindings(db, source.id, bundle.instance_bindings)
    normalized = sync_normalized_asset_model(
        db,
        source.id,
        summary={
            "sync_job_id": result.sync_job_id,
            "warnings": bundle.warnings,
            "probe_live": probe_live,
            "probed_count": probed_count,
            "host_profile_count": len(host_profiles),
        },
    )
    ha_snapshot_count = 0
    if probe_live:
        from app.cmdb import ha_diagnostics

        ha_snapshot_count = len(ha_diagnostics.capture_all_snapshots(db, source="live_discovery"))
    successful_probe_count = sum(
        1 for profile in detected_profiles.values()
        if profile.get("discovery_status") == "success"
    )
    failed_probe_count = sum(
        1 for profile in detected_profiles.values()
        if profile.get("discovery_status") == "failed"
    )
    return {
        **preview,
        "source_id": source.id,
        "source_name": source.source_name,
        "sync_job_id": result.sync_job_id,
        "applied_created_count": result.created_count,
        "applied_updated_count": result.updated_count,
        "pending_confirmation_count": result.pending_confirmation_count,
        "applied_relation_count": relation_count,
        "applied_binding_count": binding_count,
        "probe_live": probe_live,
        "probed_count": probed_count,
        "detected_architecture_count": successful_probe_count,
        "failed_probe_count": failed_probe_count,
        "warning_count": len(bundle.warnings),
        "warnings": bundle.warnings,
        "host_profile_count": len(host_profiles),
        "ha_snapshot_count": ha_snapshot_count,
        "normalized": normalized,
    }


def ensure_internal_assets(db: Session) -> dict[str, Any]:
    source = _ensure_internal_source(db)
    asset_count = db.query(CmdbAsset).filter(CmdbAsset.source_id == source.id).count()
    if asset_count:
        normalized = sync_normalized_asset_model(
            db,
            source.id,
            summary={"startup_projection": True},
            record_batch=False,
        )
        return {"source_id": source.id, "initialized": False, "asset_count": asset_count, "normalized": normalized}
    result = apply_internal_discovery(db)
    return {"source_id": source.id, "initialized": True, "asset_count": result["created_count"]}


def sync_normalized_asset_model(
    db: Session,
    source_id: int | None,
    summary: dict[str, Any] | None = None,
    record_batch: bool = True,
) -> dict[str, Any]:
    source = db.get(CmdbSource, source_id) if source_id is not None else None
    inventory = list_inventory(db)
    try:
        db.flush()
        # Legacy projector commits must only flush until the relation guard passes.
        with Session(bind=db.connection(), join_transaction_mode="rollback_only") as projection_db:
            saved = snapshot_normalized_relations(projection_db)
            result = normalized_service.sync_from_inventory(
                projection_db, inventory, source_id=source_id,
                source_name=source.source_name if source else "TraceMind 资产同步",
                source_type=source.source_type if source else "internal",
                summary=summary, record_batch=record_batch,
            )
            reconcile_normalized_relations(projection_db, inventory, saved)
            projection_db.commit()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _set_field_path(payload: dict[str, Any], field_path: str, value: Any) -> None:
    parts = field_path.split(".")
    target = payload
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[parts[-1]] = value


def _get_field_path(payload: dict[str, Any], field_path: str) -> Any:
    value: Any = payload
    for part in field_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _serialize_override(row: CmdbAssetOverride) -> dict[str, Any]:
    return {
        "id": row.id,
        "asset_id": row.asset_id,
        "field_path": row.field_path,
        "value": row.override_value,
        "locked": bool(row.locked),
        "active": bool(row.active),
        "reason": row.reason,
        "created_by": row.created_by,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def _effective_asset_payload(
    asset: CmdbAsset,
    source: CmdbSource | None = None,
    overrides: list[CmdbAssetOverride] | None = None,
    relation_count: int = 0,
) -> dict[str, Any]:
    payload = deepcopy(serialize_asset(asset))
    payload["attributes"] = deepcopy(payload.get("attributes") or {})
    applied_overrides = [row for row in (overrides or []) if row.active]
    for override in applied_overrides:
        _set_field_path(payload, override.field_path, deepcopy(override.override_value))
    payload["source"] = serialize_source(source) if source else None
    payload["source_name"] = source.source_name if source else "未知来源"
    payload["source_type"] = source.source_type if source else "unknown"
    payload["source_kind"] = "automatic" if source and source.source_type == INTERNAL_SOURCE_TYPE else "external"
    payload["relation_count"] = relation_count
    payload["override_count"] = len(applied_overrides)
    payload["locked_fields"] = [row.field_path for row in applied_overrides if row.locked]
    payload["native_instance_id"] = payload["attributes"].get("native_instance_id")
    payload["health_status"] = payload["attributes"].get("health_status") or (
        "unknown" if asset.status == "active" else asset.status
    )
    return payload


def list_inventory(
    db: Session,
    asset_type: str | None = None,
    db_type: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    source_id: int | None = None,
    keyword: str | None = None,
    allowed_native_instance_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    query = db.query(CmdbAsset)
    allowed_asset_ids = _asset_ids_for_native_scope(db, allowed_native_instance_ids)
    if allowed_asset_ids is not None:
        if not allowed_asset_ids:
            return []
        query = query.filter(CmdbAsset.id.in_(allowed_asset_ids))
    if asset_type:
        query = query.filter(CmdbAsset.asset_type == asset_type)
    if status:
        query = query.filter(CmdbAsset.status == status)
    if source_id is not None:
        query = query.filter(CmdbAsset.source_id == source_id)
    if keyword:
        query = query.filter(CmdbAsset.name.ilike(f"%{keyword}%"))
    assets = query.order_by(CmdbAsset.asset_type.asc(), CmdbAsset.name.asc()).all()
    if not assets:
        return []
    asset_ids = [row.id for row in assets]
    sources = {row.id: row for row in db.query(CmdbSource).all()}
    overrides_by_asset: dict[int, list[CmdbAssetOverride]] = defaultdict(list)
    for row in db.query(CmdbAssetOverride).filter(CmdbAssetOverride.asset_id.in_(asset_ids), CmdbAssetOverride.active == 1).all():
        overrides_by_asset[row.asset_id].append(row)
    relation_counts: Counter[int] = Counter()
    for row in db.query(CmdbRelation).filter(CmdbRelation.status == "active").all():
        relation_counts[row.source_asset_id] += 1
        relation_counts[row.target_asset_id] += 1
    items = [
        _effective_asset_payload(
            asset,
            source=sources.get(asset.source_id),
            overrides=overrides_by_asset.get(asset.id, []),
            relation_count=relation_counts[asset.id],
        )
        for asset in assets
    ]
    if db_type:
        items = [item for item in items if item["attributes"].get("db_type") == db_type]
    if environment:
        items = [item for item in items if item["attributes"].get("environment") == environment]
    return items


def get_asset_detail(
    db: Session,
    asset_id: int,
    allowed_native_instance_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    asset = db.get(CmdbAsset, asset_id)
    if not asset:
        return None
    item = next((row for row in list_inventory(db, allowed_native_instance_ids=allowed_native_instance_ids) if row["id"] == asset_id), None)
    if not item:
        return None
    allowed_asset_ids = _asset_ids_for_native_scope(db, allowed_native_instance_ids)
    related_rows = (
        db.query(CmdbRelation)
        .filter(
            CmdbRelation.status == "active",
            (CmdbRelation.source_asset_id == asset_id) | (CmdbRelation.target_asset_id == asset_id),
        )
        .order_by(CmdbRelation.id.desc())
        .all()
    )
    if allowed_asset_ids is not None:
        related_rows = [
            row
            for row in related_rows
            if row.source_asset_id in allowed_asset_ids and row.target_asset_id in allowed_asset_ids
        ]
    related_asset_ids = {
        relation.target_asset_id if relation.source_asset_id == asset_id else relation.source_asset_id
        for relation in related_rows
    }
    related_assets = {
        row.id: row
        for row in db.query(CmdbAsset).filter(CmdbAsset.id.in_(related_asset_ids)).all()
    } if related_asset_ids else {}
    item["relations"] = [
        {
            **serialize_relation(relation),
            "direction": "outgoing" if relation.source_asset_id == asset_id else "incoming",
            "related_asset": serialize_asset(related_assets[
                relation.target_asset_id if relation.source_asset_id == asset_id else relation.source_asset_id
            ]) if (relation.target_asset_id if relation.source_asset_id == asset_id else relation.source_asset_id) in related_assets else None,
        }
        for relation in related_rows
    ]
    item["overrides"] = [
        _serialize_override(row)
        for row in db.query(CmdbAssetOverride)
        .filter(CmdbAssetOverride.asset_id == asset_id, CmdbAssetOverride.active == 1)
        .order_by(CmdbAssetOverride.id.desc())
        .all()
    ]
    return item


def asset_overview(
    db: Session,
    allowed_native_instance_ids: set[int] | None = None,
    allowed_normalized_instance_ids: set[int] | None = None,
) -> dict[str, Any]:
    assets = list_inventory(db, allowed_native_instance_ids=allowed_native_instance_ids)
    sources = list_sources(db)
    if allowed_native_instance_ids is not None:
        visible_source_ids = {item["source_id"] for item in assets}
        sources = [row for row in sources if row["id"] in visible_source_ids]
    type_counts = Counter(item["asset_type"] for item in assets)
    health_counts = Counter(item["health_status"] for item in assets)
    status_counts = Counter(item["status"] for item in assets)
    source_counts = Counter(item["source_kind"] for item in assets)
    latest_seen = max((item["last_seen_at"] for item in assets if item.get("last_seen_at")), default=None)
    normalized = normalized_service.overview(db, allowed_instance_ids=allowed_normalized_instance_ids)
    return {
        "total_assets": len(assets),
        "database_instances": type_counts["database_instance"],
        "hosts": type_counts["host"],
        "clusters": type_counts["database_cluster"],
        "healthy": health_counts["healthy"],
        "abnormal": sum(count for key, count in health_counts.items() if key in {"critical", "warning", "unhealthy"}),
        "pending_confirmation": status_counts["pending_confirmation"],
        "manual_overrides": sum(item["override_count"] for item in assets),
        "automatic_assets": source_counts["automatic"],
        "external_assets": source_counts["external"],
        "source_count": len(sources),
        "latest_seen_at": latest_seen,
        "asset_type_counts": dict(type_counts),
        "health_counts": dict(health_counts),
        "sources": sources,
        **normalized,
    }


def topology_groups(db: Session, allowed_native_instance_ids: set[int] | None = None) -> dict[str, Any]:
    assets = [
        item
        for item in list_inventory(db, allowed_native_instance_ids=allowed_native_instance_ids)
        if item["asset_type"] in {"database_instance", "database_cluster"}
    ]
    database_assets = [item for item in assets if item["asset_type"] == "database_instance" and item["status"] == "active"]
    cluster_assets = {
        topology_scope(item): item
        for item in assets
        if item["asset_type"] == "database_cluster" and item["status"] == "active" and item["attributes"].get("topology_group")
    }
    members_by_group: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for asset in database_assets:
        members_by_group[topology_scope(asset)].append(asset)

    relations = db.query(CmdbRelation).filter(
        CmdbRelation.status == "active", CmdbRelation.relation_type == "replicates_to",
    ).order_by(CmdbRelation.id.asc()).all()
    key_counts = Counter(scope[-1] for scope in members_by_group)

    groups: list[dict[str, Any]] = []
    for scope, members in members_by_group.items():
        group_key = scope[-1]
        cluster = cluster_assets.get(scope)
        roles = Counter(member["attributes"].get("role_type") or "unknown" for member in members)
        primary_members = [member for member in members if member["attributes"].get("role_type") == "primary"]
        secondary_members = [member for member in members if member not in primary_members]
        ordered_members = primary_members + secondary_members
        architecture = next((member["attributes"].get("architecture_type") for member in members if member["attributes"].get("architecture_type")), "standalone")
        node_items = []
        for index, member in enumerate(ordered_members):
            attrs = member["attributes"]
            role = attrs.get("role_type") or "unknown"
            node_items.append({
                "id": str(member["id"]),
                "asset_id": member["id"],
                "name": member["name"],
                "role": role,
                "db_type": attrs.get("db_type") or "database",
                "host": attrs.get("host"),
                "port": attrs.get("port"),
                "version": attrs.get("db_version"),
                "vip": attrs.get("vip"),
                "health_status": member["health_status"],
                "environment": attrs.get("environment"),
                "system_name": attrs.get("system_name"),
                "delay_seconds": attrs.get("replication_delay_seconds"),
                "last_seen_at": member["last_seen_at"],
                "source_name": member["source_name"],
                "manually_corrected": bool(member["override_count"]),
                "position_hint": "primary" if role == "primary" or (not primary_members and index == 0) else "secondary",
            })
        edge_items = saved_topology_edges(members, relations)
        warnings = []
        if len(members) > 1 and roles["primary"] == 0 and architecture not in {"rac", "cluster"}:
            warnings.append("未识别到主节点，复制方向待人工确认")
        if len(members) > 1 and roles["primary"] > 1 and architecture not in {"rac", "mgr", "cluster"}:
            warnings.append("识别到多个主节点，请确认角色或集群模式")
        collected_at = max((member["last_seen_at"] for member in members if member.get("last_seen_at")), default=None)
        groups.append({
            "id": group_key if key_counts[group_key] == 1 else f"{group_key}:asset:{min(member['id'] for member in members)}",
            "name": (cluster or {}).get("name") or members[0]["attributes"].get("cluster_name") or members[0]["attributes"].get("system_name") or members[0]["name"],
            "db_type": members[0]["attributes"].get("db_type") or "database",
            "architecture_type": architecture,
            "environment": members[0]["attributes"].get("environment"),
            "health_status": (
                "healthy"
                if all(member["health_status"] == "healthy" for member in members)
                else "warning"
                if any(member["health_status"] not in {"healthy", "unknown"} for member in members)
                else "unknown"
            ),
            "collected_at": collected_at,
            "node_count": len(node_items),
            "nodes": node_items,
            "edges": edge_items,
            "replication_state": "verified" if edge_items else "unknown",
            "warnings": warnings,
        })
    groups.sort(key=lambda item: (item["environment"] or "", item["db_type"], item["name"]))
    return {
        "groups": groups,
        "group_count": len(groups),
        "node_count": sum(group["node_count"] for group in groups),
        "warning_count": sum(len(group["warnings"]) for group in groups),
    }


def upsert_asset_overrides(
    db: Session,
    asset_id: int,
    values: dict[str, Any],
    reason: str | None,
    locked: bool,
    operator: str,
) -> dict[str, Any]:
    asset = db.get(CmdbAsset, asset_id)
    if not asset:
        raise ValueError("Asset not found")
    unknown_fields = set(values) - ALLOWED_OVERRIDE_FIELDS
    if unknown_fields:
        raise ValueError(f"Unsupported override fields: {', '.join(sorted(unknown_fields))}")
    current = get_asset_detail(db, asset_id) or serialize_asset(asset)
    for field_path, value in values.items():
        row = (
            db.query(CmdbAssetOverride)
            .filter(CmdbAssetOverride.asset_id == asset_id, CmdbAssetOverride.field_path == field_path)
            .first()
        )
        old_value = _get_field_path(current, field_path)
        if not row:
            row = CmdbAssetOverride(asset_id=asset_id, field_path=field_path)
            db.add(row)
        row.override_value = mask_sensitive_payload(value, field_path)
        row.locked = 1 if locked else 0
        row.active = 1
        row.reason = reason
        row.created_by = operator
        db.add(CmdbChangeEvent(
            entity_type="asset",
            entity_id=asset_id,
            action="override",
            field_path=field_path,
            old_value=mask_sensitive_payload(old_value, field_path),
            new_value=mask_sensitive_payload(value, field_path),
            source_type="manual",
            operator=operator,
            reason=reason,
        ))
    db.commit()
    return get_asset_detail(db, asset_id) or {}


def remove_asset_override(db: Session, override_id: int, operator: str) -> bool:
    row = db.get(CmdbAssetOverride, override_id)
    if not row or not row.active:
        return False
    row.active = 0
    db.add(CmdbChangeEvent(
        entity_type="asset",
        entity_id=row.asset_id,
        action="override_removed",
        field_path=row.field_path,
        old_value=row.override_value,
        new_value=None,
        source_type="manual",
        operator=operator,
        reason="撤销人工修正",
    ))
    db.commit()
    return True


def list_change_events(db: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(CmdbChangeEvent).order_by(CmdbChangeEvent.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "action": row.action,
            "field_path": row.field_path,
            "old_value": row.old_value,
            "new_value": row.new_value,
            "source_type": row.source_type,
            "operator": row.operator,
            "reason": row.reason,
            "create_time": row.create_time,
        }
        for row in rows
    ]
