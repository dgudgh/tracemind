"""Build and verify immutable, fixture-only signed Provider identities."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import platform
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.runtime.engine_packs.contract import build_payload_inventory, scan_payload_directory
from app.runtime.engine_packs.trust import TrustedEngineKey, canonical_manifest_bytes, verify_engine_manifest_signature

ROOT = Path(__file__).resolve().parents[4]
SDK_ROOT = Path(__file__).resolve().parents[1] / "engine_sdk"
ENTRYPOINT = "python.sdk.fixture.v1"
LAUNCHER = ROOT / "scripts/fixture_sdk_launcher.py"


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def describe_context():
    sdk = {p.name: sha(p.read_bytes()) for p in sorted(SDK_ROOT.glob("*.py"))}
    host_root = Path(__file__).parent
    host = {p.name: sha(p.read_bytes()) for p in sorted(host_root.glob("*.py"))}
    return {"os": sys.platform, "cpu": platform.machine(), "python": platform.python_version(),
            "python_abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
            "interpreter_digest": sha(Path(sys.executable).resolve().read_bytes()),
            "launcher_digest": sha(canonical_manifest_bytes({
                "loader": sha((SDK_ROOT / "launcher.py").read_bytes()),
                "entry": sha(LAUNCHER.read_bytes())})),
            "sdk_digest": sha(canonical_manifest_bytes(sdk)),
            "host_digest": sha(canonical_manifest_bytes(host)), "driver": "none/fake"}


def runtime_digest():
    context = describe_context()
    return sha(canonical_manifest_bytes({k: context[k] for k in
        ("os", "cpu", "python", "python_abi", "interpreter_digest", "launcher_digest", "sdk_digest")}))


@dataclass(frozen=True)
class FixturePackage:
    version: str
    digest: str
    source: Path
    manifest: dict
    signature: dict
    trusted_keys: tuple
    payload_digest: str = ""
    runtime_digest: str = ""

    def verify(self):
        verified = verify_engine_manifest_signature(self.manifest, self.signature,
            trusted_keys=self.trusted_keys, expected_namespace="tmengine.test", expected_channel="test")
        expected = sha(canonical_manifest_bytes(self.manifest))
        if self.digest != expected or self.version != self.manifest.get("version"):
            raise ValueError("package_identity_mismatch")
        inventory = build_payload_inventory(Path(self.source), self.manifest)
        payload = inventory["self_payload_digest"]
        runtime = self.manifest.get("runtime_digest", runtime_digest())
        if self.payload_digest and self.payload_digest != payload:
            raise ValueError("payload_identity_mismatch")
        if self.runtime_digest and self.runtime_digest != runtime:
            raise ValueError("runtime_identity_mismatch")
        return {"engine_id": self.manifest["engine_id"], "provider_version": self.version,
                "package_digest": expected, "manifest_sha256": verified.manifest_sha256,
                "payload_digest": payload, "runtime_digest": runtime,
                "sdk_version": self.manifest.get("sdk_version", "1.0")}


def build_fixture_package(root, version="1.0.0", *, sdk_version="1.0", manifest_updates=None):
    if version not in {"1.0.0", "1.0.1"}:
        raise ValueError("fixture_version_unsupported")
    root = Path(root).resolve()
    source = root / ("pkg-" + version)
    source.mkdir(parents=True, mode=0o700, exist_ok=False)
    fixtures = ROOT / "backend/tests/fixtures/engine_providers"
    name = "fake_provider.py" if version == "1.0.0" else "fake_provider_v2.py"
    shutil.copyfile(fixtures / name, source / "fake_provider.py")
    sdk = source / "sdk_runtime"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("")
    for filename in ("wire.py", "contracts.py", "provider.py", "broker_client.py", "profiles.py"):
        shutil.copyfile(SDK_ROOT / filename, sdk / filename)
    components = [{"path": p.relative_to(source).as_posix(), "version": version,
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "license": "fixture-self-built",
        "redistribution_status": "not-required"} for p in sorted(source.rglob("*.py"))]
    (source / "SBOM.json").write_bytes(canonical_manifest_bytes({"schema": "tmengine.sbom/v1", "components": components}))
    private = Ed25519PrivateKey.generate()
    key = TrustedEngineKey.create(private.public_key().public_bytes_raw(), namespace="tmengine.test",
        channel="test", publisher="fixture", engine_scopes=("test.fake",), fixture_only=True)
    files = scan_payload_directory(source)
    context = describe_context()
    manifest = {"format": 1, "package_kind": "tmengine", "package_id": "fixture", "version": version,
        "namespace": "tmengine.test", "channel": "test", "purpose": "tmengine.manifest-signing.v1",
        "publisher": "fixture", "engine_id": "test.fake", "signing_key_id": key.key_id,
        "entrypoint_id": ENTRYPOINT, "sdk_version": sdk_version, "sdk_versions": ["0.9", "1.0"],
        "rpc_version": "engine.rpc/v1", "task_schema": "engine.task/v1", "result_schema": "engine.result/v1",
        "driver": "none/fake", "os": context["os"], "cpu": context["cpu"], "python_abi": context["python_abi"],
        "runtime_digest": runtime_digest(), "capabilities": ["test.read", "test.effect"],
        "limits": {"max_inflight": 8, "max_connections": 32, "max_output_bytes": 64*1024*1024},
        "package_file_inventory": files, "whole_package_signed_files": [x["path"] for x in files],
        "file_classification": {x["path"]: "self-built" for x in files}}
    if manifest_updates:
        manifest.update(manifest_updates)
    signature = {"algorithm": "ed25519", "key_id": key.key_id,
                 "value": base64.b64encode(private.sign(canonical_manifest_bytes(manifest))).decode()}
    package = FixturePackage(version, sha(canonical_manifest_bytes(manifest)), source, manifest, signature, (key,),
        build_payload_inventory(source, manifest)["self_payload_digest"], manifest["runtime_digest"])
    package.verify()
    return package


def save_package(package, path):
    package.verify()
    path = Path(path)
    value = {"version": package.version, "digest": package.digest, "source": str(Path(package.source).resolve()),
        "manifest": package.manifest, "signature": package.signature, "payload_digest": package.payload_digest,
        "runtime_digest": package.runtime_digest, "trusted_keys": [{"raw": k.raw_public_key.hex(),
            "namespace": k.namespace, "channel": k.channel, "publisher": k.publisher,
            "engine_scopes": list(k.engine_scopes)} for k in package.trusted_keys]}
    from app.runtime.engine_sdk.wire import canonical
    path.write_bytes(canonical(value))
    os.chmod(path, 0o600)
    return path


def load_package(path, expected_digest=None):
    from app.runtime.engine_sdk.wire import strict_json
    value = strict_json(Path(path).read_bytes())
    if expected_digest is not None and value.get("digest") != expected_digest:
        raise ValueError("package_identity_mismatch")
    keys = tuple(TrustedEngineKey.create(bytes.fromhex(k["raw"]), namespace=k["namespace"], channel=k["channel"],
        publisher=k["publisher"], engine_scopes=tuple(k["engine_scopes"]), fixture_only=True) for k in value["trusted_keys"])
    package = FixturePackage(value["version"], value["digest"], Path(value["source"]), value["manifest"],
        value["signature"], keys, value.get("payload_digest", ""), value.get("runtime_digest", ""))
    package.verify()
    return package
