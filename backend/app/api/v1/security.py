from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.cmdb.models import AssetCluster, AssetHost, AssetInstance
from app.core.config import settings
from app.core.database import get_db
from app.models.access_control import (
    AccessRole,
    Organization,
    RoleActionPermission,
    SecurityAuditEvent,
    Tenant,
    UserAccessRole,
    UserAssetScope,
    UserOrganization,
)
from app.models.user import User
from app.models.db_instance import DBInstance
from app.services.access_control import (
    ACCESS_LEVELS,
    SCOPE_TYPES,
    action_catalog,
    actions_beyond_actor,
    append_security_audit,
    ensure_builtin_roles,
    is_superuser_identity,
    role_action_keys,
    scope_options,
    verify_audit_chain,
)

router = APIRouter(prefix="/security", tags=["平台安全与数据权限"])
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


class RoleWrite(BaseModel):
    role_key: str = Field(..., min_length=2, max_length=80)
    name: str = Field(..., min_length=2, max_length=120)
    description: str | None = Field(None, max_length=1000)
    action_keys: list[str] = Field(default_factory=list, max_length=100)


class RoleAssignments(BaseModel):
    role_ids: list[int] = Field(default_factory=list, max_length=50)


class ScopeItem(BaseModel):
    scope_type: str
    scope_id: int | None = None
    access_level: str = "read"


class ScopeAssignments(BaseModel):
    scopes: list[ScopeItem] = Field(default_factory=list, max_length=200)


class TenantWrite(BaseModel):
    code: str = Field(..., min_length=2, max_length=80)
    name: str = Field(..., min_length=2, max_length=160)
    status: str = Field("active", pattern="^(active|disabled)$")
    remark: str | None = Field(None, max_length=1000)


class OrganizationWrite(BaseModel):
    tenant_id: int = Field(..., ge=1)
    parent_id: int | None = Field(None, ge=1)
    code: str = Field(..., min_length=2, max_length=80)
    name: str = Field(..., min_length=2, max_length=160)
    organization_type: str = Field("department", pattern="^(group|company|factory|department|team|custom)$")
    status: str = Field("active", pattern="^(active|disabled)$")
    remark: str | None = Field(None, max_length=1000)


class UserOrganizationAssignments(BaseModel):
    organization_ids: list[int] = Field(default_factory=list, max_length=100)
    primary_organization_id: int | None = Field(None, ge=1)


def _actor(request: Request) -> tuple[int | None, str]:
    info = getattr(request.state, "auth", {}) or {}
    return info.get("user_id"), str(info.get("username") or "system")


def _audit(db: Session, request: Request, action: str, target_type: str, target_id, details=None):
    actor_user_id, actor = _actor(request)
    append_security_audit(
        db,
        category="authorization",
        action=action,
        actor=actor,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        details=details,
        retention_days=settings.SECURITY_AUDIT_RETENTION_DAYS,
    )


def _role_payload(db: Session, row: AccessRole) -> dict:
    actions = [
        item.action_key
        for item in db.query(RoleActionPermission).filter(RoleActionPermission.role_id == row.id).order_by(RoleActionPermission.action_key).all()
    ]
    return {
        "id": row.id,
        "role_key": row.role_key,
        "name": row.name,
        "description": row.description,
        "builtin": bool(row.builtin),
        "status": row.status,
        "action_keys": actions,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


@router.get("/actions")
def list_actions():
    return {"items": action_catalog()}


@router.get("/roles")
def list_roles(db: Session = Depends(get_db)):
    ensure_builtin_roles(db)
    rows = db.query(AccessRole).order_by(AccessRole.builtin.desc(), AccessRole.name).all()
    return {"items": [_role_payload(db, row) for row in rows]}


def _validate_actions(action_keys: list[str], *, builtin: bool = False) -> list[str]:
    known = {item["action_key"] for item in action_catalog()}
    normalized = sorted(set(action_keys))
    invalid = [
        key
        for key in normalized
        if key != "*"
        and key not in known
        and not (key.endswith(".*") and any(item.startswith(key[:-1]) for item in known))
    ]
    if "*" in normalized and not builtin:
        invalid.append("*")
    if invalid:
        raise HTTPException(status_code=422, detail=f"未知或不允许的动作权限：{', '.join(sorted(set(invalid)))}")
    return normalized


@router.post("/roles")
def create_role(payload: RoleWrite, request: Request, db: Session = Depends(get_db)):
    if not _KEY_PATTERN.fullmatch(payload.role_key):
        raise HTTPException(status_code=422, detail="角色标识格式不正确")
    if db.query(AccessRole).filter(AccessRole.role_key == payload.role_key).first():
        raise HTTPException(status_code=409, detail="角色标识已存在")
    actions = _validate_actions(payload.action_keys)
    row = AccessRole(role_key=payload.role_key, name=payload.name, description=payload.description, status="active")
    db.add(row)
    db.flush()
    db.add_all([RoleActionPermission(role_id=row.id, action_key=key) for key in actions])
    _audit(db, request, "role_created", "role", row.id, {"role_key": row.role_key, "action_keys": actions})
    db.commit()
    return _role_payload(db, row)


@router.put("/roles/{role_id}")
def update_role(role_id: int, payload: RoleWrite, request: Request, db: Session = Depends(get_db)):
    row = db.get(AccessRole, role_id)
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    actions = _validate_actions(payload.action_keys, builtin=bool(row.builtin))
    if row.builtin and payload.role_key != row.role_key:
        raise HTTPException(status_code=422, detail="内置角色标识不可修改")
    duplicate = db.query(AccessRole).filter(AccessRole.role_key == payload.role_key, AccessRole.id != role_id).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="角色标识已存在")
    before = _role_payload(db, row)
    row.role_key = payload.role_key
    row.name = payload.name
    row.description = payload.description
    db.query(RoleActionPermission).filter(RoleActionPermission.role_id == role_id).delete(synchronize_session=False)
    db.add_all([RoleActionPermission(role_id=role_id, action_key=key) for key in actions])
    _audit(db, request, "role_updated", "role", role_id, {"before": before, "action_keys": actions})
    db.commit()
    return _role_payload(db, row)


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(AccessRole, role_id)
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    if row.builtin:
        raise HTTPException(status_code=422, detail="内置角色不可删除")
    db.query(UserAccessRole).filter(UserAccessRole.role_id == role_id).delete(synchronize_session=False)
    db.query(RoleActionPermission).filter(RoleActionPermission.role_id == role_id).delete(synchronize_session=False)
    db.delete(row)
    _audit(db, request, "role_deleted", "role", role_id, {"role_key": row.role_key})
    db.commit()
    return {"detail": "角色已删除"}


@router.get("/users/{user_id}/roles")
def get_user_roles(user_id: int, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    role_ids = [row.role_id for row in db.query(UserAccessRole).filter(UserAccessRole.user_id == user_id).all()]
    return {"user_id": user_id, "role_ids": role_ids}


@router.put("/users/{user_id}/roles")
def set_user_roles(user_id: int, payload: RoleAssignments, request: Request, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    role_ids = sorted(set(payload.role_ids))
    found = {row.id for row in db.query(AccessRole).filter(AccessRole.id.in_(role_ids), AccessRole.status == "active").all()} if role_ids else set()
    if found != set(role_ids):
        raise HTTPException(status_code=422, detail="包含不存在或已停用的角色")
    # A delegated security administrator must not hand out permissions above its own.
    actor_user_id, actor = _actor(request)
    beyond = actions_beyond_actor(
        db,
        int(actor_user_id or 0),
        role_action_keys(db, role_ids),
        actor_is_superuser=is_superuser_identity(getattr(request.state, "auth", None)),
    )
    if beyond:
        raise HTTPException(
            status_code=403,
            detail=f"不能分配超出当前账号自身权限的角色权限：{', '.join(beyond)}",
        )
    db.query(UserAccessRole).filter(UserAccessRole.user_id == user_id).delete(synchronize_session=False)
    db.add_all([UserAccessRole(user_id=user_id, role_id=role_id) for role_id in role_ids])
    _audit(db, request, "user_roles_updated", "user", user_id, {"role_ids": role_ids})
    db.commit()
    from app.core.auth import invalidate_user_tokens
    invalidate_user_tokens(user_id, db=db, reason="roles_changed")
    return {"user_id": user_id, "role_ids": role_ids}


@router.get("/scope-options")
def get_scope_options(db: Session = Depends(get_db)):
    result = scope_options(db)
    result["tenants"] = [{"id": row.id, "name": row.name, "detail": row.code} for row in db.query(Tenant).filter(Tenant.status == "active").all()]
    result["organizations"] = [{"id": row.id, "name": row.name, "detail": row.code} for row in db.query(Organization).filter(Organization.status == "active").all()]
    return result


@router.get("/users/{user_id}/scopes")
def get_user_scopes(user_id: int, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    rows = db.query(UserAssetScope).filter(UserAssetScope.user_id == user_id).order_by(UserAssetScope.id).all()
    return {"user_id": user_id, "scopes": [
        {"id": row.id, "scope_type": row.scope_type, "scope_id": row.scope_id, "access_level": row.access_level}
        for row in rows
    ]}


@router.put("/users/{user_id}/scopes")
def set_user_scopes(user_id: int, payload: ScopeAssignments, request: Request, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    scopes = []
    seen = set()
    for item in payload.scopes:
        if item.scope_type not in SCOPE_TYPES:
            raise HTTPException(status_code=422, detail=f"不支持的数据范围：{item.scope_type}")
        if item.access_level not in ACCESS_LEVELS:
            raise HTTPException(status_code=422, detail=f"不支持的访问级别：{item.access_level}")
        if item.scope_type == "all" and item.scope_id is not None:
            raise HTTPException(status_code=422, detail="全量范围不能指定资源 ID")
        if item.scope_type != "all" and item.scope_id is None:
            raise HTTPException(status_code=422, detail=f"{item.scope_type} 范围必须指定资源 ID")
        key = (item.scope_type, item.scope_id)
        if key not in seen:
            seen.add(key)
            scopes.append(item)
    if any(item.scope_type == "all" for item in scopes) and len(scopes) > 1:
        raise HTTPException(status_code=422, detail="全量范围不能与其他范围同时配置")
    db.query(UserAssetScope).filter(UserAssetScope.user_id == user_id).delete(synchronize_session=False)
    db.add_all([UserAssetScope(user_id=user_id, **item.model_dump()) for item in scopes])
    _audit(db, request, "user_scopes_updated", "user", user_id, {"scopes": [item.model_dump() for item in scopes]})
    db.commit()
    return {"user_id": user_id, "scopes": [item.model_dump() for item in scopes]}


@router.get("/tenants")
def list_tenants(db: Session = Depends(get_db)):
    return {"items": [_tenant_payload(row) for row in db.query(Tenant).order_by(Tenant.name).all()]}


def _tenant_payload(row: Tenant) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "status": row.status, "remark": row.remark}


@router.post("/tenants")
def create_tenant(payload: TenantWrite, request: Request, db: Session = Depends(get_db)):
    if not _KEY_PATTERN.fullmatch(payload.code):
        raise HTTPException(status_code=422, detail="租户编码格式不正确")
    if db.query(Tenant).filter(Tenant.code == payload.code).first():
        raise HTTPException(status_code=409, detail="租户编码已存在")
    row = Tenant(**payload.model_dump())
    db.add(row)
    db.flush()
    _audit(db, request, "tenant_created", "tenant", row.id, {"code": row.code, "name": row.name})
    db.commit()
    return _tenant_payload(row)


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: int, payload: TenantWrite, request: Request, db: Session = Depends(get_db)):
    row = db.get(Tenant, tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="租户不存在")
    if not _KEY_PATTERN.fullmatch(payload.code):
        raise HTTPException(status_code=422, detail="租户编码格式不正确")
    duplicate = db.query(Tenant).filter(Tenant.code == payload.code, Tenant.id != tenant_id).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="租户编码已存在")
    before = _tenant_payload(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    _audit(db, request, "tenant_updated", "tenant", row.id, {"before": before, "after": _tenant_payload(row)})
    db.commit()
    return _tenant_payload(row)


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(Tenant, tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="租户不存在")
    referenced = (
        db.query(Organization).filter(Organization.tenant_id == tenant_id).first()
        or db.query(UserOrganization).filter(UserOrganization.tenant_id == tenant_id).first()
        or db.query(DBInstance).filter(DBInstance.tenant_id == tenant_id).first()
        or db.query(AssetHost).filter(AssetHost.tenant_id == tenant_id).first()
        or db.query(AssetCluster).filter(AssetCluster.tenant_id == tenant_id).first()
        or db.query(AssetInstance).filter(AssetInstance.tenant_id == tenant_id).first()
    )
    if referenced:
        raise HTTPException(status_code=409, detail="租户仍被组织、用户或资产引用，请先解除关联")
    before = _tenant_payload(row)
    db.delete(row)
    _audit(db, request, "tenant_deleted", "tenant", tenant_id, before)
    db.commit()
    return {"detail": "租户已删除"}


@router.get("/organizations")
def list_organizations(tenant_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(Organization)
    if tenant_id is not None:
        query = query.filter(Organization.tenant_id == tenant_id)
    return {"items": [_organization_payload(row) for row in query.order_by(Organization.tenant_id, Organization.parent_id, Organization.name).all()]}


def _organization_payload(row: Organization) -> dict:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "parent_id": row.parent_id,
        "code": row.code,
        "name": row.name,
        "organization_type": row.organization_type,
        "status": row.status,
        "remark": row.remark,
    }


@router.post("/organizations")
def create_organization(payload: OrganizationWrite, request: Request, db: Session = Depends(get_db)):
    if not _KEY_PATTERN.fullmatch(payload.code):
        raise HTTPException(status_code=422, detail="组织编码格式不正确")
    if not db.get(Tenant, payload.tenant_id):
        raise HTTPException(status_code=422, detail="租户不存在")
    if payload.parent_id:
        parent = db.get(Organization, payload.parent_id)
        if not parent or parent.tenant_id != payload.tenant_id:
            raise HTTPException(status_code=422, detail="上级组织不存在或不属于同一租户")
    duplicate = db.query(Organization).filter(Organization.tenant_id == payload.tenant_id, Organization.code == payload.code).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="同一租户下组织编码已存在")
    row = Organization(**payload.model_dump())
    db.add(row)
    db.flush()
    _audit(db, request, "organization_created", "organization", row.id, {"code": row.code, "name": row.name})
    db.commit()
    return _organization_payload(row)


def _validate_organization_parent(db: Session, row_id: int | None, tenant_id: int, parent_id: int | None) -> None:
    if not parent_id:
        return
    parent = db.get(Organization, parent_id)
    if not parent or parent.tenant_id != tenant_id:
        raise HTTPException(status_code=422, detail="上级组织不存在或不属于同一租户")
    seen = {row_id} if row_id else set()
    cursor = parent
    while cursor:
        if cursor.id in seen:
            raise HTTPException(status_code=422, detail="组织层级不能形成循环")
        seen.add(cursor.id)
        cursor = db.get(Organization, cursor.parent_id) if cursor.parent_id else None


@router.put("/organizations/{organization_id}")
def update_organization(
    organization_id: int,
    payload: OrganizationWrite,
    request: Request,
    db: Session = Depends(get_db),
):
    row = db.get(Organization, organization_id)
    if not row:
        raise HTTPException(status_code=404, detail="组织不存在")
    if not _KEY_PATTERN.fullmatch(payload.code):
        raise HTTPException(status_code=422, detail="组织编码格式不正确")
    if not db.get(Tenant, payload.tenant_id):
        raise HTTPException(status_code=422, detail="租户不存在")
    _validate_organization_parent(db, row.id, payload.tenant_id, payload.parent_id)
    duplicate = db.query(Organization).filter(
        Organization.tenant_id == payload.tenant_id,
        Organization.code == payload.code,
        Organization.id != organization_id,
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="同一租户下组织编码已存在")
    if payload.tenant_id != row.tenant_id and (
        db.query(Organization).filter(Organization.parent_id == row.id).first()
        or db.query(UserOrganization).filter(UserOrganization.organization_id == row.id).first()
    ):
        raise HTTPException(status_code=409, detail="组织存在下级或用户关联，不能直接更换租户")
    before = _organization_payload(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    _audit(db, request, "organization_updated", "organization", row.id, {"before": before, "after": _organization_payload(row)})
    db.commit()
    return _organization_payload(row)


@router.delete("/organizations/{organization_id}")
def delete_organization(organization_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(Organization, organization_id)
    if not row:
        raise HTTPException(status_code=404, detail="组织不存在")
    referenced = (
        db.query(Organization).filter(Organization.parent_id == organization_id).first()
        or db.query(UserOrganization).filter(UserOrganization.organization_id == organization_id).first()
        or db.query(DBInstance).filter(DBInstance.organization_id == organization_id).first()
        or db.query(AssetHost).filter(AssetHost.organization_id == organization_id).first()
        or db.query(AssetCluster).filter(AssetCluster.organization_id == organization_id).first()
        or db.query(AssetInstance).filter(AssetInstance.organization_id == organization_id).first()
    )
    if referenced:
        raise HTTPException(status_code=409, detail="组织仍被下级组织、用户或资产引用，请先解除关联")
    before = _organization_payload(row)
    db.delete(row)
    _audit(db, request, "organization_deleted", "organization", organization_id, before)
    db.commit()
    return {"detail": "组织已删除"}


@router.put("/users/{user_id}/organizations")
def set_user_organizations(user_id: int, payload: UserOrganizationAssignments, request: Request, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    ids = sorted(set(payload.organization_ids))
    organizations = db.query(Organization).filter(Organization.id.in_(ids)).all() if ids else []
    if {row.id for row in organizations} != set(ids):
        raise HTTPException(status_code=422, detail="包含不存在的组织")
    if payload.primary_organization_id and payload.primary_organization_id not in ids:
        raise HTTPException(status_code=422, detail="主组织必须包含在组织列表中")
    db.query(UserOrganization).filter(UserOrganization.user_id == user_id).delete(synchronize_session=False)
    db.add_all([
        UserOrganization(
            user_id=user_id,
            tenant_id=row.tenant_id,
            organization_id=row.id,
            is_primary=int(row.id == payload.primary_organization_id),
        )
        for row in organizations
    ])
    _audit(db, request, "user_organizations_updated", "user", user_id, payload.model_dump())
    db.commit()
    from app.core.auth import invalidate_user_tokens
    invalidate_user_tokens(user_id, db=db, reason="organizations_changed")
    return payload.model_dump()


@router.get("/users/{user_id}/organizations")
def get_user_organizations(user_id: int, db: Session = Depends(get_db)):
    rows = db.query(UserOrganization).filter(UserOrganization.user_id == user_id).all()
    return {
        "user_id": user_id,
        "organization_ids": [row.organization_id for row in rows],
        "primary_organization_id": next((row.organization_id for row in rows if row.is_primary), None),
    }


@router.get("/audit")
def list_security_audit(
    category: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(SecurityAuditEvent)
    if category:
        query = query.filter(SecurityAuditEvent.category == category)
    rows = query.order_by(SecurityAuditEvent.id.desc()).limit(limit).all()
    return {"items": [
        {
            "id": row.id,
            "category": row.category,
            "action": row.action,
            "outcome": row.outcome,
            "actor": row.actor,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "details": row.details,
            "previous_hash": row.previous_hash,
            "event_hash": row.event_hash,
            "retention_until": row.retention_until,
            "create_time": row.create_time,
        }
        for row in rows
    ]}


@router.get("/audit/verify")
def verify_security_audit(db: Session = Depends(get_db)):
    rows = db.query(SecurityAuditEvent).order_by(SecurityAuditEvent.id).all()
    return verify_audit_chain(rows)
