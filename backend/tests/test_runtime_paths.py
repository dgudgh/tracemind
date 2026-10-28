from __future__ import annotations

from pathlib import Path

from app.runtime.paths import _versioned_release_runtime_root


def test_windows_style_versioned_release_uses_shared_runtime(tmp_path: Path) -> None:
    install_root = tmp_path / "TraceMind"
    executable = install_root / "releases" / "1.2.0-abcd" / "TraceMindAI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    (install_root / "active-release.json").write_text("{}", encoding="utf-8")

    assert _versioned_release_runtime_root(executable) == install_root / "shared"


def test_macos_bundle_versioned_release_uses_shared_runtime(tmp_path: Path) -> None:
    install_root = tmp_path / "TraceMind"
    executable = (
        install_root
        / "releases"
        / "1.2.0-abcd"
        / "TraceMind AI.app"
        / "Contents"
        / "MacOS"
        / "TraceMindAI"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    (install_root / "active-release.json").write_text("{}", encoding="utf-8")

    assert _versioned_release_runtime_root(executable) == install_root / "shared"


def test_unmanaged_binary_does_not_change_runtime_root(tmp_path: Path) -> None:
    executable = tmp_path / "TraceMindAI.exe"
    executable.write_bytes(b"exe")
    assert _versioned_release_runtime_root(executable) is None
