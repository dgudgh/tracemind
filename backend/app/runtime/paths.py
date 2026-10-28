import os
import sys
from pathlib import Path


APP_SUPPORT_NAME = "TraceMindAI"
ACTIVE_RELEASE_FILENAME = "active-release.json"


def _versioned_release_runtime_root(executable: Path) -> Path | None:
    for candidate in executable.parents:
        releases_dir = candidate.parent
        install_root = releases_dir.parent
        if releases_dir.name != "releases":
            continue
        if (install_root / ACTIVE_RELEASE_FILENAME).is_file():
            return install_root / "shared"
    return None


def frozen_runtime_root() -> Path:
    override = os.environ.get("TRACEMIND_RUNTIME_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    executable_dir = Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        release_runtime = _versioned_release_runtime_root(Path(sys.executable).resolve())
        if release_runtime is not None:
            return release_runtime
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_SUPPORT_NAME
    return executable_dir


def ensure_frozen_runtime_root() -> Path:
    root = frozen_runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    return root
