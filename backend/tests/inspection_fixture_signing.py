"""Temporary real signing keys shared by standalone inspection test fixtures."""
from pathlib import Path

from app.core import jwt_keys
from app.core.config import settings


def isolate_report_signing(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    configured = settings.JWT_PRIVATE_KEY_PATH.strip()
    candidate = Path(configured).expanduser() if configured else None
    # Preserve an enclosing fixture's temporary key, including fault-injection targets.
    if candidate is not None and candidate.is_absolute() and (
            candidate.is_relative_to(tmp_path) or candidate.is_relative_to(root)):
        path = candidate.resolve()
        assert path.is_relative_to(root)
    else:
        path = root / "inspection-signing-key.pem"
        monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(path))
    assert settings.jwt_private_key_path == path
    key = jwt_keys.signing_key()
    assert key.key_size >= 2048 and path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    return path
