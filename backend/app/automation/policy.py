import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

AUTOMATION_EXECUTION_ENABLED = os.getenv("AUTOMATION_EXECUTION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUTOMATION_ALLOWED_HOSTS = {
    item.strip()
    for item in os.getenv("AUTOMATION_ALLOWED_HOSTS", "").split(",")
    if item.strip()
}
MAX_CAPTURED_OUTPUT_BYTES = max(4096, int(os.getenv("AUTOMATION_MAX_OUTPUT_BYTES", "1048576")))

_TARGET_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")

_SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?imx)
    (
        [\"']?(?:password|passwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*
    )
    (?:
        ([\"']).*?\2
        | [^\s,;]+
    )
    """,
)
_AUTHORIZATION = re.compile(r"(?im)(authorization\s*:\s*(?:bearer|basic)\s+)(\S+)")


@dataclass(frozen=True)
class ExecutablePlan:
    status: str
    request_id: int
    plan_id: int
    target_host: str
    executor_type: str
    script: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def validate_plan_integrity(plan) -> None:
    actual_hash = hashlib.sha256(plan.script.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, plan.script_sha256):
        raise ValueError("script hash mismatch")
    if plan.target_host not in set(plan.host_whitelist or []):
        raise ValueError("target host is not in the whitelist")
    validate_target_host(plan.target_host)
    if plan.validation_script:
        validation_hash = hashlib.sha256(plan.validation_script.encode("utf-8")).hexdigest()
        if not plan.validation_sha256 or not hmac.compare_digest(validation_hash, plan.validation_sha256):
            raise ValueError("validation script hash mismatch")


def validate_target_host(value: str) -> str:
    target = str(value or "").strip()
    if not _TARGET_HOST.fullmatch(target):
        raise ValueError("target host contains unsupported characters")
    return target


def validate_server_whitelist(target_host: str, allowed_hosts: set[str] | None = None) -> None:
    trusted = AUTOMATION_ALLOWED_HOSTS if allowed_hosts is None else set(allowed_hosts)
    if not trusted:
        raise ValueError("server automation whitelist is not configured")
    if target_host not in trusted:
        raise ValueError("target host is not in the server automation whitelist")


def validate_maintenance_window(plan, now: datetime | None = None) -> None:
    now = _as_utc(now or utcnow())
    start = _as_utc(plan.maintenance_window_start) if plan.maintenance_window_start else None
    end = _as_utc(plan.maintenance_window_end) if plan.maintenance_window_end else None
    if start and end and end <= start:
        raise ValueError("maintenance window end must be later than start")
    if start and now < start:
        raise ValueError("maintenance window has not started")
    if end and now > end:
        raise ValueError("maintenance window has ended")


def bounded_redacted_output(value: str | None) -> str:
    redacted = redact_output(value)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURED_OUTPUT_BYTES:
        return redacted
    clipped = encoded[:MAX_CAPTURED_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return f"{clipped}\n[output truncated at {MAX_CAPTURED_OUTPUT_BYTES} bytes]"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def redact_output(value: str | None) -> str:
    value = value or ""
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1***", value)
    return _AUTHORIZATION.sub(r"\1***", value)
