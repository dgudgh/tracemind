from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8800"


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            return response.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = {"raw": text}
        return exc.code, body


def fail(message: str, detail: Any = None) -> int:
    report = {"ok": False, "error": message}
    if detail is not None:
        report["detail"] = detail
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1


def select_instance(items: list[dict[str, Any]], instance_id: int | None, db_type: str | None) -> dict[str, Any] | None:
    enabled = [item for item in items if item.get("enabled", 1) == 1]
    if instance_id is not None:
        return next((item for item in enabled if int(item.get("id") or 0) == instance_id), None)
    if db_type:
        return next((item for item in enabled if item.get("db_type") == db_type), None)
    for preferred in ("mysql", "oracle", "postgresql", "sqlserver"):
        found = next((item for item in enabled if item.get("db_type") == preferred), None)
        if found:
            return found
    return enabled[0] if enabled else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an end-to-end diagnosis smoke test. This creates one local diagnosis incident."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Backend base URL.")
    parser.add_argument("--username", default=os.getenv("MESGUARD_SMOKE_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("MESGUARD_SMOKE_PASSWORD"))
    parser.add_argument("--instance-id", type=int, default=None)
    parser.add_argument("--db-type", choices=["oracle", "mysql", "postgresql", "sqlserver"], default=None)
    parser.add_argument("--timeout", type=float, default=90.0, help="Diagnosis request timeout seconds.")
    args = parser.parse_args()
    if not args.password:
        parser.error("--password or MESGUARD_SMOKE_PASSWORD is required")

    base_url = args.base_url.rstrip("/")
    status, login = request_json(
        "POST",
        f"{base_url}/api/v1/auth/login",
        {"username": args.username, "password": args.password},
        timeout=10.0,
    )
    if status >= 400 or not login.get("token"):
        return fail("login failed", {"status": status, "response": login})

    token = urllib.parse.quote(str(login["token"]))
    status, instances = request_json("GET", f"{base_url}/api/v1/instances?token={token}&page_size=100", timeout=10.0)
    if status >= 400:
        return fail("instance list failed", {"status": status, "response": instances})

    instance = select_instance(instances.get("items") or [], args.instance_id, args.db_type)
    if not instance:
        return fail("no enabled instance matched", {"instance_id": args.instance_id, "db_type": args.db_type})

    payload = {
        "instance_id": instance["id"],
        "title": f"API smoke diagnosis {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "trigger_source": "api_smoke",
        "create_ticket": False,
        "notify": False,
    }
    status, diagnosis = request_json(
        "POST",
        f"{base_url}/api/v1/diagnosis-center/diagnose?token={token}",
        payload,
        timeout=args.timeout,
    )
    if status >= 500:
        return fail("diagnosis endpoint returned 5xx", {"status": status, "response": diagnosis})
    if status >= 400 or not diagnosis.get("id"):
        return fail("diagnosis failed", {"status": status, "response": diagnosis})

    report = {
        "ok": True,
        "base_url": base_url,
        "instance": {
            "id": instance.get("id"),
            "name": instance.get("instance_name"),
            "db_type": instance.get("db_type"),
        },
        "incident": {
            "id": diagnosis.get("id"),
            "status": diagnosis.get("status"),
            "severity": diagnosis.get("severity"),
            "score": diagnosis.get("score"),
            "evidence_count": diagnosis.get("evidence_count"),
            "root_cause_count": diagnosis.get("root_cause_count"),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
