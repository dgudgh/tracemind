from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    normalized = "".join(secret.upper().split())
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def totp_code(secret: str, counter: int, digits: int = 6) -> str:
    digest = hmac.new(_secret_bytes(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{value:0{digits}d}"


def verify_totp(secret: str, code: str, *, last_counter: int | None = None, now: float | None = None) -> int | None:
    if not code.isdigit() or len(code) != 6:
        return None
    current = int((time.time() if now is None else now) // 30)
    for counter in (current - 1, current, current + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp_code(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, username: str, issuer: str) -> str:
    label = quote(f"{issuer}:{username}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits=6&period=30"


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [f"{secrets.token_hex(4)[:4]}-{secrets.token_hex(4)[:4]}".upper() for _ in range(count)]


def recovery_code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()
