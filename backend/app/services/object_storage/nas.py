from __future__ import annotations

from pathlib import Path
import hashlib
import shutil

from app.services.object_storage.base import ObjectStorageAdapter, validate_object_key


class NasStorageAdapter(ObjectStorageAdapter):
    def _root(self) -> Path:
        root = Path(self.config.root_path or "").expanduser()
        if not root.is_absolute():
            raise ValueError("NAS 根目录必须是绝对路径")
        return root.resolve()

    def resolve_path(self, object_key: str) -> Path:
        key = validate_object_key(object_key)
        root = self._root()
        candidate = (root / key).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("对象键超出 NAS 根目录")
        return candidate

    def object_location(self, object_key: str) -> str:
        return str(self.resolve_path(object_key))

    def read_bytes(self, object_key: str) -> bytes:
        return self.resolve_path(object_key).read_bytes()

    def write_file(
        self,
        source_path: Path,
        object_key: str,
        *,
        sha256: str,
        legal_hold: bool = False,
    ) -> dict:
        source = Path(source_path).resolve()
        target = self.resolve_path(object_key)
        hold_path = self._hold_path(object_key)
        was_held = hold_path.is_file()
        if was_held and target.is_file() and _sha256_file(target) != sha256:
            raise PermissionError("对象处于 legal hold，禁止覆盖")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            temporary = target.with_name(f".{target.name}.uploading")
            shutil.copyfile(source, temporary)
            temporary.replace(target)
        if legal_hold:
            self.set_legal_hold(object_key, True)
        elif not was_held:
            self.set_legal_hold(object_key, False)
        return {"size_bytes": target.stat().st_size, "sha256": _sha256_file(target)}

    def download_file(self, object_key: str, target_path: Path) -> Path:
        source = self.resolve_path(object_key)
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def stat_object(self, object_key: str) -> dict:
        path = self.resolve_path(object_key)
        return {
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "legal_hold": self._hold_path(object_key).is_file(),
        }

    def delete_object(self, object_key: str) -> None:
        if self._hold_path(object_key).is_file():
            raise PermissionError("对象处于 legal hold，禁止删除")
        self.resolve_path(object_key).unlink(missing_ok=True)

    def set_legal_hold(self, object_key: str, enabled: bool) -> None:
        marker = self._hold_path(object_key)
        if enabled:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ON\n", encoding="ascii")
        else:
            marker.unlink(missing_ok=True)

    def _hold_path(self, object_key: str) -> Path:
        target = self.resolve_path(object_key)
        return target.with_name(f".{target.name}.legal-hold")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
