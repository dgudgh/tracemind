"""Local process callback fixture for platform/Provider transaction rehearsals."""
from __future__ import annotations

import argparse
import json
import os
import socketserver
import sqlite3
import threading
import time
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--active", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--update-lease-db", type=Path)
    args = parser.parse_args(argv)
    if args.socket.exists():
        raise FileExistsError(args.socket)
    state = {"active": args.active, "ready": {args.active, args.candidate}, "fail_next": False}
    lock = threading.Lock()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(65536))
                command = request.get("command")
                with lock:
                    if command == "status":
                        result = {"active_digest": state["active"], "pid": os.getpid()}
                    elif command == "probe":
                        target = request["digest"]
                        result = {"digest": target, "ready": target in state["ready"], "pid": os.getpid()}
                    elif command == "switch":
                        if args.update_lease_db:
                            with sqlite3.connect(args.update_lease_db, timeout=5) as lease_db:
                                lease_db.execute("BEGIN IMMEDIATE")
                                row = lease_db.execute("SELECT owner,epoch,expires FROM lease WHERE id=1").fetchone()
                                if (row is None or row[0] != request.get("lease_owner") or
                                        row[1] != request.get("lease_epoch") or row[2] <= time.time()):
                                    raise ValueError("provider_update_fence_lost")
                                if state["fail_next"]:
                                    state["fail_next"] = False
                                    raise ValueError("fixture_injected_provider_switch_failure")
                                if request["expected"] != state["active"] or request["target"] not in state["ready"]:
                                    raise ValueError("provider_cas_or_readiness_failed")
                                state["active"] = request["target"]
                        else:
                            if state["fail_next"]:
                                state["fail_next"] = False
                                raise ValueError("fixture_injected_provider_switch_failure")
                            if request["expected"] != state["active"] or request["target"] not in state["ready"]:
                                raise ValueError("provider_cas_or_readiness_failed")
                            state["active"] = request["target"]
                        result = {"active_digest": state["active"], "pid": os.getpid()}
                    elif command == "fail-next":
                        state["fail_next"] = True
                        result = {"armed": True}
                    else:
                        raise ValueError("unknown_command")
                self.wfile.write(json.dumps({"ok": True, "result": result}).encode() + b"\n")
            except (ValueError, KeyError, sqlite3.Error) as exc:
                self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")

    args.socket.parent.mkdir(parents=True, exist_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(args.socket), Handler) as server:
        os.chmod(args.socket, 0o600)
        try:
            server.serve_forever()
        finally:
            args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
