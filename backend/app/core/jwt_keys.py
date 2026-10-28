from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings


def _b64url(value: int) -> str:
    width = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(width, "big")).rstrip(b"=").decode("ascii")


def _load_or_create_private_key():
    path = settings.jwt_private_key_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(pem)
    except FileExistsError:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    return key


def signing_key():
    return _load_or_create_private_key()


def key_id() -> str:
    public_der = signing_key().public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_der).hexdigest()[:24]


def issue_access_token(claims: dict) -> str:
    return jwt.encode(
        claims,
        signing_key(),
        algorithm="RS256",
        headers={"kid": key_id(), "typ": "JWT"},
    )


def decode_access_token(token: str, *, verify_exp: bool = True) -> dict:
    return jwt.decode(
        token,
        signing_key().public_key(),
        algorithms=["RS256"],
        options={"verify_exp": verify_exp},
    )


def jwks_document() -> dict:
    numbers = signing_key().public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": key_id(),
                "n": _b64url(numbers.n),
                "e": _b64url(numbers.e),
            }
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
