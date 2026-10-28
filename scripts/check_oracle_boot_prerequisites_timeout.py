from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def main() -> int:
    from app.services import db_control

    captured: dict[str, object] = {}
    original_create_connection = db_control.socket.create_connection

    def fake_create_connection(address, timeout=None):
        captured["address"] = address
        captured["timeout"] = timeout
        raise TimeoutError("network probe timeout")

    instance = SimpleNamespace(
        instance_name="offline-oracle",
        host="192.0.2.40",
        port=1521,
        ssh_host=None,
    )
    db_control.socket.create_connection = fake_create_connection
    try:
        result = db_control.check_boot_prerequisites(instance)
    finally:
        db_control.socket.create_connection = original_create_connection

    failures = {}
    if captured.get("timeout") != 2:
        failures["socket_timeout"] = {"expected": 2, "actual": captured.get("timeout")}
    if result.get("summary", {}).get("server_online") is not False:
        failures["server_online"] = {"expected": False, "actual": result.get("summary", {}).get("server_online")}
    if result.get("summary", {}).get("all_pass") is not False:
        failures["all_pass"] = {"expected": False, "actual": result.get("summary", {}).get("all_pass")}

    ok = not failures
    print(json.dumps({"ok": ok, "failures": failures, "captured": captured, "summary": result.get("summary")}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
