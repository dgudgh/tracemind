"""Explicit release-maintenance entrypoint; never performs a database backup.

From the release root: python scripts/migrate_schema.py check
After verified backup and quiescence: python scripts/migrate_schema.py apply
    --backup-verified --service-quiesced
Retained legacy evidence: python scripts/migrate_schema.py inspection-results-check
Missing-only repair after verified backup and quiescence: inspection-results-reconcile
    --backup-verified --service-quiesced --expected-snapshot-hash <fresh-check-hash>
The database URL is read only from the named process environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=("check", "status", "contract", "apply", "inspection-results-check", "inspection-results-reconcile"), default="check"
    )
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--lock-timeout", type=float, default=30)
    parser.add_argument("--expected-snapshot-hash", help="SHA-256 from inspection-results-check; required for reconciliation")
    parser.add_argument(
        "--backup-verified",
        action="store_true",
        help="attest a verified pre-upgrade backup exists",
    )
    parser.add_argument(
        "--service-quiesced",
        action="store_true",
        help="attest all application writers are stopped",
    )
    args = parser.parse_args(argv)
    url = os.environ.get(args.database_url_env, "").strip()
    if not url:
        parser.error(
            f"environment variable {args.database_url_env} must contain the target database URL"
        )

    # The file entrypoint resolves the selected target before importing app settings.
    os.environ["DATABASE_URL"] = url
    if __package__ in {None, ""}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.core.database import initialize_schema
    from app.core.database_options import (
        database_engine_options,
        database_target_summary,
        redact_database_error,
    )
    from app.migrations.policy import SchemaUpgradeRequired
    from app.migrations.runner import migration_status
    from app.migrations.contract import migration_status as contract_status
    from app.migrations.inspection_result_integrity import (
        InspectionResultIntegrityError, inspect_legacy_results, reconcile_missing_legacy_results,
    )
    from app.migrations.locking import schema_lock
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    engine = None
    try:
        if (
            args.command == "apply"
            and make_url(url).get_backend_name() != "sqlite"
            and (not args.backup_verified or not args.service_quiesced)
        ):
            parser.error(
                "external database apply requires --backup-verified and --service-quiesced"
            )
        if args.command == "inspection-results-reconcile":
            if not args.backup_verified or not args.service_quiesced:
                parser.error("inspection reconciliation requires --backup-verified and --service-quiesced for every database")
            if (not args.expected_snapshot_hash or len(args.expected_snapshot_hash) != 64
                    or any(char not in "0123456789abcdef" for char in args.expected_snapshot_hash)):
                parser.error("inspection reconciliation requires a lowercase --expected-snapshot-hash from a fresh check")
        engine = create_engine(url, **database_engine_options(url, sql_echo=False))
        if args.command == "contract":
            contract = contract_status(engine)
            print(json.dumps({"ok": contract["ready"], "command": args.command,
                              "target": database_target_summary(url), "contract": contract}))
            return 0 if contract["ready"] else 2
        if args.command == "inspection-results-check":
            with engine.connect() as connection:
                report = inspect_legacy_results(connection)
            okay = report["state"] in {"absent", "consistent"}
            print(json.dumps({"ok": okay, "command": args.command, "target": database_target_summary(url),
                              "inspection_results": report, "code": None if okay else InspectionResultIntegrityError.code}))
            return 0 if okay else 3
        if args.command == "inspection-results-reconcile":
            with schema_lock(engine, timeout=args.lock_timeout) as connection:
                repaired = reconcile_missing_legacy_results(connection, expected_snapshot_hash=args.expected_snapshot_hash)
            print(json.dumps({"ok": True, "command": args.command, "target": database_target_summary(url),
                              "reconciliation": repaired}))
            return 0
        applied = []
        if args.command != "status":
            applied = initialize_schema(
                engine,
                allow_upgrade=args.command == "apply",
                lock_timeout=args.lock_timeout,
            )
        with engine.connect() as connection:
            inspection_results = inspect_legacy_results(connection)
        if inspection_results["state"] not in {"absent", "consistent"}:
            raise InspectionResultIntegrityError(inspection_results)
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": args.command,
                    "target": database_target_summary(url),
                    "applied": applied,
                    "revisions": migration_status(engine),
                    "inspection_results": inspection_results,
                }
            )
        )
        return 0
    except InspectionResultIntegrityError as exc:
        print(json.dumps({"ok": False, "command": args.command, "code": exc.code,
                          "inspection_results": exc.report, "error": str(exc)}))
        return 3
    except SchemaUpgradeRequired as exc:
        print(json.dumps({"ok": False, "upgrade_required": True, "error": str(exc)}))
        return 2
    except Exception as exc:  # noqa: BLE001 - sanitize failures at the operator CLI boundary
        print(
            json.dumps({"ok": False, "error": redact_database_error(exc)}),
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
