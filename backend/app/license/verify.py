import base64
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("app.license.verify")
_LICENSE_CACHE_TTL_SECONDS = float(os.getenv("LICENSE_CACHE_TTL_SECONDS", "30"))
_LICENSE_INFO_CACHE: dict[str, object] = {"expires_at": 0.0, "value": None}
_SYSTEM_COMMAND_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")

PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1Iy1pgls65Mm1aJoqK07
czmsSTFx0CE2+vgnkpz6ywDqqsQmuCgTGYxUH8ExxzSlMa6/wR6Yma4ZDfV0vkBU
C77K0ulKEcbQOtVO+FqoQnx18yp531oCB8KPD5zE86Q5tgRrXudbxBfW3liwcTUE
sxBqPl0D5uYy0QKOd1sQjPFLnu6Z87snirlV6XKD8hRZlRG3JJFf72372K90qGHH
wtphGs2InWDBcH/AugasZzXADbF6AdVST29VqZ1FS4ISzMWLz3EYTuUKo7bdQURr
zfF7n0YrS9QnHmdyoUdWwyoR5wUuS4LRTXV5oz6Xcxgf1CX5EsxDeG9secewKOsx
2wIDAQAB
-----END PUBLIC KEY-----
"""


def _resolve_executable(command: str) -> str:
    if not command or os.path.isabs(command):
        return command
    resolved = shutil.which(command)
    if resolved:
        return resolved
    candidates = [os.path.join(directory, command) for directory in _SYSTEM_COMMAND_DIRS]
    system_root = os.getenv("SystemRoot", "").strip()
    if system_root:
        candidates.append(os.path.join(system_root, "System32", command))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return command


def _resolved_args(args: list[str]) -> list[str]:
    if not args:
        return args
    return [_resolve_executable(args[0]), *args[1:]]


def _run_wmic(args: list[str]) -> str:
    try:
        result = subprocess.check_output(_resolved_args(args), stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        text = result.decode("gbk", errors="ignore").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[1] if len(lines) >= 2 else ""
    except Exception:
        return ""


def _run_text(args: list[str]) -> str:
    try:
        result = subprocess.check_output(
            _resolved_args(args),
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=3,
        )
        return result.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _get_machine_guid() -> str:
    if platform.system() != "Windows":
        return ""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return value
    except Exception:
        return ""


def _get_macos_platform_uuid() -> str:
    text = _run_text(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    match = re.search(r'"IOPlatformUUID"\s=\s"([^"]+)"', text)
    return match.group(1).strip() if match else ""


def _get_linux_machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _get_unix_mac_address() -> str:
    candidates: list[str] = []
    for command in (["ifconfig"], ["ip", "link"]):
        text = _run_text(command)
        if not text:
            continue
        candidates.extend(re.findall(r"(?:ether|link/ether)\s+([0-9a-fA-F:]{17})", text))
    normalized = sorted({
        value.lower()
        for value in candidates
        if value and value != "00:00:00:00:00:00" and not value.lower().startswith("ff:ff")
    })
    if normalized:
        return normalized[0]
    node = uuid.getnode()
    if node:
        return ":".join(f"{(node >> offset) & 0xff:02x}" for offset in range(40, -1, -8))
    return ""


def get_machine_fingerprint() -> dict:
    system_name = platform.system()
    if system_name == "Windows":
        return {
            "system": system_name,
            "cpu_id": _run_wmic(["wmic", "cpu", "get", "ProcessorId"]),
            "board_serial": _run_wmic(["wmic", "baseboard", "get", "SerialNumber"]),
            "bios_serial": _run_wmic(["wmic", "bios", "get", "SerialNumber"]),
            "disk_serial": _run_wmic(["wmic", "diskdrive", "get", "SerialNumber"]),
            "machine_guid": _get_machine_guid(),
            "platform_uuid": "",
            "machine_id": "",
            "mac_address": hex(uuid.getnode()),
        }
    platform_uuid = _get_macos_platform_uuid() if system_name == "Darwin" else ""
    machine_id = _get_linux_machine_id() if system_name == "Linux" else ""
    return {
        "system": system_name,
        "cpu_id": "",
        "board_serial": "",
        "bios_serial": "",
        "disk_serial": "",
        "machine_guid": "",
        "platform_uuid": platform_uuid,
        "machine_id": machine_id,
        "mac_address": _get_unix_mac_address(),
    }


def generate_machine_code() -> str:
    info = get_machine_fingerprint()
    if info.get("system") == "Windows":
        raw = "|".join([
            info.get("cpu_id", ""),
            info.get("board_serial", ""),
            info.get("bios_serial", ""),
            info.get("disk_serial", ""),
            info.get("machine_guid", ""),
            info.get("mac_address", ""),
        ])
    elif info.get("system") == "Darwin":
        raw = info.get("platform_uuid") or info.get("mac_address", "")
    elif info.get("system") == "Linux":
        raw = info.get("machine_id") or info.get("mac_address", "")
    else:
        raw = "|".join([
            info.get("system", ""),
            info.get("platform_uuid", ""),
            info.get("machine_id", ""),
            info.get("mac_address", ""),
        ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()


def _load_public_key():
    try:
        return serialization.load_pem_public_key(PUBLIC_KEY_PEM)
    except Exception:
        return None


def _verify_signature(public_key, data: bytes, signature: str) -> bool:
    try:
        public_key.verify(base64.b64decode(signature), data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def license_signature_issues(license_data: dict) -> str | None:
    """Return a reason string when the payload is not a vendor-signed license.

    The read path (_build_license_info) already refuses an unsigned or forged
    license. The write path has to apply the same check *before* it replaces the
    stored license, otherwise a rejected upload still destroys the working one.
    """
    if not isinstance(license_data, dict):
        return "授权内容必须是 JSON 对象"
    signature = license_data.get("signature")
    if not signature:
        return "no_signature"
    public_key = _load_public_key()
    if public_key is None:
        return "no_public_key"
    check_data = {k: v for k, v in license_data.items() if k != "signature"}
    data_bytes = json.dumps(check_data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if not _verify_signature(public_key, data_bytes, signature):
        return "signature_invalid"
    return None


def license_payload_issues(license_data: dict, *, current_machine_code: str | None = None) -> str | None:
    """Reason a payload cannot become the *active* license, or None when it can.

    Mirrors the read path (_build_license_info) rather than only checking the
    signature. Replacing the stored license is destructive, so anything the read
    path would reject must be refused before the stored row is touched -- a
    correctly signed but wrong-machine or expired license would otherwise disable
    a working installation just as effectively as a forged one.
    """
    issue = license_signature_issues(license_data)
    if issue:
        return issue
    expected = current_machine_code or generate_machine_code()
    if license_data.get("machine_code") != expected:
        return "machine_mismatch"
    try:
        if license_is_expired(license_data.get("expire_date")):
            return "expired"
    except Exception:
        return "expire_date_invalid"
    return None


def _get_license_from_db() -> dict | None:
    db = None
    try:
        from app.core.database import SessionLocal
        from app.models.license import License
        db = SessionLocal()
        lic = db.query(License).order_by(License.id.desc()).first()
        if lic:
            return {
                "machine_code": lic.machine_code,
                "customer": lic.customer,
                "product": lic.product,
                "expire_date": lic.expire_date,
                "features": json.loads(lic.features) if lic.features else [],
                "signature": lic.signature,
            }
    except Exception as exc:
        logger.debug("Load license from database failed, fallback to license file | error=%s", exc)
    finally:
        if db is not None:
            db.close()
    return None


def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        from app.runtime.paths import ensure_frozen_runtime_root

        return str(ensure_frozen_runtime_root())
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _get_license_from_file() -> dict | None:
    license_path = os.path.join(_get_app_dir(), "license.json")
    if not os.path.isfile(license_path):
        return None
    try:
        with open(license_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("Load license file failed | path=%s | error=%s", license_path, e)
    return None


def _fix_display_text(value: str) -> str:
    if not isinstance(value, str):
        return value
    try:
        fixed = value.encode("latin1").decode("utf-8")
        if fixed and fixed != value:
            return fixed
    except Exception:
        pass
    return value


def _display_or_fallback(value: str, fallback: str) -> str:
    text = _fix_display_text(value or "").strip()
    if not text or text.replace("?", "") == "" or "\ufffd" in text:
        return fallback
    return text


def clear_license_cache() -> None:
    _LICENSE_INFO_CACHE["expires_at"] = 0.0
    _LICENSE_INFO_CACHE["value"] = None


def license_is_expired(expire_date: str, now: datetime | None = None) -> bool:
    """Treat a date-only license as valid through the end of that date."""
    expires_on = datetime.strptime(expire_date, "%Y-%m-%d").date()
    return (now or datetime.now()).date() > expires_on


def _build_license_info() -> dict:
    current_machine_code = generate_machine_code()
    result = {"machine_code": current_machine_code}

    license_data = _get_license_from_db() or _get_license_from_file()
    if not license_data:
        result["valid"] = False
        result["reason"] = "license_not_found"
        return result

    result["customer"] = _display_or_fallback(license_data.get("customer", ""), "未命名客户")
    result["product"] = _display_or_fallback(license_data.get("product", ""), "TraceMind AI 智能运维平台")
    result["expire_date"] = license_data.get("expire_date", "")
    result["features"] = license_data.get("features", [])

    signature = license_data.get("signature")
    if not signature:
        result["valid"] = False
        result["reason"] = "no_signature"
        return result

    check_data = {k: v for k, v in license_data.items() if k != "signature"}
    data_bytes = json.dumps(check_data, sort_keys=True, ensure_ascii=False).encode("utf-8")

    public_key = _load_public_key()
    if public_key is None:
        result["valid"] = False
        result["reason"] = "no_public_key"
        return result

    if not _verify_signature(public_key, data_bytes, signature):
        result["valid"] = False
        result["reason"] = "signature_invalid"
        return result

    if license_data.get("machine_code") != current_machine_code:
        result["valid"] = False
        result["reason"] = "machine_mismatch"
        return result

    expire_date = license_data.get("expire_date")
    try:
        if license_is_expired(expire_date):
            result["valid"] = False
            result["reason"] = "expired"
            return result
    except Exception:
        result["valid"] = False
        result["reason"] = "expire_date_invalid"
        return result

    result["valid"] = True
    result["reason"] = ""
    return result


def get_license_info(force_refresh: bool = False) -> dict:
    now = time.monotonic()
    cached = _LICENSE_INFO_CACHE.get("value")
    if not force_refresh and isinstance(cached, dict) and now < float(_LICENSE_INFO_CACHE.get("expires_at") or 0):
        return dict(cached)

    result = _build_license_info()
    _LICENSE_INFO_CACHE["value"] = dict(result)
    _LICENSE_INFO_CACHE["expires_at"] = now + _LICENSE_CACHE_TTL_SECONDS
    return result


def save_license_to_db(license_data: dict) -> dict:
    # Reject before the destructive replace: a forged, wrong-machine or expired
    # upload must not be able to overwrite (and thereby disable) a valid license.
    issue = license_payload_issues(license_data)
    if issue:
        logger.warning("拒绝写入未通过校验的授权 | reason=%s", issue)
        return {"ok": False, "message": f"授权校验失败：{issue}"}

    db = None
    try:
        from app.core.database import SessionLocal
        from app.models.license import License
        db = SessionLocal()
        db.query(License).delete()
        lic = License(
            machine_code=license_data["machine_code"],
            customer=license_data.get("customer", ""),
            product=license_data.get("product", ""),
            expire_date=license_data.get("expire_date", ""),
            features=json.dumps(license_data.get("features", []), ensure_ascii=False),
            signature=license_data.get("signature", ""),
        )
        db.add(lic)
        db.commit()
        clear_license_cache()
        return {"ok": True}
    except Exception as e:
        if db is not None:
            try:
                db.rollback()
            except Exception as rollback_error:
                logger.warning("Rollback license save failed | error=%s", rollback_error)
        return {"ok": False, "message": str(e)}
    finally:
        if db is not None:
            db.close()


def check_license() -> bool:
    info = get_license_info()
    if info["valid"]:
        logger.info("授权校验通过 | customer=%s | expire=%s", info.get("customer"), info.get("expire_date"))
        return True
    logger.warning("授权校验失败 | reason=%s | machine_code=%s", info.get("reason"), info.get("machine_code"))
    return False
