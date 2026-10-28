#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any


def _json_request(url: str, *, token: str | None = None, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body or "{}")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _api_url(base_url: str, path: str, params: dict[str, Any] | None = None) -> str:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    return f"{base_url.rstrip('/')}{path}{'?' + query if query else ''}"


def _login(api_base: str, username: str, password: str) -> str:
    data = _json_request(
        _api_url(api_base, "/auth/login"),
        method="POST",
        payload={"username": username, "password": password},
    )
    token = data.get("token") or data.get("access_token")
    if not token:
        raise AssertionError("登录成功但未返回 token")
    return str(token)


def _find_instance(api_base: str, token: str, instance_id: int | None, host: str) -> dict[str, Any]:
    if instance_id:
        data = _json_request(_api_url(api_base, f"/instances/{instance_id}"), token=token)
        return data
    data = _json_request(_api_url(api_base, "/instances", {"page": 1, "page_size": 500}), token=token)
    items = data.get("items") or []
    for item in items:
        if str(item.get("host") or "") == host:
            return item
    raise AssertionError(f"未找到主机为 {host} 的数据库实例")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check instance-level Zabbix/Prometheus linkage health.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8800/api/v1")
    parser.add_argument("--instance-id", type=int)
    parser.add_argument("--host", default=os.getenv("MESGUARD_OBSERVABILITY_HOST"))
    parser.add_argument("--username", default=os.getenv("MESGUARD_SMOKE_USER", "admin"))
    parser.add_argument("--password", default=os.getenv("MESGUARD_SMOKE_PASSWORD"))
    parser.add_argument("--probe", action="store_true", help="Also require source probe results.")
    args = parser.parse_args()
    if not args.password:
        parser.error("--password or MESGUARD_SMOKE_PASSWORD is required")
    if not args.instance_id and not args.host:
        parser.error("--instance-id or --host/MESGUARD_OBSERVABILITY_HOST is required")

    token = _login(args.api_base, args.username, args.password)
    instance = _find_instance(args.api_base, token, args.instance_id, args.host)
    instance_id = int(instance["id"])
    data = _json_request(
        _api_url(
            args.api_base,
            "/observability/linkage-health",
            {"instance_id": instance_id, "probe_sources": 1 if args.probe else 0, "probe_timeout_seconds": 2},
        ),
        token=token,
    )

    _assert(data.get("instance", {}).get("id") == instance_id, "健康检查必须返回目标实例")
    _assert(data.get("overall_status") in {"ok", "warning", "failed"}, "必须返回整体状态")
    _assert(data.get("summary"), "必须返回可读摘要")
    _assert(isinstance(data.get("checks"), list) and data["checks"], "必须返回检查项列表")

    checks = {item.get("key"): item for item in data["checks"]}
    _assert("binding" in checks, "必须检查实例绑定")
    _assert("zabbix_mapping" in checks, "必须检查 Zabbix 绑定")
    _assert("prometheus_mapping" in checks, "必须检查 Prometheus Target")
    _assert("recent_evidence" in checks, "必须检查最近告警/指标证据")

    zabbix = checks["zabbix_mapping"]
    prometheus = checks["prometheus_mapping"]
    recommendations = data.get("recommendations") or []
    _assert(zabbix.get("status") in {"ok", "warning", "failed"}, "Zabbix 检查必须有明确状态")
    _assert(prometheus.get("status") in {"ok", "warning", "failed"}, "Prometheus 检查必须有明确状态")
    if zabbix.get("status") != "ok":
        _assert(any("Zabbix" in str(item) for item in recommendations), "Zabbix 异常时必须给出修复建议")
    if prometheus.get("status") != "ok":
        _assert(any("Prometheus" in str(item) for item in recommendations), "Prometheus 异常时必须给出修复建议")

    if args.probe:
        _assert("source_probes" in data and data["source_probes"], "主动探测时必须返回数据源探测结果")

    print(json.dumps({
        "status": "passed",
        "instance_id": instance_id,
        "host": instance.get("host"),
        "overall_status": data.get("overall_status"),
        "summary": data.get("summary"),
        "checks": {key: checks[key].get("status") for key in sorted(checks)},
        "recommendations": recommendations,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
