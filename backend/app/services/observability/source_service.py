from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password, encrypt_password
from app.core.http_security import require_http_url
from app.models.observability import ObservabilityCollectLog, ObservabilitySource


SENSITIVE_FIELDS = {"password", "token", "api_key"}


def _source_payload(row: ObservabilitySource) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_name": row.source_name,
        "source_type": row.source_type,
        "api_url": row.api_url,
        "auth_type": row.auth_type,
        "username": row.username,
        "enabled": row.enabled,
        "timeout_seconds": row.timeout_seconds,
        "collect_interval_seconds": row.collect_interval_seconds,
        "remark": row.remark,
        "has_password": bool(row.password_encrypted),
        "has_token": bool(row.token_encrypted),
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


def list_sources(db: Session, source_type: str | None = None, enabled: int | None = None) -> list[dict[str, Any]]:
    query = db.query(ObservabilitySource)
    if source_type:
        query = query.filter(ObservabilitySource.source_type == source_type)
    if enabled is not None:
        query = query.filter(ObservabilitySource.enabled == enabled)
    return [_source_payload(row) for row in query.order_by(ObservabilitySource.id.desc()).all()]


def get_source(db: Session, source_id: int) -> ObservabilitySource | None:
    return db.query(ObservabilitySource).filter(ObservabilitySource.id == source_id).first()


def upsert_source(db: Session, data: dict[str, Any], source: ObservabilitySource | None = None) -> dict[str, Any]:
    row = source or ObservabilitySource(
        source_name=data.get("source_name") or data.get("config_name") or "Observability Source",
        source_type=data.get("source_type") or "webhook",
    )
    if not source:
        db.add(row)
    for field in [
        "source_name",
        "source_type",
        "api_url",
        "auth_type",
        "username",
        "enabled",
        "timeout_seconds",
        "collect_interval_seconds",
        "remark",
    ]:
        if field in data and data[field] is not None:
            value = data[field]
            if field == "api_url" and isinstance(value, str):
                value = value.strip().rstrip("/")
                if value:
                    value = require_http_url(value)
            setattr(row, field, value)
    if data.get("password"):
        row.password_encrypted = encrypt_password(data["password"])
    if data.get("token") or data.get("api_key"):
        row.token_encrypted = encrypt_password(data.get("token") or data.get("api_key"))
    db.commit()
    db.refresh(row)
    return _source_payload(row)


def delete_source(db: Session, row: ObservabilitySource) -> None:
    db.delete(row)
    db.commit()


def _headers(row: ObservabilitySource) -> dict[str, str]:
    token = decrypt_password(row.token_encrypted) if row.token_encrypted else None
    if not token:
        return {}
    auth_type = (row.auth_type or "").lower()
    if auth_type in {"bearer", "token", "api_key"}:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _basic_auth(row: ObservabilitySource) -> dict[str, str]:
    if (row.auth_type or "").lower() != "basic" or not row.username or not row.password_encrypted:
        return {}
    password = decrypt_password(row.password_encrypted)
    encoded = base64.b64encode(f"{row.username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def test_source(db: Session, row: ObservabilitySource) -> dict[str, Any]:
    if not row.api_url:
        return {"status": "unknown", "message": "未配置访问地址，仅保存数据源配置。"}
    started = datetime.utcnow()
    headers = {"Accept": "application/json"}
    headers.update(_headers(row))
    headers.update(_basic_auth(row))
    if row.source_type == "zabbix":
        return _test_zabbix(db, row, headers, started)
    return _test_http(db, row, headers, started)


def _test_http(db: Session, row: ObservabilitySource, headers: dict[str, str], started: datetime) -> dict[str, Any]:
    try:
        req = urllib.request.Request(require_http_url(row.api_url), headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=row.timeout_seconds or 10) as resp:
            elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
            _log(db, row.id, "info", "connection_test", f"{row.source_type} 连接测试成功，HTTP {resp.status}", {"elapsed_ms": elapsed})
            return {"status": "success", "message": "连接测试成功", "http_status": resp.status, "elapsed_ms": elapsed}
    except Exception as exc:
        _log(db, row.id, "error", "connection_test", f"{row.source_type} 连接测试失败：{exc}")
        return {"status": "failed", "message": str(exc)}


def _test_zabbix(db: Session, row: ObservabilitySource, headers: dict[str, str], started: datetime) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "method": "apiinfo.version", "params": {}, "id": 1}
    try:
        req = urllib.request.Request(
            require_http_url(row.api_url),
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=row.timeout_seconds or 10) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            elapsed = int((datetime.utcnow() - started).total_seconds() * 1000)
            _log(db, row.id, "info", "connection_test", "Zabbix API 连接测试成功", body)
            return {"status": "success", "message": "Zabbix API 连接测试成功", "version": body.get("result"), "elapsed_ms": elapsed}
    except (urllib.error.URLError, ValueError, OSError) as exc:
        _log(db, row.id, "error", "connection_test", f"Zabbix API 连接测试失败：{exc}")
        return {"status": "failed", "message": str(exc)}


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
