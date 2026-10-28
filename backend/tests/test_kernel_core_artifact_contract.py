from hashlib import sha256
from pathlib import Path

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]
CORE_OBJECT = ROOT / "agent/cmd/tracemind-ebpf-loader/bpf/tracemind_core_bpfel.o"
CORE_MANIFEST = CORE_OBJECT.with_suffix(CORE_OBJECT.suffix + ".sha256")


def test_embedded_core_object_matches_manifest_and_backend_allowlist():
    digest = sha256(CORE_OBJECT.read_bytes()).hexdigest()
    manifest_digest = CORE_MANIFEST.read_text(encoding="utf-8").strip()
    default_allowlist = Settings.model_fields["KERNEL_CORE_OBJECT_SHA256_ALLOWLIST"].default

    assert digest == manifest_digest
    assert digest in {value.strip().lower() for value in default_allowlist.split(",")}
