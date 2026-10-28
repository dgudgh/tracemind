from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import get_db
from app.core.security import hash_password, password_complexity_issues, verify_password
from app.models.user import User, UserModulePermission

router = APIRouter(prefix="/users", tags=["用户管理"])


def _check_password_complexity(password: str) -> list[str]:
    return password_complexity_issues(password)


def _require_superuser_for_admin_role(request: Request, granting_role: str | None) -> None:
    """``role="admin"`` bypasses every policy check in the request pipeline.

    Granting it -- by creating such an account or by promoting an existing one --
    therefore has to be reserved for a superuser. Otherwise a delegated
    ``platform.security`` operator could silently promote itself.

    Deliberately narrow: the platform already treats ``platform.security`` as a
    powerful delegated credential-administrator role (see
    ``test_explicit_security_role_can_reset_using_its_own_password``), so editing or
    resetting an existing administrator is *not* restricted here.
    """
    if granting_role != "admin":
        return
    from app.services.access_control import is_superuser_identity

    if not is_superuser_identity(getattr(request.state, "auth", None)):
        raise HTTPException(403, "只有平台管理员可以授予管理员角色")


def _require_superuser_for_admin_account(request: Request, user: User) -> None:
    """Deleting an administrator account is reserved for a superuser.

    The pre-existing rule in ``delete_user`` ("不能删除管理员账号") only recognises the
    literal username ``admin``, but that is not the account this platform creates:
    ``/setup`` forwards the operator's own ``admin_username`` to ``_init_admin_user``,
    so the superuser is routinely called something else. The rule was therefore
    enforced against the wrong key.

    Why this is not merely cosmetic given that a delegated ``platform.security``
    holder may already reset an administrator's password (a documented rule asserted
    by ``test_explicit_security_role_can_reset_using_its_own_password``): with
    ``MFA_REQUIRED_FOR_ADMIN`` enabled an administrator cannot log in without an
    enabled MFA credential, and a password reset leaves that credential in place, so
    the documented power does not hand over the account. Deletion does -- it cascades
    over the credential, the role assignments, the asset scopes and the sessions, and
    cannot be undone. Hence: decide on the role, not on a username string.
    """
    if user.role != "admin":
        return
    from app.services.access_control import is_superuser_identity

    if not is_superuser_identity(getattr(request.state, "auth", None)):
        raise HTTPException(403, "只有平台管理员可以删除管理员账号")


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: Literal["admin", "user"] = "user"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    role: Literal["admin", "user"] | None = None
    status: str | None = None
    locked_until: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


class SetPermissionsRequest(BaseModel):
    module_keys: list[str]


class PasswordPolicyConfig(BaseModel):
    password_expire_days: int = Field(90, ge=0, le=3650)
    max_login_fail: int = Field(5, ge=1, le=20)
    lock_duration_minutes: int = Field(30, ge=1, le=1440)
    enforce_complexity: bool = True


def load_password_policy(db: Session) -> PasswordPolicyConfig:
    from app.services import system_settings as settings_svc

    return PasswordPolicyConfig(
        password_expire_days=settings_svc.get_int(db, "password_expire_days", 90),
        max_login_fail=settings_svc.get_int(db, "max_login_fail", 5),
        lock_duration_minutes=settings_svc.get_int(db, "lock_duration_minutes", 30),
        enforce_complexity=bool(settings_svc.get(db, "enforce_password_complexity")),
    )


@router.get("")
def list_users(db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.id).all()
    return {"items": [_user_to_dict(u) for u in users]}


@router.post("")
def create_user(req: CreateUserRequest, request: Request, db: Session = Depends(get_db)):
    _require_superuser_for_admin_role(request, req.role)
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "用户名已存在")
    if load_password_policy(db).enforce_complexity:
        issues = _check_password_complexity(req.password)
        if issues:
            raise HTTPException(400, "；".join(issues))
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name or req.username,
        role=req.role,
        status="active",
        password_changed_at=datetime.now(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_to_dict(user)


@router.get("/password-policy")
def get_password_policy(db: Session = Depends(get_db)):
    return load_password_policy(db).model_dump()


@router.put("/password-policy")
def update_password_policy(req: PasswordPolicyConfig, db: Session = Depends(get_db)):
    from app.services import system_settings as settings_svc

    settings_svc.update_settings(db, {
        "password_expire_days": req.password_expire_days,
        "max_login_fail": req.max_login_fail,
        "lock_duration_minutes": req.lock_duration_minutes,
        "enforce_password_complexity": req.enforce_complexity,
    })
    return load_password_policy(db).model_dump()


@router.put("/{user_id}")
def update_user(user_id: int, req: UpdateUserRequest, request: Request, db: Session = Depends(get_db)):
    from app.core.auth import invalidate_user_tokens

    try:
        user = db.query(User).filter(User.id == user_id).populate_existing().with_for_update().first()
        if not user:
            raise HTTPException(404, "用户不存在")
        _require_superuser_for_admin_role(request, req.role)
        if req.display_name is not None:
            user.display_name = req.display_name
        if req.role is not None:
            user.role = req.role
        if req.status is not None:
            user.status = req.status
        if req.locked_until is not None:
            try:
                user.locked_until = datetime.fromisoformat(req.locked_until) if req.locked_until else None
            except ValueError:
                raise HTTPException(400, "锁定时间格式无效") from None
        invalidate_user_tokens(user.id, db=db, commit=False)
        db.flush()
        result = _user_to_dict(user)
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "用户修改未完成，请稍后重试") from None


@router.put("/{user_id}/reset-password")
def reset_password(user_id: int, req: ChangePasswordRequest, request: Request, db: Session = Depends(get_db)):
    from app.core.auth import _password_version, invalidate_user_tokens
    from app.services.access_control import append_security_audit, is_action_allowed

    identity = getattr(request.state, "auth", None) or {}
    actor_id = identity.get("user_id")
    if not isinstance(actor_id, int) or actor_id <= 0:
        raise HTTPException(401, "请先登录")
    if identity.get("source") != "db":
        raise HTTPException(403, "此操作需要使用本地账号登录并验证当前密码")

    try:
        # Serialize credential checks and changes, including two admins resetting each other.
        users = db.query(User).filter(User.id.in_({actor_id, user_id})).order_by(User.id).populate_existing().with_for_update().all()
        actor = next((row for row in users if row.id == actor_id), None)
        user = next((row for row in users if row.id == user_id), None)
        now = datetime.now()
        if not actor or actor.status != "active" or (actor.locked_until and actor.locked_until > now):
            raise HTTPException(401, "当前账号不可用，请重新登录")
        if identity.get("password_version") != _password_version(actor):
            raise HTTPException(401, "登录状态已失效，请重新登录")
        own_password = actor_id == user_id
        if not own_password and actor.role != "admin":
            allowed, explicit = is_action_allowed(db, actor_id, "platform.security")
            if not (allowed and explicit):
                raise HTTPException(403, "需要用户安全管理权限")
        if not user:
            raise HTTPException(404, "用户不存在")
        policy = load_password_policy(db)

        def audit(outcome, reason):
            event = append_security_audit(
                db, category="account", action="password_change" if own_password else "password_reset",
                actor=actor.username, actor_user_id=actor.id, outcome=outcome,
                target_type="user", target_id=user.id, details={"reason": reason},
            )
            if event is None:
                raise HTTPException(503, "安全审计写入失败，密码未修改")

        if not verify_password(req.current_password, actor.password_hash):
            actor.login_fail_count = (actor.login_fail_count or 0) + 1
            locked = actor.login_fail_count >= policy.max_login_fail
            if locked:
                actor.status = "locked"
                actor.locked_until = now + timedelta(minutes=policy.lock_duration_minutes)
                invalidate_user_tokens(actor.id, db=db, reason="password_reauthentication_locked", commit=False)
            audit("failure", "current_credential_invalid")
            db.commit()
            if locked:
                raise HTTPException(401, "当前密码连续验证失败，账号已锁定，请稍后重新登录")
            raise HTTPException(400, "当前密码不正确" if own_password else "操作管理员的当前密码不正确")
        if policy.enforce_complexity:
            issues = _check_password_complexity(req.new_password)
            if issues:
                raise HTTPException(400, "；".join(issues))
        if verify_password(req.new_password, user.password_hash):
            raise HTTPException(400, "新密码不能与原密码相同")
        actor.login_fail_count = 0
        user.password_hash = hash_password(req.new_password)
        user.password_changed_at = now
        user.login_fail_count = 0
        user.locked_until = None
        if user.status == "locked":
            user.status = "active"
        invalidate_user_tokens(user.id, db=db, reason="password_changed", commit=False)
        audit("success", "current_credential_verified")
        db.commit()
        return {"detail": "密码已修改，请重新登录" if own_password else "密码已重置，目标用户需重新登录", "reauthenticate": own_password}
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "密码修改未完成，请稍后重试") from None


@router.delete("/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.username == "admin":
        raise HTTPException(400, "不能删除管理员账号")
    _require_superuser_for_admin_account(request, user)
    db.query(UserModulePermission).filter(UserModulePermission.user_id == user_id).delete()
    from app.models.access_control import (
        AuthSession,
        UserAccessRole,
        UserAssetScope,
        UserMfaCredential,
        UserOrganization,
    )
    db.query(UserAccessRole).filter(UserAccessRole.user_id == user_id).delete(synchronize_session=False)
    db.query(UserAssetScope).filter(UserAssetScope.user_id == user_id).delete(synchronize_session=False)
    db.query(UserOrganization).filter(UserOrganization.user_id == user_id).delete(synchronize_session=False)
    db.query(UserMfaCredential).filter(UserMfaCredential.user_id == user_id).delete(synchronize_session=False)
    db.query(AuthSession).filter(AuthSession.user_id == user_id).delete(synchronize_session=False)
    from app.core.auth import invalidate_user_tokens
    invalidate_user_tokens(user.id, db=db)
    db.delete(user)
    db.commit()
    return {"detail": "用户已删除"}


@router.get("/{user_id}/permissions")
def get_permissions(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    perms = db.query(UserModulePermission).filter(UserModulePermission.user_id == user_id).all()
    return {"user_id": user_id, "role": user.role, "module_keys": [p.module_key for p in perms]}


@router.put("/{user_id}/permissions")
def set_permissions(user_id: int, req: SetPermissionsRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    db.query(UserModulePermission).filter(UserModulePermission.user_id == user_id).delete()
    for key in req.module_keys:
        db.add(UserModulePermission(user_id=user_id, module_key=key))
    db.commit()
    return {"user_id": user_id, "module_keys": req.module_keys}


@router.post("/{user_id}/unlock")
def unlock_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    actor_id = (getattr(request.state, "auth", None) or {}).get("user_id")
    if not actor_id:
        raise HTTPException(401, "请先登录")
    if actor_id == user_id:
        raise HTTPException(403, "不能自行清除安全锁定或密码验证失败次数，请联系其他管理员")
    user = db.query(User).filter(User.id == user_id).populate_existing().with_for_update().first()
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.status != "locked":
        db.rollback()
        raise HTTPException(400, "仅可解锁已锁定账号")
    user.locked_until = None
    user.login_fail_count = 0
    user.status = "active"
    db.commit()
    return {"detail": "用户已解锁"}


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "password_changed_at": user.password_changed_at.isoformat(sep=" ") if user.password_changed_at else None,
        "locked_until": user.locked_until.isoformat(sep=" ") if user.locked_until else None,
        "login_fail_count": user.login_fail_count,
        "last_login_at": user.last_login_at.isoformat(sep=" ") if user.last_login_at else None,
        "create_time": user.create_time.isoformat(sep=" ") if user.create_time else None,
    }
