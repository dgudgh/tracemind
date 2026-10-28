"""Password hashing helpers with backward compatibility for legacy records."""

import base64
import hashlib
import hmac
import os
import re


PBKDF2_ITERATIONS = 310_000
PBKDF2_SCHEME = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "$".join(
        (
            PBKDF2_SCHEME,
            str(PBKDF2_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    if password_hash.startswith(f"{PBKDF2_SCHEME}$"):
        try:
            scheme, iterations, salt_text, digest_text = password_hash.split("$", 3)
            if scheme != PBKDF2_SCHEME:
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False

    # Existing deployments stored unsalted SHA-256 values. Accept them once so
    # successful logins can migrate the record to PBKDF2.
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, password_hash)


def needs_password_rehash(password_hash: str) -> bool:
    return not password_hash.startswith(f"{PBKDF2_SCHEME}${PBKDF2_ITERATIONS}$")


def password_complexity_issues(password: str) -> list[str]:
    issues = []
    if len(password) < 8:
        issues.append("密码长度不能少于 8 位")
    if not re.search(r"[A-Z]", password):
        issues.append("密码需包含至少一个大写字母")
    if not re.search(r"[a-z]", password):
        issues.append("密码需包含至少一个小写字母")
    if not re.search(r"\d", password):
        issues.append("密码需包含至少一个数字")
    return issues
