from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


_ENVELOPE_PREFIX = "tmsec:v2"
_ENV_REFERENCE = re.compile(r"^env://([A-Za-z_][A-Za-z0-9_]*)$")
_VAULT_REFERENCE = re.compile(
    r"^vault://([A-Za-z0-9_.-]+)/([A-Za-z0-9_./-]+)#([A-Za-z0-9_.-]+)$"
)


class SecretResolutionError(ValueError):
    """Raised when a stored secret cannot be safely resolved."""


@dataclass(frozen=True)
class _LocalKey:
    key_id: str
    cipher: Fernet


def _derive_key(material: str) -> _LocalKey:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return _LocalKey(
        key_id=hashlib.sha256(digest).hexdigest()[:12],
        cipher=Fernet(base64.urlsafe_b64encode(digest)),
    )


def _keyring() -> list[_LocalKey]:
    materials = [settings.SECRET_KEY, *settings.secret_key_previous]
    seen: set[str] = set()
    keys: list[_LocalKey] = []
    for material in materials:
        if not material or material in seen:
            continue
        seen.add(material)
        keys.append(_derive_key(material))
    return keys


def encrypt_local_secret(raw_value: str) -> str:
    if raw_value is None:
        raise SecretResolutionError("秘密值不能为空")
    raw = str(raw_value)
    _validate_secret_size(raw)
    primary = _keyring()[0]
    token = primary.cipher.encrypt(raw.encode("utf-8")).decode("ascii")
    return f"{_ENVELOPE_PREFIX}:{primary.key_id}:{token}"


def decrypt_local_secret(stored_value: str) -> str:
    stored = str(stored_value or "")
    if not stored:
        raise SecretResolutionError("保存的秘密值为空")
    keys = _keyring()
    token = stored
    candidates = keys
    if stored.startswith(f"{_ENVELOPE_PREFIX}:"):
        parts = stored.split(":", 3)
        if len(parts) != 4 or not parts[2] or not parts[3]:
            raise SecretResolutionError("保存的秘密值格式无效")
        key_id, token = parts[2], parts[3]
        candidates = [entry for entry in keys if entry.key_id == key_id]
        if not candidates:
            raise SecretResolutionError("保存的秘密值使用了未配置的历史密钥")
    for entry in candidates:
        try:
            value = entry.cipher.decrypt(token.encode("ascii")).decode("utf-8")
            _validate_secret_size(value)
            return value
        except (InvalidToken, UnicodeDecodeError, ValueError):
            continue
    raise SecretResolutionError("保存的连接密码无法解密，请重新录入或恢复历史密钥")


def is_secret_reference(value: str | None) -> bool:
    text = str(value or "")
    return text.startswith("env://") or text.startswith("vault://")


def validate_secret_reference(reference: str) -> str:
    value = str(reference or "").strip()
    if _ENV_REFERENCE.fullmatch(value):
        return value
    vault_match = _VAULT_REFERENCE.fullmatch(value)
    if vault_match:
        path = vault_match.group(2)
        if ".." in path.split("/"):
            raise SecretResolutionError("Vault 秘密引用路径无效")
        return value
    raise SecretResolutionError("仅支持 env://变量名 或 vault://挂载点/路径#字段")


def secret_source(stored_value: str | None) -> str:
    value = str(stored_value or "")
    if not value:
        return "none"
    if value.startswith("env://"):
        return "environment"
    if value.startswith("vault://"):
        return "vault"
    if value.startswith(f"{_ENVELOPE_PREFIX}:"):
        return "local_encrypted"
    return "local_legacy"


def resolve_secret(stored_value: str) -> str:
    value = str(stored_value or "")
    if value.startswith("env://"):
        match = _ENV_REFERENCE.fullmatch(validate_secret_reference(value))
        assert match is not None
        resolved = os.environ.get(match.group(1), "")
        if not resolved:
            raise SecretResolutionError("引用的环境秘密未配置或为空")
        _validate_secret_size(resolved)
        return resolved
    if value.startswith("vault://"):
        return _resolve_vault_secret(validate_secret_reference(value))
    return decrypt_local_secret(value)


def store_secret(*, plaintext: str | None = None, reference: str | None = None) -> str:
    if plaintext and reference:
        raise SecretResolutionError("密码与外部秘密引用不能同时提交")
    if reference:
        return validate_secret_reference(reference)
    if plaintext:
        return encrypt_local_secret(plaintext)
    raise SecretResolutionError("必须提供密码或外部秘密引用")


def rewrap_secret(stored_value: str) -> str:
    if is_secret_reference(stored_value):
        return validate_secret_reference(stored_value)
    return encrypt_local_secret(decrypt_local_secret(stored_value))


def local_secret_requires_rotation(stored_value: str | None) -> bool:
    value = str(stored_value or "")
    if not value or is_secret_reference(value):
        return False
    primary_id = _keyring()[0].key_id
    if not value.startswith(f"{_ENVELOPE_PREFIX}:"):
        return True
    parts = value.split(":", 3)
    return len(parts) != 4 or parts[2] != primary_id


def _resolve_vault_secret(reference: str) -> str:
    match = _VAULT_REFERENCE.fullmatch(reference)
    assert match is not None
    mount, secret_path, field = match.groups()
    base_url = str(settings.SECRETS_VAULT_ADDR or "").strip().rstrip("/")
    if not base_url:
        raise SecretResolutionError("Vault 服务地址未配置")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SecretResolutionError("Vault 服务地址无效")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not loopback:
        raise SecretResolutionError("Vault 服务必须使用 HTTPS")
    token = os.environ.get(settings.SECRETS_VAULT_TOKEN_ENV, "")
    if not token:
        raise SecretResolutionError("Vault 访问令牌未通过环境变量提供")

    import requests

    encoded_path = "/".join(quote(part, safe="") for part in secret_path.split("/"))
    url = f"{base_url}/v1/{quote(mount, safe='')}/data/{encoded_path}"
    headers = {"X-Vault-Token": token}
    if settings.SECRETS_VAULT_NAMESPACE:
        headers["X-Vault-Namespace"] = settings.SECRETS_VAULT_NAMESPACE
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=settings.SECRETS_RESOLVE_TIMEOUT_SECONDS,
            verify=settings.SECRETS_VAULT_VERIFY_TLS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SecretResolutionError("Vault 秘密读取失败") from exc
    except ValueError as exc:
        raise SecretResolutionError("Vault 返回了无效响应") from exc

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    values = data.get("data", data) if isinstance(data, dict) else {}
    resolved = values.get(field) if isinstance(values, dict) else None
    if resolved is None or str(resolved) == "":
        raise SecretResolutionError("Vault 引用字段不存在或为空")
    value = str(resolved)
    _validate_secret_size(value)
    return value


def _validate_secret_size(value: str) -> None:
    if len(value.encode("utf-8")) > settings.SECRETS_MAX_VALUE_BYTES:
        raise SecretResolutionError("秘密值超过允许大小")
