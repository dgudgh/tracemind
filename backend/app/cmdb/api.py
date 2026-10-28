from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cmdb import ha_diagnostics, normalized_service, service
from app.cmdb.models import AssetCluster, CmdbAssetOverride
from app.cmdb.schemas import (
    AssetCorrectionRequest,
    AssetDiscoveryRequest,
    AssetOverrideRequest,
    CmdbBindingCreate,
    CmdbRelationCreate,
    CmdbSourceCreate,
    CmdbSourceUpdate,
    CmdbSyncRequest,
)
from app.core.database import get_db

router = APIRouter(prefix="/cmdb", tags=["CMDB 资产与业务拓扑"])
asset_router = APIRouter(prefix="/assets", tags=["统一资产中心"])


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    return {"items": service.list_sources(db)}


@router.post("/sources")
def create_source(payload: CmdbSourceCreate, db: Session = Depends(get_db)):
    try:
        return service.create_source(db, payload.model_dump())
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="CMDB source name already exists")


@router.put("/sources/{source_id}")
def update_source(source_id: int, payload: CmdbSourceUpdate, db: Session = Depends(get_db)):
    try:
        source = service.update_source(db, source_id, payload.model_dump(exclude_unset=True))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="CMDB source name already exists")
    if not source:
        raise HTTPException(status_code=404, detail="CMDB source not found")
    return source


def _request_scope(request: Request, db: Session, minimum_access: str = "read"):
    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("role") == "admin" or not auth.get("user_id"):
        return None
    from app.services.access_control import resolve_scope
    return resolve_scope(db, int(auth["user_id"]), minimum_access=minimum_access)


def _allowed_native_instance_ids(request: Request, db: Session):
    scope = _request_scope(request, db)
    return set(scope.native_instance_ids) if scope and scope.restricted else None


def _require_global_scope(request: Request, db: Session, minimum_access: str = "read") -> None:
    scope = _request_scope(request, db, minimum_access=minimum_access)
    if scope and scope.restricted:
        raise HTTPException(status_code=403, detail="该操作需要全部资产范围")


@router.get("/assets")
def list_assets(
    request: Request,
    source_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    return {"items": service.list_assets(
        db,
        source_id=source_id,
        status=status,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
    )}


@router.get("/relations")
def list_relations(request: Request, source_id: int | None = None, db: Session = Depends(get_db)):
    return {"items": service.list_relations(
        db,
        source_id=source_id,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
    )}


@router.post("/relations")
def create_relation(payload: CmdbRelationCreate, db: Session = Depends(get_db)):
    try:
        return service.upsert_relation(db, payload.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/bindings")
def list_bindings(request: Request, instance_id: int | None = None, db: Session = Depends(get_db)):
    allowed = _allowed_native_instance_ids(request, db)
    if allowed is not None and instance_id is not None and instance_id not in allowed:
        raise HTTPException(status_code=403, detail="目标实例不在当前用户的数据范围内")
    rows = service.list_bindings(db, instance_id=instance_id)
    return {"items": [row for row in rows if allowed is None or row["instance_id"] in allowed]}


@router.post("/bindings")
def create_binding(payload: CmdbBindingCreate, db: Session = Depends(get_db)):
    try:
        return service.upsert_binding(db, payload.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sync")
def list_sync_jobs(source_id: int | None = None, db: Session = Depends(get_db)):
    return {"items": service.list_sync_jobs(db, source_id=source_id)}


@router.post("/sync")
def sync_assets(payload: CmdbSyncRequest, db: Session = Depends(get_db)):
    try:
        result = service.upsert_assets(
            db,
            payload.source_id,
            payload.items,
            authoritative_snapshot=payload.authoritative_snapshot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    normalized = service.sync_normalized_asset_model(
        db,
        payload.source_id,
        summary={"sync_job_id": result.sync_job_id, "external_sync": True},
    )
    return {
        "created_count": result.created_count,
        "updated_count": result.updated_count,
        "pending_confirmation_count": result.pending_confirmation_count,
        "sync_job_id": result.sync_job_id,
        "normalized": normalized,
    }


@router.get("/topology")
def get_topology(request: Request, source_id: int | None = Query(None), db: Session = Depends(get_db)):
    return service.topology(
        db,
        source_id=source_id,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
    )


@router.get("/instance-context/{instance_id}")
def get_instance_context(instance_id: int, request: Request, db: Session = Depends(get_db)):
    allowed = _allowed_native_instance_ids(request, db)
    if allowed is not None and instance_id not in allowed:
        raise HTTPException(status_code=403, detail="目标实例不在当前用户的数据范围内")
    return service.resolve_instance_context(db, instance_id)


def _request_operator(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("username") or auth.get("sub") or auth.get("user_id") or "admin")


def _allowed_normalized_instance_ids(request: Request, db: Session):
    scope = _request_scope(request, db)
    return set(scope.normalized_instance_ids) if scope and scope.restricted else None


@asset_router.get("/overview")
def get_asset_overview(request: Request, db: Session = Depends(get_db)):
    return service.asset_overview(
        db,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
        allowed_normalized_instance_ids=_allowed_normalized_instance_ids(request, db),
    )


@asset_router.get("/inventory")
def get_asset_inventory(
    request: Request,
    asset_type: str | None = None,
    db_type: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    source_id: int | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
):
    return {"items": service.list_inventory(
        db,
        asset_type=asset_type,
        db_type=db_type,
        environment=environment,
        status=status,
        source_id=source_id,
        keyword=keyword,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
    )}


@asset_router.get("/topology")
def get_asset_topology(request: Request, db: Session = Depends(get_db)):
    return service.topology_groups(db, allowed_native_instance_ids=_allowed_native_instance_ids(request, db))


@asset_router.get("/audit")
def get_asset_audit(request: Request, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    _require_global_scope(request, db)
    return {"items": service.list_change_events(db, limit=limit)}


@asset_router.post("/discovery/preview")
def preview_asset_discovery(request: Request, db: Session = Depends(get_db)):
    _require_global_scope(request, db)
    return service.preview_internal_discovery(db)


@asset_router.post("/discovery/apply")
def apply_asset_discovery(request: Request, payload: AssetDiscoveryRequest | None = None, db: Session = Depends(get_db)):
    _require_global_scope(request, db, minimum_access="operate")
    return service.apply_internal_discovery(db, probe_live=bool(payload and payload.probe_live))


@asset_router.get("/tree")
def get_normalized_asset_tree(request: Request, db: Session = Depends(get_db)):
    return normalized_service.asset_tree(db, allowed_instance_ids=_allowed_normalized_instance_ids(request, db))


@asset_router.get("/clusters")
def list_normalized_clusters(request: Request, db: Session = Depends(get_db)):
    return {"items": normalized_service.list_clusters(db, allowed_instance_ids=_allowed_normalized_instance_ids(request, db))}


@asset_router.get("/clusters/{cluster_id}")
def get_normalized_cluster(cluster_id: int, db: Session = Depends(get_db)):
    item = normalized_service.get_cluster(db, cluster_id)
    if not item:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return item


@asset_router.get("/clusters/{cluster_id}/ha-diagnostics")
def get_cluster_ha_diagnostics(cluster_id: int, db: Session = Depends(get_db)):
    item = ha_diagnostics.assess_cluster(db, cluster_id)
    if not item:
        raise HTTPException(status_code=404, detail="Cluster not found")
    history = ha_diagnostics.list_snapshots(db, cluster_id, limit=1)
    item["latest_snapshot"] = history[0] if history else None
    return item


@asset_router.post("/clusters/{cluster_id}/ha-diagnostics/refresh")
def refresh_cluster_ha_diagnostics(cluster_id: int, db: Session = Depends(get_db)):
    item = ha_diagnostics.capture_snapshot(db, cluster_id, source="manual_refresh")
    if not item:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return item


@asset_router.get("/clusters/{cluster_id}/ha-diagnostics/history")
def get_cluster_ha_diagnostic_history(
    cluster_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    if not db.get(AssetCluster, cluster_id):
        raise HTTPException(status_code=404, detail="Cluster not found")
    return {"items": ha_diagnostics.list_snapshots(db, cluster_id, limit=limit)}


@asset_router.get("/instances")
def list_normalized_instances(request: Request, db: Session = Depends(get_db)):
    return {"items": normalized_service.list_instances(db, allowed_instance_ids=_allowed_normalized_instance_ids(request, db))}


@asset_router.get("/instances/{instance_id}")
def get_normalized_instance(instance_id: int, db: Session = Depends(get_db)):
    item = normalized_service.get_instance(db, instance_id)
    if not item:
        raise HTTPException(status_code=404, detail="Instance not found")
    return item


@asset_router.get("/hosts")
def list_normalized_hosts(request: Request, db: Session = Depends(get_db)):
    return {"items": normalized_service.list_hosts(db, allowed_instance_ids=_allowed_normalized_instance_ids(request, db))}


@asset_router.get("/hosts/{host_id}")
def get_normalized_host(host_id: int, db: Session = Depends(get_db)):
    item = normalized_service.get_host(db, host_id)
    if not item:
        raise HTTPException(status_code=404, detail="Host not found")
    return item


@asset_router.get("/discovery-batches")
def list_normalized_discovery_batches(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _require_global_scope(request, db)
    return {"items": normalized_service.list_discovery_batches(db, limit=limit)}


@asset_router.get("/corrections")
def list_normalized_corrections(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    _require_global_scope(request, db)
    return {"items": normalized_service.list_corrections(db, limit=limit)}


@asset_router.post("/corrections")
def create_normalized_correction(
    payload: AssetCorrectionRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("role") != "admin" and auth.get("user_id"):
        from app.services.access_control import scope_allows_entity
        if not scope_allows_entity(
            db,
            int(auth["user_id"]),
            payload.entity_type,
            payload.entity_id,
            minimum_access="operate",
        ):
            raise HTTPException(status_code=403, detail="目标资产不在当前用户的可操作范围内")
    try:
        return normalized_service.apply_correction(db, payload.model_dump(), _request_operator(request))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@asset_router.get("/{asset_id}")
def get_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    asset = service.get_asset_detail(
        db,
        asset_id,
        allowed_native_instance_ids=_allowed_native_instance_ids(request, db),
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@asset_router.post("/{asset_id}/overrides")
def save_asset_overrides(
    asset_id: int,
    payload: AssetOverrideRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    allowed = _allowed_native_instance_ids(request, db)
    if service.get_asset_detail(db, asset_id, allowed_native_instance_ids=allowed) is None:
        raise HTTPException(status_code=403, detail="目标资产不在当前用户的数据范围内")
    try:
        return service.upsert_asset_overrides(
            db,
            asset_id,
            payload.values,
            payload.reason,
            payload.locked,
            _request_operator(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@asset_router.delete("/overrides/{override_id}", status_code=204)
def delete_asset_override(override_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(CmdbAssetOverride, override_id)
    if not row:
        raise HTTPException(status_code=404, detail="Asset override not found")
    allowed = _allowed_native_instance_ids(request, db)
    if service.get_asset_detail(db, row.asset_id, allowed_native_instance_ids=allowed) is None:
        raise HTTPException(status_code=403, detail="目标资产不在当前用户的数据范围内")
    if not service.remove_asset_override(db, override_id, _request_operator(request)):
        raise HTTPException(status_code=404, detail="Asset override not found")
