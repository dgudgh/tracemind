from __future__ import annotations

from pathlib import PurePosixPath
from pathlib import Path


def validate_object_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or "\\" in key:
        raise ValueError("对象键必须是非空 POSIX 相对路径")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("对象键不允许包含路径穿越")
    return path.as_posix()


class ObjectStorageAdapter:
    def __init__(self, config):
        self.config = config

    def object_location(self, object_key: str) -> str:
        raise NotImplementedError

    def read_bytes(self, object_key: str) -> bytes:
        raise NotImplementedError

    def write_file(
        self,
        source_path: Path,
        object_key: str,
        *,
        sha256: str,
        legal_hold: bool = False,
    ) -> dict:
        raise NotImplementedError

    def download_file(self, object_key: str, target_path: Path) -> Path:
        raise NotImplementedError

    def stat_object(self, object_key: str) -> dict:
        raise NotImplementedError

    def delete_object(self, object_key: str) -> None:
        raise NotImplementedError

    def set_legal_hold(self, object_key: str, enabled: bool) -> None:
        raise NotImplementedError
