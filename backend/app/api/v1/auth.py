import secrets
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import (
    authenticate,
    authenticate_by_mes,
    get_bearer_token,
    get_token_info,
    logout,
    verify_token,
)
from app.core.database import get_db
from app.core.config import settings
from app.core.http_security import require_http_url
from app.core.jwt_keys import jwks_document
from app.core.mfa import (
    generate_recovery_codes,
    generate_secret,
    provisioning_uri,
    recovery_code_hash,
    verify_totp,
)
from app.core.secrets import resolve_secret, store_secret
from app.identity.models import IdentityProvider
from app.models.access_control import AuthSession, UserMfaCredential
from app.models.user import User
from app.services.access_control import append_security_audit
from app.identity.providers.oidc import OIDCIdentityProvider
from app.identity.service import map_identity, provider_runtime_configuration, record_login_audit

router = APIRouter(prefix="/auth", tags=["认证"])
OIDC_PENDING_STATES: dict[str, dict] = {}
OIDC_LOGIN_CODES: dict[str, dict] = {}
OIDC_PENDING_STATE_TTL_SECONDS = 300
OIDC_LOGIN_CODE_TTL_SECONDS = 120
OIDC_CACHE_MAX_ITEMS = 2048
MFA_LOGIN_CHALLENGES: dict[str, dict] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


class MesAuthRequest(BaseModel):
    token: str


class LogoutRequest(BaseModel):
    token: str | None = None


class OidcExchangeRequest(BaseModel):
    code: str


class MfaVerifyRequest(BaseModel):
    challenge_token: str = Field(..., min_length=20, max_length=500)
    code: str = Field(..., min_length=6, max_length=20)


class MfaCodeRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=20)


def _prune_oidc_cache(
    cache: dict[str, dict],
    *,
    max_age_seconds: int,
    reserve_slot: bool = False,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    for key, item in list(cache.items()):
        if current - float(item.get("created_at") or 0) > max_age_seconds:
            cache.pop(key, None)
    target_size = OIDC_CACHE_MAX_ITEMS - (1 if reserve_slot else 0)
    overflow = max(0, len(cache) - target_size)
    for key, _ in sorted(cache.items(), key=lambda pair: float(pair[1].get("created_at") or 0))[:overflow]:
        cache.pop(key, None)


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    credential = db.query(UserMfaCredential).filter(UserMfaCredential.user_id == user.id).first() if user else None
    mfa_required = bool(credential and credential.enabled) or bool(user and user.role == "admin" and settings.MFA_REQUIRED_FOR_ADMIN)
    # While the second factor is pending the lockout budget must survive this call,
    # otherwise each re-issued challenge would hand the attacker a fresh budget.
    result = authenticate(
        req.username, req.password, db=db,
        issue_token=not mfa_required, reset_fail_count=not mfa_required,
    )
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if result.get("password_expired"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=result["password_expire_message"])
    if mfa_required:
        if not credential or not credential.enabled:
            raise HTTPException(status_code=403, detail="管理员账号必须先启用 MFA")
        _prune_oidc_cache(MFA_LOGIN_CHALLENGES, max_age_seconds=settings.AUTH_MFA_CHALLENGE_SECONDS, reserve_slot=True)
        challenge = secrets.token_urlsafe(40)
        MFA_LOGIN_CHALLENGES[challenge] = {
            "user_id": result["user_id"],
            "source": "db",
            "password_version": result["password_version"],
            "credential_version": result["credential_version"],
            "created_at": time.time(),
        }
        return {
            "mfa_required": True,
            "challenge_token": challenge,
            "expires_in": settings.AUTH_MFA_CHALLENGE_SECONDS,
            "username": user.username,
        }
    return result


def _verify_mfa_code(credential: UserMfaCredential, code: str, db: Session, *, commit: bool = True) -> bool:
    secret = resolve_secret(credential.secret_cipher)
    counter = verify_totp(secret, code, last_counter=credential.last_totp_counter)
    if counter is not None:
        credential.last_totp_counter = counter
        if commit:
            db.commit()
        return True
    expected = recovery_code_hash(code)
    hashes = list(credential.recovery_code_hashes or [])
    if expected in hashes:
        hashes.remove(expected)
        credential.recovery_code_hashes = hashes
        if commit:
            db.commit()
        return True
    return False


@router.post("/mfa/verify")
def verify_mfa_login(payload: MfaVerifyRequest, db: Session = Depends(get_db)):
    _prune_oidc_cache(MFA_LOGIN_CHALLENGES, max_age_seconds=settings.AUTH_MFA_CHALLENGE_SECONDS)
    challenge = MFA_LOGIN_CHALLENGES.pop(payload.challenge_token, None)
    if not challenge:
        raise HTTPException(status_code=401, detail="MFA 验证已过期，请重新登录")
    from app.core.auth import _credential_version, _password_version

    user = _locked_mfa_user(db, challenge["user_id"])
    credential = db.query(UserMfaCredential).filter(UserMfaCredential.user_id == challenge["user_id"]).populate_existing().with_for_update().first()
    valid_identity = bool(user and user.status == "active"
                          and not (user.locked_until and user.locked_until > datetime.now())
                          and challenge.get("password_version") == _password_version(user)
                          and challenge.get("credential_version") == _credential_version(user))
    if not valid_identity or not credential or not credential.enabled or not _verify_mfa_code(credential, payload.code, db, commit=False):
        account_locked = False
        if valid_identity and user is not None:
            # A wrong second factor has to consume the same lockout budget as a wrong
            # password. Otherwise a known password is enough to brute-force the
            # 6-digit TOTP space, because /login hands out a fresh challenge each time.
            from datetime import timedelta

            from app.api.v1.users import load_password_policy
            from app.core.auth import invalidate_user_tokens

            policy = load_password_policy(db)
            user.login_fail_count = (user.login_fail_count or 0) + 1
            if user.login_fail_count >= policy.max_login_fail:
                user.status = "locked"
                user.locked_until = datetime.now() + timedelta(minutes=policy.lock_duration_minutes)
                invalidate_user_tokens(user.id, db=db, reason="mfa_verification_locked", commit=False)
                account_locked = True
        append_security_audit(
            db,
            category="authentication",
            action="mfa_login",
            actor=user.username if user else "unknown",
            actor_user_id=user.id if user else None,
            outcome="failure",
            details={
                "reason": "invalid_mfa_code" if valid_identity else "mfa_identity_changed",
                "login_fail_count": (user.login_fail_count if valid_identity and user else None),
                "account_locked": account_locked,
            },
            retention_days=settings.SECURITY_AUDIT_RETENTION_DAYS,
        )
        db.commit()
        raise HTTPException(status_code=401, detail="MFA 验证码无效")
    from app.core.auth import issue_user_token

    # Only a completed two-factor login clears the budget that /login left intact.
    user.login_fail_count = 0
    result = issue_user_token(user, source=challenge.get("source") or "db", db=db, mfa_verified=True)
    append_security_audit(
        db,
        category="authentication",
        action="mfa_login",
        actor=user.username,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        retention_days=settings.SECURITY_AUDIT_RETENTION_DAYS,
    )
    db.commit()
    return result


def _request_identity(request: Request) -> tuple[int, str]:
    info = getattr(request.state, "auth", {}) or {}
    user_id = int(info.get("user_id") or 0)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")
    return user_id, str(info.get("username") or user_id)


def _locked_mfa_user(db: Session, user_id: int) -> User | None:
    if db.get_bind(mapper=User).dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. A no-op write locks before reading MFA state,
        # even inside an existing transaction; preserve the account update timestamp.
        db.execute(User.__table__.update().where(User.id == user_id).values(id=User.id, update_time=User.update_time))
    return db.query(User).filter(User.id == user_id).populate_existing().with_for_update().first()


def _locked_mfa_credential(db: Session, user_id: int) -> UserMfaCredential | None:
    # The user row also serializes first enrollment, before a credential exists.
    user = _locked_mfa_user(db, user_id)
    if user is None or user.status != "active" or (user.locked_until and user.locked_until > datetime.now()):
        raise HTTPException(401, "当前账号不可用，请重新登录")
    return db.query(UserMfaCredential).filter(UserMfaCredential.user_id == user_id).populate_existing().with_for_update().first()


@router.get("/jwks")
def jwks():
    return jwks_document()


@router.post("/mfa/enroll")
def enroll_mfa(request: Request, db: Session = Depends(get_db)):
    user_id, username = _request_identity(request)
    row = _locked_mfa_credential(db, user_id)
    if row is not None and row.enabled:
        raise HTTPException(409, "MFA 已启用，请先验证并停用后重新注册")
    secret = generate_secret()
    recovery_codes = generate_recovery_codes()
    if row is None:
        row = UserMfaCredential(user_id=user_id, secret_cipher=store_secret(plaintext=secret))
        db.add(row)
    else:
        row.secret_cipher = store_secret(plaintext=secret)
        row.enabled = 0
        row.last_totp_counter = None
    row.recovery_code_hashes = [recovery_code_hash(code) for code in recovery_codes]
    db.commit()
    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri(secret, username, settings.MFA_ISSUER),
        "recovery_codes": recovery_codes,
        "note": "密钥与恢复码仅在本次注册时显示",
    }


@router.post("/mfa/confirm")
def confirm_mfa(payload: MfaCodeRequest, request: Request, db: Session = Depends(get_db)):
    user_id, username = _request_identity(request)
    row = _locked_mfa_credential(db, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="请先发起 MFA 注册")
    if not _verify_mfa_code(row, payload.code, db, commit=False):
        raise HTTPException(status_code=422, detail="MFA 验证码无效")
    row.enabled = 1
    row.verified_at = datetime.now()
    append_security_audit(
        db,
        category="authentication",
        action="mfa_enabled",
        actor=username,
        actor_user_id=user_id,
        target_type="user",
        target_id=user_id,
        retention_days=settings.SECURITY_AUDIT_RETENTION_DAYS,
    )
    db.commit()
    return {"enabled": True}


@router.post("/mfa/disable")
def disable_mfa(payload: MfaCodeRequest, request: Request, db: Session = Depends(get_db)):
    user_id, username = _request_identity(request)
    row = _locked_mfa_credential(db, user_id)
    if row is None or not row.enabled:
        return {"enabled": False}
    if not _verify_mfa_code(row, payload.code, db, commit=False):
        raise HTTPException(status_code=422, detail="MFA 验证码无效")
    row.enabled = 0
    append_security_audit(
        db,
        category="authentication",
        action="mfa_disabled",
        actor=username,
        actor_user_id=user_id,
        target_type="user",
        target_id=user_id,
        retention_days=settings.SECURITY_AUDIT_RETENTION_DAYS,
    )
    db.commit()
    return {"enabled": False}


@router.get("/mfa/status")
def mfa_status(request: Request, db: Session = Depends(get_db)):
    user_id, _ = _request_identity(request)
    row = db.query(UserMfaCredential).filter(UserMfaCredential.user_id == user_id).first()
    return {
        "enabled": bool(row and row.enabled),
        "verified_at": row.verified_at if row else None,
        "recovery_codes_remaining": len(row.recovery_code_hashes or []) if row else 0,
    }


@router.get("/sessions")
def list_sessions(request: Request, db: Session = Depends(get_db)):
    user_id, _ = _request_identity(request)
    rows = db.query(AuthSession).filter(AuthSession.user_id == user_id).order_by(AuthSession.id.desc()).limit(100).all()
    return {"items": [
        {
            "id": row.id,
            "source": row.source,
            "mfa_verified": bool(row.mfa_verified),
            "client_ip": row.client_ip,
            "user_agent": row.user_agent,
            "expires_at": row.expires_at,
            "revoked_at": row.revoked_at,
            "last_seen_at": row.last_seen_at,
            "create_time": row.create_time,
        }
        for row in rows
    ]}


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: int, request: Request, db: Session = Depends(get_db)):
    user_id, _ = _request_identity(request)
    row = db.query(AuthSession).filter(AuthSession.id == session_id, AuthSession.user_id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    row.revoked_at = datetime.now()
    row.revoke_reason = "manual_revoke"
    db.commit()
    return {"detail": "会话已撤销"}


@router.post("/external-login")
def external_login(req: LoginRequest, db: Session = Depends(get_db)):
    from app.identity.service import login_external

    result = login_external({"username": req.username, "password": req.password}, db=db)
    if not result:
        raise HTTPException(status_code=401, detail="企业账号认证失败")
    return result


@router.get("/oidc/providers")
def oidc_providers(db: Session = Depends(get_db)):
    rows = db.query(IdentityProvider).filter(
        IdentityProvider.provider_type == "oidc",
        IdentityProvider.enabled == 1,
    ).order_by(IdentityProvider.priority, IdentityProvider.id).all()
    return {"items": [{"id": row.id, "name": row.name} for row in rows]}


def _runtime_provider(provider: IdentityProvider):
    return SimpleNamespace(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        configuration=provider_runtime_configuration(provider),
    )


@router.get("/oidc/{provider_id}/start")
def oidc_start(provider_id: int, db: Session = Depends(get_db)):
    provider = db.get(IdentityProvider, provider_id)
    if provider is None or not provider.enabled or provider.provider_type != "oidc":
        raise HTTPException(status_code=404, detail="OIDC 身份源不存在或未启用")
    runtime_provider = _runtime_provider(provider)
    redirect_uri = require_http_url(str(runtime_provider.configuration.get("redirect_uri") or ""))
    try:
        _prune_oidc_cache(
            OIDC_PENDING_STATES,
            max_age_seconds=OIDC_PENDING_STATE_TTL_SECONDS,
            reserve_slot=True,
        )
        request = OIDCIdentityProvider().begin_authorization(runtime_provider, redirect_uri, OIDC_PENDING_STATES)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="OIDC 身份源配置不完整") from exc
    return {"authorization_url": request.authorization_url}


def _redirect_with_login_code(uri: str, code: str) -> str:
    parsed = urlsplit(require_http_url(uri))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["external_login_code"] = code
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


@router.get("/oidc/callback")
def oidc_callback(state: str, code: str, db: Session = Depends(get_db)):
    _prune_oidc_cache(OIDC_PENDING_STATES, max_age_seconds=OIDC_PENDING_STATE_TTL_SECONDS)
    pending = OIDC_PENDING_STATES.get(state)
    provider = db.get(IdentityProvider, pending.get("provider_id")) if pending else None
    if provider is None or not provider.enabled or provider.provider_type != "oidc":
        OIDC_PENDING_STATES.pop(state, None)
        raise HTTPException(status_code=401, detail="OIDC 登录状态无效或已过期")
    runtime_provider = _runtime_provider(provider)
    try:
        identity = OIDCIdentityProvider().complete_authorization(
            runtime_provider,
            state=state,
            code=code,
            pending_states=OIDC_PENDING_STATES,
        )
        record_login_audit(db, provider=provider, username=identity.username, outcome="success", details={"flow": "oidc"})
        user = map_identity(identity, db=db)
        from app.core.auth import issue_user_token

        token_result = issue_user_token(user, source="identity", db=db)
        login_code = secrets.token_urlsafe(32)
        _prune_oidc_cache(
            OIDC_LOGIN_CODES,
            max_age_seconds=OIDC_LOGIN_CODE_TTL_SECONDS,
            reserve_slot=True,
        )
        OIDC_LOGIN_CODES[login_code] = {"result": token_result, "created_at": time.time()}
        redirect_uri = str((provider.configuration or {}).get("frontend_redirect_uri") or "")
        return RedirectResponse(_redirect_with_login_code(redirect_uri, login_code), status_code=302)
    except HTTPException:
        raise
    except Exception as exc:
        record_login_audit(db, provider=provider, username="", outcome="failure", reason="oidc_callback_rejected", details={"flow": "oidc"})
        db.commit()
        raise HTTPException(status_code=401, detail="OIDC 登录校验失败") from exc


@router.post("/oidc/exchange")
def oidc_exchange(payload: OidcExchangeRequest):
    _prune_oidc_cache(OIDC_LOGIN_CODES, max_age_seconds=OIDC_LOGIN_CODE_TTL_SECONDS)
    item = OIDC_LOGIN_CODES.pop(payload.code, None)
    if not item:
        raise HTTPException(status_code=401, detail="OIDC 登录凭证无效或已过期")
    return item["result"]


@router.post("/mes-login")
def mes_login(req: MesAuthRequest):
    result = authenticate_by_mes(req.token)
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="MES Token 无效")
    return result


@router.post("/logout")
def do_logout(request: Request, payload: LogoutRequest, db: Session = Depends(get_db)):
    token = get_bearer_token(request.headers.get("Authorization", "")) or (payload.token or "")
    logout(token, db=db)
    return {"message": "已登出"}


@router.get("/info")
def auth_info():
    return {
        "enabled": True,
        "mode": "mes",
        "fallback_login": "/api/v1/auth/login",
        "note": "MES 集成需配置 MES_AUTH_TOKEN；本地账号由初始化流程创建。",
    }


@router.get("/current-user")
def current_user(request: Request, db: Session = Depends(get_db)):
    token = get_bearer_token(request.headers.get("Authorization", ""))
    info = get_token_info(token, db=db) if token else None
    if not info:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "id": info.get("user_id"),
        "username": info.get("username"),
        "role": info.get("role", "user"),
        "source": info.get("source"),
        "mfa_verified": bool(info.get("mfa_verified")),
    }
