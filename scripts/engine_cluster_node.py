"""Run one signed fixture Engine Supervisor as a fenced local cluster node.

This is a local software exercise, not a remote Host identity authority.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path

from _bootstrap import add_backend_to_path


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--effects", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--capacity", type=int, default=1)
    parser.add_argument("--lease-seconds", type=float, default=1.5)
    args = parser.parse_args(argv)
    add_backend_to_path()
    from app.runtime.engine_cluster import EngineCluster, ClusterRefused
    from app.runtime.engine_host.packages import load_package
    from app.runtime.engine_host.supervisor import FixtureSupervisor, SupervisorError

    package = load_package(args.package)
    cluster = EngineCluster(args.cluster, args.store, lease_seconds=args.lease_seconds)
    supervisor = FixtureSupervisor(workdir=args.workdir, restart=False)
    stopped = threading.Event()
    status = {"state": "starting", "node_id": args.node_id, "host_pid": os.getpid(),
              "events": []}

    def publish(event=None, **detail):
        if event:
            status["events"].append({"event": event, **detail})
        _write(args.status, status)

    def probe():
        heartbeat = supervisor.heartbeat()
        record = supervisor.record
        identity = cluster._identity_from_supervisor(package, supervisor.identity)
        return {"host_pid": os.getpid(), "provider_pid": record.pid,
                "provider_created_at": record.start_time,
                "launch_id": record.launch_instance_id, "identity": identity,
                "generation": args.generation, "provider_heartbeat": heartbeat,
                "host_state": supervisor.status()["state"]}

    def stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    joined = False
    try:
        supervisor.start(package.source, package.manifest, package.signature, package.trusted_keys,
                         expected_namespace="tmengine.test", expected_channel="test",
                         generation=args.generation, store_path=args.effects)
        member = cluster.join(args.node_id, package, args.generation, args.capacity, probe)
        joined = True
        launch_id = member["launch_id"]
        status.update(state="ready", launch_id=launch_id, provider_pid=supervisor.record.pid,
                      identity=member["identity"], membership_epoch=member["membership_epoch"])
        publish("joined", membership_epoch=member["membership_epoch"])

        def heartbeat_loop():
            while not stopped.wait(.2):
                try:
                    status["membership_epoch"] = cluster.heartbeat(args.node_id, launch_id, probe)
                    _write(args.status, status)
                except Exception as exc:
                    status["state"] = "fenced"
                    publish("heartbeat_refused", reason=str(exc))
                    stopped.set()

        threading.Thread(target=heartbeat_loop, daemon=True).start()
        while not stopped.is_set():
            try:
                task = cluster.claim(args.node_id, launch_id)
            except ClusterRefused as exc:
                status["state"] = "fenced"
                publish("claim_refused", reason=str(exc))
                break
            if task is None:
                stopped.wait(.05)
                continue
            status["state"] = "busy"
            publish("claimed", task_id=task["task_id"], token=task["claim_token"],
                    epoch=task["claim_epoch"])
            renewing = threading.Event()

            def renew():
                while not renewing.wait(.2):
                    try:
                        cluster.renew_claim(task["task_id"], args.node_id, launch_id,
                                            task["claim_token"], task["claim_epoch"])
                    except ClusterRefused as exc:
                        publish("claim_renew_refused", task_id=task["task_id"], reason=str(exc))
                        return

            thread = threading.Thread(target=renew, daemon=True)
            thread.start()
            result = None
            effect_state = "none"
            error_code = ""
            try:
                context = ({"operation_id": task["operation_id"]} if task["method"] == "execute" else None)
                result = supervisor.call(task["method"], task["params"], timeout=20, context=context)
                effect_state = "completed" if task["method"] == "execute" else "none"
            except SupervisorError as exc:
                effect_state = "unknown" if task["method"] == "execute" else "none"
                error_code = exc.code
            except Exception as exc:
                effect_state = "unknown" if task["method"] == "execute" else "none"
                error_code = type(exc).__name__
            finally:
                renewing.set()
                thread.join(timeout=1)
            try:
                if result is None and task["method"] == "invoke_read":
                    # Read-only work is safe to requeue only through a new epoch.
                    status["state"] = "fenced"
                    publish("read_failed_reconcile", task_id=task["task_id"], reason=error_code)
                    stopped.set()
                    continue
                committed = cluster.commit_result(task["task_id"], args.node_id, launch_id,
                                                  task["claim_token"], task["claim_epoch"],
                                                  result=result, effect_state=effect_state,
                                                  error_code=error_code)
                publish("result", task_id=task["task_id"], state=committed["state"])
            except Exception as exc:
                publish("result_refused", task_id=task["task_id"], reason=str(exc))
            status["state"] = "ready" if not stopped.is_set() else "fenced"
            _write(args.status, status)
    except Exception as exc:
        status["state"] = "failed"
        publish("startup_failed", reason=str(exc))
        return 2
    finally:
        if joined:
            try:
                cluster.leave(args.node_id, supervisor.record.launch_instance_id)
            except ClusterRefused:
                pass
        supervisor.stop()
        status["state"] = "stopped"
        publish("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
