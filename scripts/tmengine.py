#!/usr/bin/env python3
"""Offline .tmengine build/verify and owner-only lab repository commands."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.engine_packs.archive import ArchiveError, _regular_open, build_archive, open_verified_archive  # noqa: E402
from app.runtime.engine_packs.contract import EnginePackContractError  # noqa: E402
from app.runtime.engine_packs.repository import LabRepository  # noqa: E402
from app.runtime.engine_packs.trust import TrustedEngineKey, TrustError, parse_manifest_json  # noqa: E402


def read_json(path, limit=4 * 1024 * 1024):
    with _regular_open(Path(path)) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ArchiveError("control_file_limit_exceeded")
    return parse_manifest_json(data)


def load_lab_policy(path):
    if os.name != "posix":
        raise ArchiveError("lab_policy_os_unsupported")
    path = Path(path)
    with _regular_open(path) as source:
        st = os.fstat(source.fileno())
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise ArchiveError("trust_policy_owner_mismatch")
        data = source.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ArchiveError("control_file_limit_exceeded")
    value = parse_manifest_json(data)
    if (set(value) != {"schema", "keys"} or value["schema"] != "tmengine.lab-trust/v1"
            or not isinstance(value["keys"], list) or not value["keys"] or len(value["keys"]) > 32):
        raise ArchiveError("lab_trust_policy_invalid")
    keys = []
    for item in value["keys"]:
        if (not isinstance(item, dict) or set(item) != {"public_key_hex", "publisher", "engine_scopes"}
                or not isinstance(item["public_key_hex"], str) or len(item["public_key_hex"]) != 64
                or not isinstance(item["engine_scopes"], list)):
            raise ArchiveError("lab_trust_policy_invalid")
        try:
            raw = bytes.fromhex(item["public_key_hex"])
        except ValueError as exc:
            raise ArchiveError("lab_trust_policy_invalid") from exc
        keys.append(TrustedEngineKey.create(raw, namespace="tmengine.test", channel="test",
            publisher=item["publisher"], engine_scopes=item["engine_scopes"], fixture_only=True))
    if len({x.key_id for x in keys}) != len(keys):
        raise ArchiveError("lab_trust_policy_duplicate_key")
    return tuple(keys)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-policy", required=True, help="explicit external public lab trust policy")
    parser.add_argument("--repository", help="absolute new/private lab repository root")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    for name in ("source", "manifest", "signature", "output"):
        build.add_argument("--" + name, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--expected-archive-sha256")
    install = sub.add_parser("install")
    install.add_argument("--import-name", required=True)
    install.add_argument("--request-id", required=True)
    install.add_argument("--expected-archive-sha256")
    for name in ("list", "recover", "audit"):
        sub.add_parser(name)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--digest", required=True)
    status = sub.add_parser("status")
    status.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        keys = load_lab_policy(args.trust_policy)
        if args.command == "build":
            result = build_archive(Path(args.source), read_json(args.manifest), read_json(args.signature),
                Path(args.output), trusted_keys=keys, expected_namespace="tmengine.test", expected_channel="test")
        elif args.command == "verify":
            with open_verified_archive(Path(args.archive), trusted_keys=keys, expected_namespace="tmengine.test",
                    expected_channel="test", expected_archive_sha256=args.expected_archive_sha256) as package:
                result = {"package_digest": package.package_digest, "archive_sha256": package.archive_sha256,
                          "payload_digest": package.payload_digest, "engine_id": package.manifest["engine_id"],
                          "version": package.manifest["version"]}
        else:
            if not args.repository:
                raise ArchiveError("repository_root_required")
            repository = LabRepository(Path(args.repository), trusted_keys=keys)
            if args.command == "install":
                result = repository.install(args.import_name, args.request_id,
                    expected_archive_sha256=args.expected_archive_sha256)
            elif args.command == "inspect":
                result = repository.inspect(args.digest)
            elif args.command == "status":
                result = repository.status(args.request_id)
            else:
                result = getattr(repository, args.command)()
            if args.command == "recover" and any(x["state"] == "blocked" for x in result):
                print(json.dumps({"ok": False, "lab_only": True, "code": "repository_recovery_blocked", "result": result}))
                return 2
        print(json.dumps({"ok": True, "lab_only": True, "activated": False, "result": result}, ensure_ascii=False))
        return 0
    except (ArchiveError, EnginePackContractError, TrustError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "lab_only": True, "code": getattr(exc, "code", "package_io_error")}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
