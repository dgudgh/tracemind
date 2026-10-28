"""Offline public-metadata verifier; never handles signing private keys."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.runtime.engine_packs.trust_metadata import MAX_METADATA_BYTES, TrustStore  # noqa: E402
from app.runtime.engine_packs.trust import TrustError, parse_manifest_json  # noqa: E402


def _read(path):
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_METADATA_BYTES:
        raise TrustError("metadata_file_invalid")
    return parse_manifest_json(source.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--namespace", choices=("tmengine.test", "tmengine.production"), required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap")
    boot.add_argument("--root", required=True)
    boot.add_argument("--root-sha256", required=True, help="Out-of-band verified sha256:... of canonical root envelope")
    boot.add_argument("--fixture-only", action="store_true")
    update = commands.add_parser("update")
    update.add_argument("--bundle", required=True)
    auth = commands.add_parser("authorize")
    auth.add_argument("--manifest", required=True)
    auth.add_argument("--digest", required=True)
    auth.add_argument("--rollback", action="store_true")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    store = TrustStore(args.state_dir, args.namespace)
    try:
        if args.command == "bootstrap":
            store.bootstrap(_read(args.root), expected_root_sha256=args.root_sha256,
                            fixture_only=args.fixture_only)
            result = store.status()
        elif args.command == "update":
            result = store.update(_read(args.bundle))
        elif args.command == "authorize":
            key = store.authorize_package(_read(args.manifest), args.digest, rollback=args.rollback)
            result = {"authorized_key_id": key.key_id, "namespace": key.namespace,
                      "publisher": key.publisher, "engine_scopes": key.engine_scopes,
                      "fixture_only": key.fixture_only,
                      "warning": "Release-key authorization only; detached package signature and bytes still require verification"}
        else:
            result = store.status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (TrustError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "trust_io_error")}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
