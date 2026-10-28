import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.auth import issue_user_token
from app.core.crypto import decrypt_password, encrypt_password
from app.identity.contracts import AuthenticatedIdentity, ProviderAuthenticationError
from app.identity.models import ExternalIdentity, GroupMapping, IdentityProvider, LoginAudit
from app.identity.providers.ldap import LDAPIdentityProvider
from app.identity.providers.oidc import OIDCIdentityProvider
from app.models.user import User, UserModulePermission

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "authorization",
    "code",
    "id_token",
    "access_token",
    "client_secret",
    "api_key",
    "private_key",
    "credential",
}
_IDENTIFIER_KEYS = {"username", "email", "subject", "login", "principal"}
_ROLE_ORDER = {"user": 0, "operator": 1, "admin": 2}
_SECRET_CIPHER_KEY = "ciphertext"


def mask_identifier(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return f"{text[0]}*"
    return f"{text[0]}{'*' * min(6, len(text) - 2)}{text[-1]}"


def sanitize_audit_details(value: Any, key: str = "") -> Any:
    normalized_key = key.lower()
    if any(marker in normalized_key for marker in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if normalized_key in _IDENTIFIER_KEYS:
        return mask_identifier(value)
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_audit_details(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_audit_details(item, key) for item in value]
    return value


def record_login_audit(
    db: Session,
    *,
    provider: IdentityProvider | None,
    username: object,
    outcome: str,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> LoginAudit:
    row = LoginAudit(
        provider_id=provider.id if provider else None,
        provider_type=provider.provider_type if provider else None,
        masked_username=mask_identifier(username),
        outcome=outcome,
        reason=reason,
        details=sanitize_audit_details(dict(details or {})),
    )
    db.add(row)
    db.flush()
    return row


def active_providers(db: Session) -> list[IdentityProvider]:
    return (
        db.query(IdentityProvider)
        .filter(IdentityProvider.enabled == 1)
        .order_by(IdentityProvider.priority.asc(), IdentityProvider.id.asc())
        .all()
    )


def encode_secret_config(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not value:
        return None
    serialized = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {_SECRET_CIPHER_KEY: encrypt_password(serialized)}


def decode_secret_config(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, Mapping) and value.get(_SECRET_CIPHER_KEY):
        decoded = json.loads(decrypt_password(str(value[_SECRET_CIPHER_KEY])))
        return decoded if isinstance(decoded, dict) else {}
    # Legacy plaintext values are readable for migration but never returned by APIs.
    return dict(value) if isinstance(value, Mapping) else {}


def provider_runtime_configuration(provider: IdentityProvider) -> dict[str, Any]:
    return {**dict(provider.configuration or {}), **decode_secret_config(provider.secret_config)}


def _default_provider_clients() -> dict[str, object]:
    return {"ldap": LDAPIdentityProvider(), "oidc": OIDCIdentityProvider()}


def _authenticate(provider: IdentityProvider, credentials: Mapping[str, Any], provider_clients: Mapping[str, object]) -> AuthenticatedIdentity:
    client = provider_clients.get(provider.provider_type)
    if client is None:
        raise ProviderAuthenticationError(f"unsupported identity provider type: {provider.provider_type}")
    runtime_provider = SimpleNamespace(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        configuration=provider_runtime_configuration(provider),
    )
    identity = client.authenticate(runtime_provider, dict(credentials))
    if not isinstance(identity, AuthenticatedIdentity):
        raise ProviderAuthenticationError("identity provider returned an invalid identity")
    if identity.provider_id != provider.id:
        raise ProviderAuthenticationError("identity provider returned an identity for another provider")
    return identity


def authenticate_external(
    provider_id: int,
    credentials: Mapping[str, Any],
    *,
    db: Session,
    provider_clients: Mapping[str, object] | None = None,
) -> AuthenticatedIdentity | None:
    provider = db.get(IdentityProvider, provider_id)
    username = credentials.get("username") or credentials.get("email") or ""
    if provider is None:
        record_login_audit(db, provider=None, username=username, outcome="failure", reason="provider_not_found", details=credentials)
        db.commit()
        return None
    if not provider.enabled:
        record_login_audit(db, provider=provider, username=username, outcome="skipped", reason="provider_disabled", details=credentials)
        db.commit()
        return None
    try:
        identity = _authenticate(provider, credentials, provider_clients or _default_provider_clients())
    except Exception:
        record_login_audit(db, provider=provider, username=username, outcome="failure", reason="provider_rejected", details=credentials)
        db.commit()
        return None
    record_login_audit(db, provider=provider, username=identity.username, outcome="success", details={"subject": mask_identifier(identity.subject)})
    db.commit()
    return identity


def authenticate_via_provider_chain(
    credentials: Mapping[str, Any],
    *,
    db: Session,
    provider_clients: Mapping[str, object] | None = None,
) -> AuthenticatedIdentity | None:
    clients = provider_clients or _default_provider_clients()
    username = credentials.get("username") or credentials.get("email") or ""
    providers = active_providers(db)
    for provider in providers:
        try:
            identity = _authenticate(provider, credentials, clients)
        except Exception:
            record_login_audit(db, provider=provider, username=username, outcome="failure", reason="provider_rejected", details=credentials)
            db.commit()
            continue
        record_login_audit(db, provider=provider, username=identity.username, outcome="success", details={"subject": mask_identifier(identity.subject)})
        db.commit()
        return identity
    if not providers:
        record_login_audit(
            db,
            provider=None,
            username=username,
            outcome="failure",
            reason="no_enabled_provider",
            details=credentials,
        )
        db.commit()
    return None


def _external_username(db: Session, preferred: str, provider_id: int, subject: str) -> str:
    candidate = preferred[:80]
    existing = db.query(User).filter(User.username == candidate).first()
    if existing is None:
        return candidate
    subject_digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{provider_id}-{subject_digest}"
    return f"{preferred[: 80 - len(suffix)]}{suffix}"


def _mapped_entitlements(db: Session, identity: AuthenticatedIdentity) -> tuple[str, list[str]]:
    groups = {group for group in identity.groups if group}
    mappings = (
        db.query(GroupMapping)
        .filter(GroupMapping.provider_id == identity.provider_id, GroupMapping.enabled == 1)
        .order_by(GroupMapping.id.asc())
        .all()
    )
    matches = [mapping for mapping in mappings if mapping.external_group in groups]
    roles = [mapping.role for mapping in matches if mapping.role in _ROLE_ORDER]
    role = min(roles, key=lambda item: _ROLE_ORDER[item]) if roles else "user"
    modules = sorted({str(module) for mapping in matches for module in (mapping.module_keys or []) if module})
    return role, modules


def map_identity(identity: AuthenticatedIdentity, *, db: Session) -> User:
    external = (
        db.query(ExternalIdentity)
        .filter(ExternalIdentity.provider_id == identity.provider_id, ExternalIdentity.subject == identity.subject)
        .first()
    )
    if external:
        user = db.query(User).filter(User.id == external.user_id).populate_existing().with_for_update().first()
        if user is None or user.status != "active" or (user.locked_until and user.locked_until > datetime.now()):
            raise ProviderAuthenticationError("local_account_unavailable")
    else:
        user = None
    if user is None:
        username = _external_username(db, identity.username, identity.provider_id, identity.subject)
        user = User(username=username, password_hash="!external-identity!", display_name=identity.display_name, status="active")
        db.add(user)
        db.flush()
    role, modules = _mapped_entitlements(db, identity)
    user.display_name = identity.display_name or user.display_name
    user.role = role
    db.query(UserModulePermission).filter(UserModulePermission.user_id == user.id).delete(synchronize_session=False)
    db.add_all([UserModulePermission(user_id=user.id, module_key=module) for module in modules])
    if external is None:
        external = ExternalIdentity(provider_id=identity.provider_id, subject=identity.subject, user_id=user.id, username=user.username)
        db.add(external)
    external.username = user.username
    external.display_name = identity.display_name or ""
    external.groups = list(identity.groups)
    external.status = "active"
    external.last_synced_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user


def login_external(
    credentials: Mapping[str, Any],
    *,
    db: Session,
    provider_clients: Mapping[str, object] | None = None,
) -> dict | None:
    identity = authenticate_via_provider_chain(credentials, db=db, provider_clients=provider_clients)
    if identity is None:
        return None
    try:
        user = map_identity(identity, db=db)
    except ProviderAuthenticationError:
        db.rollback()
        provider = db.get(IdentityProvider, identity.provider_id)
        record_login_audit(db, provider=provider, username=identity.username, outcome="failure", reason="local_account_unavailable")
        db.commit()
        return None
    return issue_user_token(user, source="identity", db=db)


def is_builtin_admin(user: User) -> bool:
    """Should this account be protected from automatic deactivation?

    Keyed on the administrator **role**, not on the literal username ``admin``. The
    installer chooses the superuser's username at setup time (``/setup`` forwards its own
    ``admin_username`` to ``_init_admin_user``), so a name test protects the wrong account
    and lets ``disable_missing_external_users`` lock the platform superuser out during a
    directory sync.

    The name clause is kept as well. It is an existing rule, and it is additive in the
    safe direction: over-protecting only means an administrator has to be disabled by
    hand, whereas under-protecting removes the last way back into the platform.
    """
    return (user.role or "") == "admin" or (user.username or "").casefold() == "admin"


def disable_missing_external_users(
    provider_id: int,
    *,
    seen_subjects: set[str],
    confirmed: bool,
    db: Session,
) -> list[int]:
    if not confirmed:
        return []
    disabled: list[int] = []
    identities = db.query(ExternalIdentity).filter(ExternalIdentity.provider_id == provider_id, ExternalIdentity.status == "active").all()
    for external in identities:
        if external.subject in seen_subjects:
            continue
        user = db.get(User, external.user_id)
        if user is None or is_builtin_admin(user):
            continue
        user.status = "disabled"
        external.status = "disabled"
        disabled.append(user.id)
    db.commit()
    return disabled


__all__ = [
    "AuthenticatedIdentity",
    "ProviderAuthenticationError",
    "active_providers",
    "authenticate_external",
    "authenticate_via_provider_chain",
    "disable_missing_external_users",
    "decode_secret_config",
    "encode_secret_config",
    "login_external",
    "map_identity",
    "provider_runtime_configuration",
    "record_login_audit",
    "sanitize_audit_details",
]
