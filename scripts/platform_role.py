"""Dedicated managed process entrypoint; legacy continues through existing run.py."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from _bootstrap import add_backend_to_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("api", "scheduler", "worker", "migrator"))
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--generation", default="")
    parser.add_argument("--artifact-digest", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-identity", type=Path,
                        help="JSON file with exact pinned core and Provider identity")
    parser.add_argument("--worker-status-file", type=Path,
                        help="owner-local live worker identity/heartbeat file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-verified", action="store_true")
    parser.add_argument("--service-quiesced", action="store_true")
    args = parser.parse_args(argv)
    url = os.environ.get(args.database_url_env, "").strip()
    if not url:
        parser.error(f"{args.database_url_env} must be supplied in the process environment")
    if args.role == "api" and (not args.generation or not 0 < args.port <= 65535):
        parser.error("api requires --generation and a nonzero --port")
    if args.role == "migrator" and not args.apply:
        parser.error("migrator requires explicit --apply")
    if args.role == "migrator" and not (args.backup_verified and args.service_quiesced):
        parser.error("migrator requires --backup-verified and --service-quiesced")
    if args.role != "migrator" and args.apply:
        parser.error("only the migrator may apply schema changes")
    if args.role == "worker" and not args.worker_identity:
        parser.error("managed worker requires an exact --worker-identity")
    os.environ["DATABASE_URL"] = url
    os.environ["TRACEMIND_PROCESS_ROLE"] = args.role
    os.environ["TRACEMIND_MIGRATION_RECEIPT"] = str(Path(args.receipt).resolve())
    if args.generation:
        os.environ["TRACEMIND_PLATFORM_GENERATION"] = args.generation
    if args.artifact_digest:
        os.environ["TRACEMIND_PLATFORM_ARTIFACT_DIGEST"] = args.artifact_digest
    add_backend_to_path()
    from app.core.database import engine, initialize_schema
    from app.runtime.process_roles import (platform_readiness, receipt_path,
                                           write_migration_receipt)

    if args.role == "migrator":
        from app.core.database import verify_schema_ready

        applied = initialize_schema(engine, allow_upgrade=True)
        contract = verify_schema_ready(engine)
        receipt = write_migration_receipt(receipt_path(), contract, url)
        print(json.dumps({"status": "ready", "applied": applied, "receipt": receipt}, sort_keys=True))
        return 0

    # Every managed role verifies the actual schema and the migrator's durable
    # receipt before opening any listener, starting a scheduler, or claiming work.
    platform_readiness(engine, url)
    if args.role == "api":
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")
        return 0

    stop = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        if args.role == "scheduler":
            from app.tasks.master_scheduler import start_master_scheduler, stop_master_scheduler

            if start_master_scheduler() is None:
                raise RuntimeError("scheduler could not start")
            try:
                stop.wait()
            finally:
                stop_master_scheduler()
            return 0
        from app.tasks.plan_worker import run_worker
        from app.core.database import SessionLocal
        from app.runtime.execution_identity import validate_identity

        worker_identity = dict(validate_identity(json.loads(args.worker_identity.read_text(encoding="utf-8"))))
        status_thread = None
        status_nonce = os.urandom(16).hex()
        status_path = args.worker_status_file.resolve() if args.worker_status_file else None
        if status_path:
            import psutil
            from app.runtime.release_checkpoint import durable_json

            created = psutil.Process(os.getpid()).create_time()
            status_path.parent.mkdir(parents=True, exist_ok=True)

            def heartbeat():
                while not stop.is_set():
                    durable_json(status_path, {"pid": os.getpid(), "process_created_at": created,
                                               "heartbeat_at": time.time(), "identity": worker_identity,
                                               "nonce": status_nonce})
                    if stop.wait(0.5):
                        break

            status_thread = threading.Thread(target=heartbeat, name="managed-worker-status", daemon=True)
            status_thread.start()
        try:
            return run_worker(SessionLocal, worker_id=f"managed:{args.generation}:{os.getpid()}",
                              worker_identity=worker_identity, once=args.once, stop=stop)
        finally:
            stop.set()
            if status_thread:
                status_thread.join()
            if status_path and status_path.exists():
                try:
                    if json.loads(status_path.read_text()).get("nonce") == status_nonce:
                        status_path.unlink()
                except (OSError, ValueError):
                    pass
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
