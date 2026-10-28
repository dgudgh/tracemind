from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.common import ConnectionTestResponse, PageResponse
from app.schemas.db_instance import (
    DBInstanceConnectionTest,
    DBInstanceCreate,
    DBInstanceCredentialAuditResponse,
    DBInstanceCredentialRotate,
    DBInstanceResponse,
    DBInstanceUpdate,
)
from app.services import instances as instance_service
from app.services import host_overview

router = APIRouter(prefix="/instances", tags=["数据库实例管理"])


def _actor(request: Request) -> str:
    auth = getattr(request.state, "auth", {}) or {}
    return str(auth.get("username") or auth.get("sub") or auth.get("user_id") or "system")


@router.get("", response_model=PageResponse)
def list_instances(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=500),
    db_type: str | None = Query(None, pattern="^(oracle|mysql|postgresql|sqlserver)$"),
    keyword: str | None = Query(None),
    db: Session = Depends(get_db),
):
    auth = getattr(request.state, "auth", {}) or {}
    allowed_ids = None
    if auth.get("role") != "admin" and auth.get("user_id"):
        from app.services.access_control import resolve_scope
        scope = resolve_scope(db, int(auth["user_id"]))
        allowed_ids = set(scope.native_instance_ids) if scope.restricted else None
    items, total = instance_service.get_list(db, page, page_size, db_type, keyword, allowed_ids=allowed_ids)
    return PageResponse(items=[DBInstanceResponse.model_validate(item) for item in items], total=total, page=page, page_size=page_size)


@router.get("/{instance_id}", response_model=DBInstanceResponse)
def get_instance(instance_id: int, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    return obj


@router.post("", response_model=DBInstanceResponse, status_code=201)
def create_instance(data: DBInstanceCreate, request: Request, db: Session = Depends(get_db)):
    try:
        return instance_service.create(db, data, actor=_actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{instance_id}", response_model=DBInstanceResponse)
def update_instance(instance_id: int, data: DBInstanceUpdate, request: Request, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    try:
        return instance_service.update(db, obj, data, actor=_actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{instance_id}", status_code=204)
def delete_instance(instance_id: int, request: Request, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    instance_service.delete(db, obj, operator=_actor(request))


@router.post("/connection-test", response_model=ConnectionTestResponse)
def test_instance_connection_configuration(data: DBInstanceConnectionTest, db: Session = Depends(get_db)):
    try:
        detail = instance_service.test_connection_configuration(db, data)
        return ConnectionTestResponse(ok=True, message="连接成功", detail=detail)
    except Exception as exc:
        return ConnectionTestResponse(ok=False, message="连接失败", detail={"error": str(exc)})


@router.post("/{instance_id}/test", response_model=ConnectionTestResponse)
def test_instance_connection(instance_id: int, request: Request, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    try:
        detail = instance_service.test_connection_and_record(db, obj, actor=_actor(request))
        return ConnectionTestResponse(ok=True, message="连接成功", detail=detail)
    except Exception as exc:
        return ConnectionTestResponse(ok=False, message="连接失败", detail={"error": str(exc)})


@router.post("/{instance_id}/credentials/rotate", response_model=DBInstanceResponse)
def rotate_instance_credential(
    instance_id: int,
    data: DBInstanceCredentialRotate,
    request: Request,
    db: Session = Depends(get_db),
):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    try:
        return instance_service.rotate_credential(db, obj, data, actor=_actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/{instance_id}/credential-audits",
    response_model=list[DBInstanceCredentialAuditResponse],
)
def list_instance_credential_audits(
    instance_id: int,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    if not instance_service.get_by_id(db, instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance_service.credential_audits(db, instance_id, limit=limit)


@router.get("/{instance_id}/host/overview")
def get_host_overview(instance_id: int, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    return host_overview.get_instance_host_overview(obj)


@router.post("/{instance_id}/host/refresh")
def refresh_host_overview(instance_id: int, db: Session = Depends(get_db)):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    return host_overview.get_instance_host_overview(obj)
