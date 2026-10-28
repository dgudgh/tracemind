"""Read-only release centre.

`app/runtime/release_manager.py` implements the whole upgrade transaction -
signature verification, pre-upgrade backup, migration preflight, atomic
activation and rollback - but nothing ever surfaced it, so operators could not
tell which release was active or whether a transaction had died halfway.

Only the read side is exposed deliberately. Installing a package means executing
code the request points at; exposing that over HTTP would turn a signed local
release process into a remote code execution endpoint, and the signing keys live
on the operator's machine, not in the web process. Mutating operations stay on
the install/upgrade CLI, which already holds the lock and the keys.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import APIRouter

from app.runtime.release_manager import ACTIVE_FILENAME, MANIFEST_FILENAME, ReleaseManager

router = APIRouter(prefix="/release-center", tags=["release-center"])

MAX_HISTORY_ITEMS = 50


def _install_root() -> Path | None:
    """Locate a versioned install (one containing `releases/` and the active marker).

    Returns None for a source checkout or a non-versioned deployment, which is a
    normal state rather than an error - the UI then says so instead of inventing
    an empty release history.
    """
    override = os.environ.get("TRACEMIND_INSTALL_ROOT", "").strip()
    candidates: list[Path] = [Path(override).expanduser()] if override else []
    if getattr(sys, "frozen", False):
        candidates.extend(Path(sys.executable).resolve().parents)
    for candidate in candidates:
        if (candidate / "releases").is_dir() and (candidate / ACTIVE_FILENAME).is_file():
            return candidate
    return None


def _release_ids(releases_dir: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in releases_dir.iterdir() if entry.is_dir())
    except OSError:
        return []


@router.get("/status")
def release_center_status():
    """Active release, recent transitions and a possibly unfinished transaction."""
    root = _install_root()
    if root is None:
        return {
            "available": False,
            "reason": "not_a_versioned_install",
            "detail": "当前运行不是 releases/<id> 版本化安装，没有可展示的发布记录。",
            "mutation_channel": "operator_cli",
        }

    manager = ReleaseManager(root)
    state = manager.state()
    history = list(state.get("history") or [])
    pending = manager.checkpoints.current()
    active = manager.active()
    return {
        "available": True,
        "install_root": str(root),
        "active": active,
        "releases": _release_ids(manager.releases_dir),
        "history": history[-MAX_HISTORY_ITEMS:],
        # An unfinished checkpoint blocks every later mutation, so it has to be
        # visible rather than discoverable only when the next upgrade refuses.
        "pending_transaction": pending,
        "blocked": pending is not None,
        "mutation_channel": "operator_cli",
    }


@router.get("/releases/{release_id}/manifest")
def release_manifest(release_id: str):
    """The manifest a release was activated with, for post-incident comparison."""
    from fastapi import HTTPException

    root = _install_root()
    if root is None:
        raise HTTPException(status_code=404, detail="not_a_versioned_install")
    manager = ReleaseManager(root)
    path = (manager.releases_dir / release_id).resolve()
    if not path.is_dir() or manager.releases_dir.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="release_not_found")
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="manifest_not_found")
    import json

    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"manifest_unreadable: {exc}") from exc
