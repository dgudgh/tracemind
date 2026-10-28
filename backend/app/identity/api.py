from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.identity.models import GroupMapping, IdentityProvider, LoginAudit
from app.identity.schemas import ExternalLoginRequest, GroupMappingCreate, IdentityProviderCreate, IdentityProviderRead
from app.identity.service import authenticate_external, encode_secret_config
from app.services.access_control import is_superuser_identity

router = APIRouter(prefix="/identity", tags=["企业认证"])


def _require_superuser_for_admin_mapping(request: Request, role: str | None) -> None:
    """A group mapping of ``role="admin"`` mints a superuser on every SSO login.

    ``map_identity`` copies the mapped role straight onto the local account, and
    ``role="admin"`` bypasses all policy in the request pipeline. Creating such a
    mapping therefore needs the same superuser-only boundary as granting the role
    through the user administration endpoints -- otherwise a delegated
    ``platform.security`` operator could promote an entire directory group.
    """
    if role != "admin":
        return
    if not is_superuser_identity(getattr(request.state, "auth", None)):
        raise HTTPException(status_code=403, detail="只有平台管理员可以授予管理员角色")


def _provider_payload(row: IdentityProvider) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "priority": row.priority,
        "enabled": bool(row.enabled),
        "configuration": row.configuration or {},
        "has_secret": bool(row.secret_config),
    }


@router.get("/providers", response_model=list[IdentityProviderRead])
def list_providers(db: Session = Depends(get_db)):
    rows = db.query(IdentityProvider).order_by(IdentityProvider.priority, IdentityProvider.id).all()
    return [_provider_payload(row) for row in rows]


@router.post("/providers", response_model=IdentityProviderRead)
def create_provider(payload: IdentityProviderCreate, db: Session = Depends(get_db)):
    row = IdentityProvider(
        name=payload.name,
        provider_type=payload.provider_type,
        priority=payload.priority,
        enabled=int(payload.enabled),
        configuration=payload.configuration,
        secret_config=encode_secret_config(payload.secret_config),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _provider_payload(row)


@router.post("/providers/{provider_id}/mappings")
def create_group_mapping(provider_id: int, payload: GroupMappingCreate, request: Request, db: Session = Depends(get_db)):
    _require_superuser_for_admin_mapping(request, payload.role)
    if not db.get(IdentityProvider, provider_id):
        raise HTTPException(status_code=404, detail="identity provider not found")
    row = GroupMapping(
        provider_id=provider_id,
        external_group=payload.external_group,
        role=payload.role,
        module_keys=payload.module_keys,
        enabled=int(payload.enabled),
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "provider_id": row.provider_id}


@router.post("/providers/{provider_id}/authenticate")
def authenticate_provider(provider_id: int, payload: ExternalLoginRequest, db: Session = Depends(get_db)):
    provider = db.get(IdentityProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="identity provider not found")
    if provider.provider_type == "oidc":
        raise HTTPException(status_code=400, detail="请使用 OIDC 授权码登录流程进行测试")
    identity = authenticate_external(provider_id, payload.credentials, db=db)
    if identity is None:
        raise HTTPException(status_code=401, detail="external authentication failed")
    return {"subject": identity.subject, "username": identity.username, "groups": identity.groups}


@router.get("/login-audit")
def list_login_audit(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(LoginAudit).order_by(LoginAudit.id.desc()).limit(max(1, min(limit, 500))).all()
    return [
        {
            "id": row.id,
            "provider_id": row.provider_id,
            "provider_type": row.provider_type,
            "masked_username": row.masked_username,
            "outcome": row.outcome,
            "reason": row.reason,
            "details": row.details,
            "create_time": row.create_time,
        }
        for row in rows
    ]
