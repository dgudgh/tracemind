from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url
from app.models.observability import ObservabilityCollectLog, ObservabilitySource
from app.services.observability.alert_ingest_service import ingest_alert
from app.services.observability.metric_mapping_service import ingest_metric
from app.services.observability.metric_normalizer import STANDARD_METRICS, normalize_metric_name


SEVERITY_MAP = {
    0: "info",
    1: "info",
    2: "warning",
    3: "warning",
    4: "high",
    5: "critical",
}

ZABBIX_STANDARD_KEY_SEARCHES = [
    "system.cpu.util",
    "system.cpu.load",
    "vm.memory.size",
    "vfs.fs.size",
    "vfs.dev.await",
    "vfs.dev.read.await",
    "vfs.dev.write.await",
    "net.if.in",
    "net.if.out",
    "net.tcp.service.perf",
    "db.connections.used_pct",
    "db.tablespace.used_pct",
    "db.replication.lag_seconds",
    "oracle.fra.used_pct",
    "oracle.dg.lag_seconds",
    "mysql.replication.lag_seconds",
]


def api_call(
    source: ObservabilitySource,
    method: str,
    params: dict[str, Any] | list[Any] | None = None,
    timeout_seconds: float | None = None,
) -> Any:
    api_url = _api_url(source)
    request_timeout = timeout_seconds if timeout_seconds is not None else source.timeout_seconds or 10
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": int(time.time() * 1000),
    }
    token = _token(source)
    auth = None
    if method != "apiinfo.version":
        auth = token or _login(source, request_timeout)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=request_timeout) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    if data.get("error") and auth and _is_auth_header_rejected(data["error"]):
        legacy_payload = {**payload, "auth": auth}
        legacy_req = urllib.request.Request(
            api_url,
            data=json.dumps(legacy_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(legacy_req, timeout=request_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    if data.get("error"):
        error = data["error"]
        raise RuntimeError(f"{error.get('message')}: {error.get('data')}")
    return data.get("result")


def sync_hosts(db: Session, source: ObservabilitySource) -> dict[str, Any]:
    hosts = api_call(
        source,
        "host.get",
        {
            "output": ["hostid", "host", "name", "status"],
            "selectInterfaces": ["ip", "dns", "port", "type", "main"],
            "sortfield": "name",
        },
    )
    count = 0
    for host in hosts or []:
        hostid = str(host.get("hostid") or "")
        if not hostid:
            continue
        ingest_metric(
            db,
            {
                "source_type": "zabbix",
                "source_id": source.id,
                "host_id": hostid,
                "source_metric": "zabbix.host.up",
                "standard_metric": "db.instance.up",
                "metric_value": 1 if str(host.get("status")) == "0" else 0,
                "metric_unit": "bool",
                "status": "ok" if str(host.get("status")) == "0" else "disabled",
            },
        )
        count += 1
    sample = _sample(hosts)
    _log(
        db,
        source.id,
        "info",
        "zabbix_hosts",
        f"Zabbix host sync completed, count={len(hosts or [])}, metrics={count}",
        {"count": len(hosts or []), "metric_count": count, "sample": sample},
    )
    return {"status": "success", "synced_count": len(hosts or []), "metric_count": count, "items": sample}


def sync_problems(db: Session, source: ObservabilitySource, limit: int = 100) -> dict[str, Any]:
    problems = api_call(
        source,
        "problem.get",
        {
            "output": "extend",
            "selectAcknowledges": "extend",
            "selectTags": "extend",
            "recent": True,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": limit,
        },
    )
    trigger_hosts = _trigger_host_map(source, problems or [])
    count = 0
    for item in problems or []:
        host_info = trigger_hosts.get(str(item.get("objectid") or "")) or {}
        severity = _int(item.get("severity"), 0)
        status = "resolved" if item.get("r_eventid") not in (None, "", "0") else "firing"
        ingest_alert(
            db,
            {
                "source_type": "zabbix",
                "source_id": source.id,
                "alert_key": item.get("eventid") or item.get("objectid") or item.get("name"),
                "host_id": str(host_info.get("hostid") or item.get("hostid") or item.get("objectid") or ""),
                "severity": SEVERITY_MAP.get(severity, "warning"),
                "title": item.get("name") or "Zabbix Problem",
                "content": item.get("opdata") or item.get("comments") or item.get("name"),
                "metric_name": item.get("objectid"),
                "status": status,
                "first_time": _timestamp(item.get("clock")),
                "last_time": _timestamp(item.get("clock")),
                "raw_data": {**item, "host": host_info},
                "labels": {
                    "hostid": host_info.get("hostid") or item.get("hostid"),
                    "host": host_info.get("host"),
                    "host_name": host_info.get("name"),
                    "triggerid": item.get("objectid"),
                    "eventid": item.get("eventid"),
                },
            },
        )
        count += 1
    _log(
        db,
        source.id,
        "info",
        "zabbix_problems",
        f"Zabbix problem sync completed, count={count}",
        {"count": count, "sample": _sample(problems)},
    )
    return {"status": "success", "synced_count": count}


def _trigger_host_map(source: ObservabilitySource, problems: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    trigger_ids = sorted({str(item.get("objectid") or "") for item in problems if item.get("objectid")})
    if not trigger_ids:
        return {}
    try:
        triggers = api_call(
            source,
            "trigger.get",
            {
                "output": ["triggerid", "description"],
                "triggerids": trigger_ids,
                "selectHosts": ["hostid", "host", "name"],
            },
        )
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for trigger in triggers or []:
        hosts = trigger.get("hosts") or []
        if hosts:
            result[str(trigger.get("triggerid"))] = hosts[0]
    return result


def sync_metrics(db: Session, source: ObservabilitySource, hostid: str | None = None, limit: int = 100) -> dict[str, Any]:
    items = _standard_items(source, hostid=hostid, limit=limit)
    count = 0
    skipped = 0
    for item in items or []:
        if (
            str(item.get("value_type")) not in {"0", "3"}
            or item.get("lastvalue") in (None, "")
            or not _should_store_standard_item(item)
        ):
            skipped += 1
            continue
        ingest_metric(
            db,
            {
                "source_type": "zabbix",
                "source_id": source.id,
                "host_id": str(item.get("hostid") or hostid or ""),
                "source_metric": item.get("key_") or item.get("name") or item.get("itemid"),
                "metric_value": item.get("lastvalue"),
                "metric_unit": item.get("units"),
                "metric_time": item.get("lastclock"),
                "status": "ok",
            },
        )
        count += 1
    _log(
        db,
        source.id,
        "info",
        "zabbix_metrics",
        f"Zabbix item metric sync completed, stored={count}, skipped={skipped}",
        {"stored": count, "skipped": skipped, "sample": _sample(items)},
    )
    return {"status": "success", "synced_count": count, "skipped_count": skipped}


def _standard_items(source: ObservabilitySource, hostid: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    per_search_limit = max(limit, 200)
    for key_search in ZABBIX_STANDARD_KEY_SEARCHES:
        params: dict[str, Any] = {
            "output": ["itemid", "hostid", "name", "key_", "value_type", "units", "lastvalue", "lastclock"],
            "filter": {"value_type": ["0", "3"], "status": "0"},
            "search": {"key_": key_search},
            "searchByAny": True,
            "sortfield": "name",
            "limit": per_search_limit,
        }
        if hostid:
            params["hostids"] = hostid
        try:
            for item in api_call(source, "item.get", params) or []:
                itemid = str(item.get("itemid") or "")
                if itemid:
                    result[itemid] = item
        except Exception as exc:
            if _is_source_unavailable(exc):
                raise
            continue
    return list(result.values())


def _is_source_unavailable(exc: Exception) -> bool:
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, urllib.error.URLError),
    ) and not isinstance(exc, urllib.error.HTTPError)


def _should_store_standard_item(item: dict[str, Any]) -> bool:
    key = str(item.get("key_") or item.get("name") or "")
    standard_metric = normalize_metric_name(key)
    if standard_metric not in STANDARD_METRICS:
        return False
    if key.startswith("system.cpu.util[") and ",idle" not in key:
        return False
    if key.startswith("vfs.fs.size[") and not (",pused]" in key or ",pfree]" in key):
        return False
    if key.startswith(("net.if.in[", "net.if.out[")) and ",errors]" not in key:
        return False
    return True


def _api_url(source: ObservabilitySource) -> str:
    if not source.api_url:
        raise ValueError("Zabbix api_url not configured")
    base_url = source.api_url.rstrip("/")
    url = base_url if base_url.endswith("api_jsonrpc.php") else f"{base_url}/api_jsonrpc.php"
    return require_http_url(url)


def _token(source: ObservabilitySource) -> str | None:
    if not source.token_encrypted:
        return None
    return decrypt_password(source.token_encrypted)


def _login(source: ObservabilitySource, timeout_seconds: float | None = None) -> str | None:
    if not source.username or not source.password_encrypted:
        return None
    password = decrypt_password(source.password_encrypted)
    last_error: dict[str, Any] | None = None
    for user_key in ("username", "user"):
        payload = {
            "jsonrpc": "2.0",
            "method": "user.login",
            "params": {user_key: source.username, "password": password},
            "id": 1,
        }
        req = urllib.request.Request(
            _api_url(source),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds or source.timeout_seconds or 10) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        if not data.get("error"):
            return data.get("result")
        last_error = data["error"]
    if last_error:
        raise RuntimeError(f"{last_error.get('message')}: {last_error.get('data')}")
    return None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except Exception:
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _sample(items: Any, limit: int = 20) -> list[Any]:
    if not isinstance(items, list):
        return []
    return items[:limit]


def _is_auth_header_rejected(error: dict[str, Any]) -> bool:
    text = f"{error.get('message') or ''} {error.get('data') or ''}".lower()
    return "not authorized" in text or "session terminated" in text or "invalid auth" in text


def _log(db: Session, source_id: int | None, level: str, log_type: str, message: str, raw_data: Any = None) -> None:
    db.add(
        ObservabilityCollectLog(
            source_id=source_id,
            log_level=level,
            log_type=log_type,
            message=message,
            raw_data=raw_data,
        )
    )
    db.commit()
