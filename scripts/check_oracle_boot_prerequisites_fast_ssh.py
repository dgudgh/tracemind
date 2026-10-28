from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


class FakeSocket:
    def close(self):
        return None


@contextmanager
def failing_oracle_connection(_instance):
    failing_oracle_connection.call_count += 1
    raise RuntimeError("db down")
    yield


failing_oracle_connection.call_count = 0


def main() -> int:
    from app.services import db_control
    from app.services import ssh_executor

    original_create_connection = db_control.socket.create_connection
    original_oracle_connection = db_control.oracle_connection
    original_execute_command = ssh_executor.execute_command
    original_test_ssh_connection = ssh_executor.test_ssh_connection
    execute_calls: list[dict] = []
    test_ssh_calls = 0

    def fake_create_connection(_address, timeout=None):
        return FakeSocket()

    def fake_execute_command(_instance, command, timeout=30, **kwargs):
        execute_calls.append({"command": command, "timeout": timeout, **kwargs})
        return {"ok": True, "stdout": "Y", "stderr": "", "exit_code": 0}

    def fake_test_ssh_connection(_instance):
        nonlocal test_ssh_calls
        test_ssh_calls += 1
        return {"ok": True, "message": "should not be called"}

    instance = SimpleNamespace(
        instance_name="oracle-with-ssh",
        host="192.0.2.50",
        port=1521,
        ssh_host="192.0.2.50",
        ssh_port=22,
        ssh_username="root",
    )
    db_control.socket.create_connection = fake_create_connection
    db_control.oracle_connection = failing_oracle_connection
    ssh_executor.execute_command = fake_execute_command
    ssh_executor.test_ssh_connection = fake_test_ssh_connection
    try:
        result = db_control.check_boot_prerequisites(instance, deep=False)
    finally:
        db_control.socket.create_connection = original_create_connection
        db_control.oracle_connection = original_oracle_connection
        ssh_executor.execute_command = original_execute_command
        ssh_executor.test_ssh_connection = original_test_ssh_connection

    failures = {}
    if len(execute_calls) != 1:
        failures["execute_call_count"] = {"expected": 1, "actual": len(execute_calls)}
    else:
        call = execute_calls[0]
        if call.get("timeout") != 2:
            failures["execute_timeout"] = {"expected": 2, "actual": call.get("timeout")}
        if call.get("connect_timeout") != 2:
            failures["connect_timeout"] = {"expected": 2, "actual": call.get("connect_timeout")}
    if test_ssh_calls != 0:
        failures["test_ssh_calls"] = {"expected": 0, "actual": test_ssh_calls}
    if failing_oracle_connection.call_count != 0:
        failures["oracle_connection_calls"] = {"expected": 0, "actual": failing_oracle_connection.call_count}
    summary = result.get("summary", {})
    if summary.get("ssh_available") is not True:
        failures["ssh_available"] = {"expected": True, "actual": summary.get("ssh_available")}

    ok = not failures
    print(
        json.dumps(
            {
                "ok": ok,
                "failures": failures,
                "execute_calls": execute_calls,
                "test_ssh_calls": test_ssh_calls,
                "oracle_connection_calls": failing_oracle_connection.call_count,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
