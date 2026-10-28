from __future__ import annotations

import base64
import json
import time
import urllib.request
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password
from app.core.http_security import require_http_url
from app.models.observability import ObservabilityCollectLog, ObservabilitySource


def auth_headers(source: ObservabilitySource) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json,text/plain,*/*"}
    auth_type = (source.auth_type or "").lower()
    token = decrypt_password(source.token_encrypted) if source.token_encrypted else None
    if token and auth_type in {"bearer", "token", "api_key"}:
        headers["Authorization"] = f"Bearer {token}"
    if auth_type == "basic" and source.username and source.password_encrypted:
        password = decrypt_password(source.password_encrypted)
        encoded = base64.b64encode(f"{source.username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


def join_url(base: str | None, path: str = "") -> str:
    if not base:
        raise ValueError("未配置访问地址")
    if not path:
        return require_http_url(base)
    return require_http_url(f"{base.rstrip('/')}/{path.lstrip('/')}")


def http_request(source: ObservabilitySource, path: str = "", method: str = "GET", payload: Any = None) -> dict[str, Any]:
    url = join_url(source.api_url, path)
    data = None
    headers = auth_headers(source)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=source.timeout_seconds or 10) as resp:
        raw = resp.read()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        text = raw.decode("utf-8", errors="replace")
        content_type = resp.headers.get("Content-Type", "")
        body: Any = text
        if "json" in content_type.lower() or text.strip().startswith(("{", "[")):
            try:
                body = json.loads(text or "{}")
            except ValueError:
                body = text
        return {"url": url, "status_code": resp.status, "elapsed_ms": elapsed_ms, "body": body, "text": text}


def metric(source: ObservabilitySource, name: str, value: Any, unit: str | None = None, status: str = "ok") -> dict[str, Any]:
    return {
        "source_type": source.source_type,
        "source_id": source.id,
        "host_id": source.api_url,
        "source_metric": name,
        "standard_metric": name,
        "metric_value": value,
        "metric_unit": unit,
        "metric_time": datetime.utcnow(),
        "status": status,
    }


def flatten_numbers(data: Any, prefix: str = "", limit: int = 80) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []

    def walk(value: Any, path: str) -> None:
        if len(items) >= limit:
            return
        if isinstance(value, bool):
            items.append((path, 1.0 if value else 0.0))
            return
        if isinstance(value, (int, float)):
            items.append((path, float(value)))
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value[:10]):
                walk(child, f"{path}.{index}" if path else str(index))

    walk(data, prefix)
    return items[:limit]


def collect_log(db: Session, source: ObservabilitySource, level: str, message: str, raw_data: Any = None) -> None:
    db.add(
        ObservabilityCollectLog(
            source_id=source.id,
            log_level=level,
            log_type="collect",
            message=message,
            raw_data=raw_data,
        )
    )
    db.commit()
