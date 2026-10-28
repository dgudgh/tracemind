import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.jwt_keys import decode_access_token, issue_access_token

logger = logging.getLogger("app.core.auth")

# Persistent sessions must be revalidated on every request, including cache hits.
# Only explicitly ephemeral MES sessions use process-local state as authority.
AUTH_TOKENS: dict[str, dict] = {}
TOKEN_EXPIRE_SECONDS = settings.AUTH_TOKEN_EXPIRE_SECONDS


class AuthenticationUnavailable(RuntimeError):
    """Persistent identity could not be checked; this is not an invalid token."""


def _utc_now_naive() -> datetime:
    """Return UTC without tzinfo for databases configured with naive DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def invalidate_user_tokens(user_id: int, db=None, reason: str = "user_security_changed", *, commit: bool = True) -> None:
    if db is None and not commit:
        raise ValueError("Deferred revocation requires a caller-owned transaction")
    stale = [token for token, info in AUTH_TOKENS.items() if info.get("user_id") == user_id]
    for token in stale:
        AUTH_TOKENS.pop(token, None)
    owns_session = db is None
    try:
        if owns_session:
            from app.core.database import SessionLocal

            db = SessionLocal()
        from app.models.access_control import AuthSession

        now = _utc_now_naive()
        db.query(AuthSession).filter(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": now, "revoke_reason": reason[:300]}, synchronize_session=False)
        if commit:
            db.commit()
    except SQLAlchemyError:
        if db is not None:
            db.rollback()
        raise
    finally:
        if owns_session and db is not None:
            db.close()


def _password_version(user) -> str:
    changed_at = getattr(user, "password_changed_at", None)
    return changed_at.isoformat() if changed_at else ""


def _credential_version(user) -> str:
    # Salted credential changes remain distinct even when DATETIME truncates to seconds.
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()


def issue_user_token(
    user,
    *,
    source: str = "db",
    db=None,
    mfa_verified: bool = False,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    ephemeral = source == "mes" and db is None and user.id == 0 and user.role == "user"
    if source not in {"db", "identity", "mes"}:
        raise ValueError("Unsupported authentication source")
    if db is None and not ephemeral:
        raise ValueError("Persistent authentication requires a database session")
    if source in {"db", "identity"} and (not user.id or user.id <= 0):
        raise ValueError("Persistent authentication requires a local user")
    jti = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TOKEN_EXPIRE_SECONDS)
    info = {
        "username": user.username,
        "source": source,
        "user_id": user.id,
        "role": user.role,
        "password_version": _password_version(user),
        "mfa_verified": bool(mfa_verified),
        "jti": jti,
        "created_at": time.time(),
        "expires_at": expires_at.timestamp(),
        "ephemeral": ephemeral,
        "last_seen_at": now.timestamp(),
    }
    token = issue_access_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "source": source,
            "password_version": info["password_version"],
            "mfa": bool(mfa_verified),
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "ephemeral": ephemeral,
        }
    )
    if db is not None:
        try:
            from app.models.access_control import AuthSession

            db.add(AuthSession(
                jti_hash=_token_hash(jti),
                user_id=user.id,
                username=user.username,
                source=source,
                role=user.role,
                password_version=info["password_version"],
                mfa_verified=int(bool(mfa_verified)),
                client_ip=(client_ip or "")[:100] or None,
                user_agent=(user_agent or "")[:500] or None,
                expires_at=expires_at.replace(tzinfo=None),
                last_seen_at=now.replace(tzinfo=None),
                create_time=now.replace(tzinfo=None),
            ))
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
    AUTH_TOKENS[token] = info
    return {"token": token, "username": info["username"], "expires_in": TOKEN_EXPIRE_SECONDS}


def authenticate_by_mes(mes_token: str) -> dict | None:
    expected_token = os.getenv("MES_AUTH_TOKEN", "").strip()
    if not expected_token or not mes_token:
        return None
    if not secrets.compare_digest(mes_token, expected_token):
        logger.warning("MES Token 认证失败")
        return None
    username = os.getenv("MES_AUTH_USERNAME", "mes_user").strip() or "mes_user"
    user = type("MesUser", (), {"id": 0, "username": username, "role": "user", "password_changed_at": None})()
    result = issue_user_token(user, source="mes")
    logger.info("MES Token 认证成功 | username=%s", username)
    return result


def authenticate(username: str, password: str, db=None, *, issue_token: bool = True,
                 reset_fail_count: bool = True) -> dict | None:
    """Verify a password and (optionally) issue a session.

    ``reset_fail_count`` exists for two-factor logins: while the second factor is
    still pending, a correct password must not wipe the lockout budget, otherwise
    re-requesting a challenge would reset it on every attempt.
    """
    if db is not None:
        from app.core.security import hash_password, needs_password_rehash, verify_password
        from app.models.user import User

        user = db.query(User).filter(User.username == username).populate_existing().with_for_update().first()
        if user:
            if user.locked_until and user.locked_until > datetime.now():
                return None
            if user.status == "locked" and user.locked_until and user.locked_until <= datetime.now():
                user.status = "active"
                user.locked_until = None
                user.login_fail_count = 0
            if user.status != "active":
                return None
            if verify_password(password, user.password_hash):
                if reset_fail_count:
                    user.login_fail_count = 0
                user.last_login_at = datetime.now()
                if needs_password_rehash(user.password_hash):
                    user.password_hash = hash_password(password)
                from app.api.v1.users import load_password_policy

                policy = load_password_policy(db)
                if user.password_changed_at:
                    expire_days = policy.password_expire_days
                    if expire_days > 0 and (datetime.now() - user.password_changed_at).days > expire_days:
                        db.commit()
                        return {
                            "username": username,
                            "password_expired": True,
                            "password_expire_message": f"密码已超过 {expire_days} 天未修改，请联系管理员重置密码",
                        }
                if not issue_token:
                    verified = {"username": user.username, "user_id": user.id, "role": user.role,
                                "password_verified": True, "password_version": _password_version(user),
                                "credential_version": _credential_version(user)}
                    db.commit()
                    return verified
                # Keep the user lock through session insertion so a concurrent
                # reset cannot upgrade an old-password login to the new version.
                return issue_user_token(user, source="db", db=db)
            from app.api.v1.users import load_password_policy

            policy = load_password_policy(db)
            user.login_fail_count = (user.login_fail_count or 0) + 1
            if user.login_fail_count >= policy.max_login_fail:
                user.locked_until = datetime.now() + timedelta(minutes=policy.lock_duration_minutes)
                user.status = "locked"
                invalidate_user_tokens(user.id, db=db, reason="login_failure_lock")
            db.commit()
            return None
    return None


def _decode_token(token: str) -> dict | None:
    try:
        return decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None


def _persistent_session_info(claims: dict, db) -> dict | None:
    from app.models.access_control import AuthSession
    from app.models.user import User

    jti = str(claims.get("jti") or "")
    if not jti:
        return None
    # Credential changes lock User before AuthSession; validation must use the
    # same order to avoid deadlocks with password changes and account locking.
    user = None
    if claims.get("source") in {"db", "identity"}:
        try:
            user_id = int(claims.get("sub") or 0)
        except (ValueError, TypeError):
            return None
        if user_id <= 0:
            return None
        user = db.query(User).populate_existing().filter(User.id == user_id).with_for_update().first()
        if user is None:
            return None
    row = db.query(AuthSession).populate_existing().filter(
        AuthSession.jti_hash == _token_hash(jti),
    ).with_for_update().first()
    now = _utc_now_naive()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    if str(row.user_id) != claims.get("sub") or row.source != claims.get("source"):
        return None
    idle_seconds = (now - (row.last_seen_at or row.create_time or now)).total_seconds()
    if idle_seconds >= settings.AUTH_SESSION_IDLE_SECONDS:
        row.revoked_at = now
        row.revoke_reason = "idle_timeout"
        db.commit()
        return None
    info = {
        "username": row.username,
        "source": row.source,
        "user_id": row.user_id,
        "role": row.role,
        "password_version": row.password_version,
        "mfa_verified": bool(row.mfa_verified),
        "jti": jti,
        "created_at": row.create_time.replace(tzinfo=timezone.utc).timestamp() if row.create_time else time.time(),
        "expires_at": row.expires_at.replace(tzinfo=timezone.utc).timestamp(),
        "ephemeral": False,
    }
    if row.source in {"db", "identity"}:
        if user is None or user.id != row.user_id:
            return None
        local_now = datetime.now()
        if user.status == "locked" and user.locked_until and user.locked_until <= local_now:
            user.status = "active"
            user.locked_until = None
            user.login_fail_count = 0
        if user.status != "active" or (user.locked_until and user.locked_until > local_now):
            return None
        if info["password_version"] != _password_version(user):
            return None
        info["role"] = user.role
        info["username"] = user.username
    elif row.source != "mes":
        return None
    row.last_seen_at = now
    db.commit()
    return info


def verify_token(token: str, db=None) -> bool:
    return get_token_info(token, db=db) is not None


def get_token_info(token: str, db=None) -> dict | None:
    """Return a verified identity, None for invalid tokens, or raise on storage failure."""
    if not token:
        return None
    claims = _decode_token(token)
    if claims is None:
        AUTH_TOKENS.pop(token, None)
        return None
    if claims.get("ephemeral") is True:
        info = AUTH_TOKENS.get(token)
        now = time.time()
        if (
            info is not None
            and info.get("ephemeral") is True
            and claims.get("source") == info.get("source") == "mes"
            and claims.get("sub") == "0" and info.get("user_id") == 0
            and claims.get("role") == info.get("role") == "user"
            and claims.get("jti") == info.get("jti")
            and now < float(info.get("expires_at") or 0)
            and now - float(info.get("last_seen_at") or 0) < settings.AUTH_SESSION_IDLE_SECONDS
        ):
            info["last_seen_at"] = now
            return dict(info)
        AUTH_TOKENS.pop(token, None)
        return None
    owns_session = db is None
    try:
        if owns_session:
            from app.core.database import SessionLocal

            db = SessionLocal()
        info = _persistent_session_info(claims, db)
        if info is None:
            AUTH_TOKENS.pop(token, None)
            db.rollback()
            return None
        AUTH_TOKENS[token] = info
        return dict(info)
    except SQLAlchemyError as exc:
        AUTH_TOKENS.pop(token, None)
        if db is not None:
            try:
                db.rollback()
            except SQLAlchemyError:
                pass
        raise AuthenticationUnavailable("Authentication storage unavailable") from exc
    finally:
        if owns_session and db is not None:
            try:
                db.close()
            except SQLAlchemyError as exc:
                AUTH_TOKENS.pop(token, None)
                raise AuthenticationUnavailable("Authentication storage unavailable") from exc


def get_bearer_token(authorization: str) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def logout(token: str, db=None, reason: str = "logout"):
    info = AUTH_TOKENS.pop(token, None)
    claims = _decode_token(token) or {}
    if claims.get("ephemeral") is True and claims.get("source") == "mes":
        return
    jti = str(claims.get("jti") or (info or {}).get("jti") or "")
    if not jti:
        return
    owns_session = db is None
    try:
        if owns_session:
            from app.core.database import SessionLocal

            db = SessionLocal()
        from app.models.access_control import AuthSession

        db.query(AuthSession).filter(
            AuthSession.jti_hash == _token_hash(jti),
            AuthSession.revoked_at.is_(None),
        ).update({"revoked_at": _utc_now_naive(), "revoke_reason": reason[:300]}, synchronize_session=False)
        db.commit()
    except SQLAlchemyError:
        if db is not None:
            db.rollback()
        raise
    finally:
        if owns_session and db is not None:
            db.close()
