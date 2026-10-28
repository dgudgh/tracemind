#!/usr/bin/env python3
"""Explicit local lab engine pack management service and client."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.runtime.engine_packs.management import LabManagement, ManagementError, request, serve  # noqa: E402
from tmengine import load_lab_policy  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--mode", choices=("lab", "production"), required=True)
    server.add_argument("--repository", required=True)
    server.add_argument("--trust-policy", required=True)
    server.add_argument("--admin-policy", required=True)
    server.add_argument("--lab-capacity-budget")
    server.add_argument("--lab-capacity-profile")
    server.add_argument("--lab-capacity-candidate")
    server.add_argument("--lab-pause-at", choices=("activation_prepared", "activation_ready", "activation_committed",
                                                   "switch_ready", "switch_committed", "uninstall_removed"))
    server.add_argument("--lab-checkpoint-file")
    client = sub.add_parser("call")
    client.add_argument("--socket", required=True)
    client.add_argument("--server-uid", required=True, type=int)
    client.add_argument("--repository", help="expected fixed repository root; required for production endpoint")
    operations = client.add_subparsers(dest="method", required=True)
    for method in ("list", "status", "recover", "gc"):
        operations.add_parser(method)
    inspect = operations.add_parser("inspect"); inspect.add_argument("--digest", required=True)
    verify = operations.add_parser("verify"); verify.add_argument("--import-name", required=True)
    verify.add_argument("--archive-sha256", required=True)
    install = operations.add_parser("install"); install.add_argument("--import-name", required=True)
    install.add_argument("--archive-sha256", required=True); install.add_argument("--request-id", required=True)
    activate = operations.add_parser("initial-activate"); activate.add_argument("--digest", required=True)
    activate.add_argument("--request-id", required=True)
    rollback = operations.add_parser("initial-rollback"); rollback.add_argument("--request-id", required=True)
    for method in ("upgrade", "rollback"):
        switch = operations.add_parser(method); switch.add_argument("--digest", required=True)
        switch.add_argument("--expected-generation", required=True, type=int)
        switch.add_argument("--request-id", required=True)
    for method in ("disable", "uninstall"):
        action = operations.add_parser(method); action.add_argument("--digest", required=True)
        action.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.role == "call":
            values = vars(args)
            params = {key: values[key] for key in ("digest", "import_name", "archive_sha256", "request_id", "expected_generation") if key in values}
            response = request(args.socket, args.method, params, expected_server_uid=args.server_uid,
                               expected_repository_root=args.repository)
            print(json.dumps(response, ensure_ascii=False, sort_keys=True))
            return 0 if response.get("ok") is True else 2
        if args.mode != "lab":
            raise ManagementError("production_management_admission_missing")
        os.umask(0o077)
        keys = load_lab_policy(args.trust_policy)
        if bool(args.lab_pause_at) != bool(args.lab_checkpoint_file):
            raise ManagementError("management_checkpoint_invalid")
        def checkpoint(point, record):
            if point != args.lab_pause_at:
                return
            path = Path(args.lab_checkpoint_file)
            if not path.is_absolute() or path.exists() or path.is_symlink():
                raise ManagementError("management_checkpoint_invalid")
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600), "w") as stream:
                json.dump({"point": point, "operation_id": record["operation_id"], "digest": record["digest"]}, stream)
                stream.flush(); os.fsync(stream.fileno())
            while True:
                import time
                time.sleep(1)
        manager = LabManagement(args.repository, trusted_keys=keys, policy_path=args.admin_policy,
                                mode=args.mode, checkpoint=checkpoint,
                                capacity_budget=args.lab_capacity_budget,
                                capacity_profile=args.lab_capacity_profile,
                                capacity_candidate=args.lab_capacity_candidate)
        def terminate(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        lock_path = manager.root / "management.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = manager.socket_path
            if path.exists() or path.is_symlink():
                st = path.lstat()
                if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise ManagementError("management_socket_unsafe")
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    try: probe.connect(str(path))
                    except (ConnectionRefusedError, FileNotFoundError): path.unlink()
                    else: raise ManagementError("management_server_running")
            serve(manager, ready=lambda: print(json.dumps({"ok": True, "lab_only": True,
                "socket": str(manager.socket_path), "server_uid": os.getuid(),
                "server_pid": os.getpid()}), flush=True))
        finally:
            manager.close()
            os.close(fd)
    except Exception as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", type(exc).__name__),
                          "lab_only": True}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
