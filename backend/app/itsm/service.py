import hashlib
import json
import logging
import re
from datetime import datetime

from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password, encrypt_password
from app.core.http_security import require_http_url
from app.itsm.adapters.base import OUTCOME_CONFIRMED, OUTCOME_UNVERIFIED
from app.itsm.adapters.jira import JiraAdapter
from app.itsm.adapters.servicenow import ServiceNowAdapter
from app.itsm.adapters.webhook import WebhookTicketAdapter
from app.itsm.adapters.zentao import ZenTaoAdapter
from app.itsm.models import (
    ItsmConfig,
    ItsmFieldMapping,
    ItsmTicket,
    ItsmTicketRule,
    ItsmTicketSyncLog,
)
from app.itsm.schemas import (
    FieldMappingCreate,
    FieldMappingUpdate,
    ItsmConfigCreate,
    ItsmConfigUpdate,
    TicketClose,
    TicketComment,
    TicketCreate,
    TicketRuleCreate,
    TicketRuleUpdate,
    WebhookCallback,
)
from app.models.alert_event import DBAlertActionLog, DBAlertEvent

logger = logging.getLogger("app.itsm.service")

ADAPTER_MAP = {
    "servicenow": ServiceNowAdapter,
    "jira": JiraAdapter,
    "zentao": ZenTaoAdapter,
    "oa": WebhookTicketAdapter,
    "landray": WebhookTicketAdapter,
    "weaver": WebhookTicketAdapter,
    "webhook": WebhookTicketAdapter,
}

STATUS_MAP = {
    "servicenow": {
        "1": "created",
        "2": "in_progress",
        "6": "resolved",
        "7": "closed",
        "New": "created",
        "Assigned": "in_progress",
        "In Progress": "in_progress",
        "Resolved": "resolved",
        "Closed": "closed",
        "Cancelled": "failed",
    },
    "jira": {"To Do": "created", "Selected for Development": "in_progress", "In Progress": "in_progress", "Done": "resolved", "Closed": "closed"},
    "zentao": {"active": "created", "resolved": "resolved", "closed": "closed"},
}

SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)

SECRET_TEXT_PATTERN = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)\b"
    r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)

DEFAULT_STATUS_MAP = {
    "new": "created",
    "open": "created",
    "created": "created",
    "active": "created",
    "to do": "created",
    "assigned": "in_progress",
    "in progress": "in_progress",
    "processing": "in_progress",
    "working": "in_progress",
    "resolved": "resolved",
    "done": "resolved",
    "fixed": "resolved",
    "closed": "closed",
    "cancelled": "failed",
    "canceled": "failed",
    "failed": "failed",
    "error": "failed",
}


def _decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return decrypt_password(value)
    except ValueError:
        # Existing installations may still contain legacy plaintext values.
        return value


def _is_secret_key(key: str) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _redact_text(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            pass
        else:
            return json.dumps(_redact_sensitive(parsed), ensure_ascii=False, separators=(",", ":"))
    return SECRET_TEXT_PATTERN.sub(lambda match: f'{match.group(1)}"***"', value)


def _redact_sensitive(value, key: str = ""):
    if _is_secret_key(key) and value not in (None, ""):
        return "***"
    if isinstance(value, dict):
        return {item_key: _redact_sensitive(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _safe_text(value, limit: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    redacted = _redact_text(value)
    return redacted[:limit] if limit else redacted


def redact_sensitive_text(value) -> str:
    return _safe_text(value) or ""


def _decode_extra_config(raw: str | dict | None) -> dict:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        text = decrypt_password(raw)
    except ValueError:
        text = raw
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("ITSM extra_config must be a JSON object") from exc
    if not isinstance(decoded, dict):
        raise ValueError("ITSM extra_config must be a JSON object")
    return decoded


def _encode_extra_config(raw: str | dict) -> str:
    serialized = json.dumps(
        _decode_extra_config(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encrypt_password(serialized)


def _merge_extra_config(current: dict, incoming: dict) -> dict:
    merged = dict(current)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            merged[key] = _merge_extra_config(current[key], value)
        elif _is_secret_key(key) and value in (None, "", "***"):
            continue
        else:
            merged[key] = value
    return merged


def serialize_config(config: ItsmConfig) -> dict:
    extra_config = _redact_sensitive(_decode_extra_config(config.extra_config))
    return {
        "id": config.id,
        "config_name": config.config_name,
        "provider_type": config.provider_type,
        "base_url": config.base_url,
        "auth_type": config.auth_type,
        "username": config.username,
        "password_cipher": None,
        "token_cipher": None,
        "has_password": bool(config.password_cipher),
        "has_token": bool(config.token_cipher),
        "extra_config": json.dumps(extra_config, ensure_ascii=False),
        "enabled": config.enabled,
        "is_default": config.is_default,
        "timeout_seconds": config.timeout_seconds,
        "remark": config.remark,
        "create_time": config.create_time,
        "update_time": config.update_time,
    }


def serialize_ticket(ticket: ItsmTicket) -> dict:
    return {
        column.name: _redact_sensitive(getattr(ticket, column.name), column.name)
        for column in ticket.__table__.columns
    }


def serialize_sync_log(log: ItsmTicketSyncLog) -> dict:
    return {
        column.name: _redact_sensitive(getattr(log, column.name), column.name)
        for column in log.__table__.columns
    }


def get_adapter(config: ItsmConfig):
    cls = ADAPTER_MAP.get(config.provider_type)
    if not cls:
        raise ValueError(f"Unsupported provider: {config.provider_type}")
    extra = _decode_extra_config(config.extra_config)
    password = _decrypt_secret(config.password_cipher)
    token = _decrypt_secret(config.token_cipher)
    if not password and str(config.auth_type or "").upper() == "BASIC":
        password = token
    cfg = {"base_url": require_http_url(config.base_url).rstrip("/"), "username": config.username, "password_cipher": password, "token_cipher": token, "extra_config": extra, "timeout_seconds": config.timeout_seconds}
    return cls(cfg)


def list_configs(db: Session):
    return db.query(ItsmConfig).order_by(ItsmConfig.id).all()


def get_config(db: Session, config_id: int):
    return db.query(ItsmConfig).filter(ItsmConfig.id == config_id).first()


def create_config(db: Session, data: ItsmConfigCreate):
    payload = data.model_dump()
    for key in ("password_cipher", "token_cipher"):
        if payload.get(key):
            payload[key] = encrypt_password(payload[key])
    if payload.get("extra_config") is not None:
        payload["extra_config"] = _encode_extra_config(payload["extra_config"])
    c = ItsmConfig(**payload)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def update_config(db: Session, config_id: int, data: ItsmConfigUpdate):
    c = get_config(db, config_id)
    if not c:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        if k in {"password_cipher", "token_cipher"}:
            if not v or v == "***":
                continue
            v = encrypt_password(v)
        elif k == "extra_config" and v is not None:
            current = _decode_extra_config(c.extra_config)
            incoming = _decode_extra_config(v)
            v = _encode_extra_config(_merge_extra_config(current, incoming))
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c


def delete_config(db: Session, config_id: int):
    c = get_config(db, config_id)
    if not c:
        return False
    db.query(ItsmFieldMapping).filter(ItsmFieldMapping.config_id == config_id).delete()
    db.delete(c)
    db.commit()
    return True


def test_config(db: Session, config_id: int):
    c = get_config(db, config_id)
    if not c:
        raise ValueError("Config not found")
    adapter = get_adapter(c)
    errors = adapter.test_connection()
    if errors:
        return {"success": False, "errors": errors}
    result = adapter.get_ticket("test")
    if not result.success:
        error = _safe_text(result.error_message) or "Remote service rejected the test request"
        return {
            "success": False,
            "errors": [error],
            "detail": _safe_text(result.response_body, 200) or "",
        }
    return {"success": True, "message": "Connection OK", "detail": _safe_text(result.response_body, 200) or ""}


def list_field_mappings(db: Session, config_id: int):
    return db.query(ItsmFieldMapping).filter(ItsmFieldMapping.config_id == config_id).order_by(ItsmFieldMapping.id).all()


def create_field_mapping(db: Session, config_id: int, data: FieldMappingCreate):
    m = ItsmFieldMapping(config_id=config_id, **data.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def update_field_mapping(db: Session, mapping_id: int, data: FieldMappingUpdate):
    m = db.query(ItsmFieldMapping).filter(ItsmFieldMapping.id == mapping_id).first()
    if not m:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return m


def _map_external_status(provider_type: str | None, external_status: str | None, default: str) -> str:
    if not external_status:
        return default
    external_status = str(external_status).strip()
    provider_map = STATUS_MAP.get((provider_type or "").lower(), {})
    if external_status in provider_map:
        return provider_map[external_status]
    return DEFAULT_STATUS_MAP.get(external_status.lower(), default)


def _ticket_result(ticket: ItsmTicket, success: bool, error_message: str = "") -> dict:
    return {
        "ticket_id": ticket.id,
        "external_ticket_id": ticket.external_ticket_id or "",
        "external_ticket_url": ticket.external_ticket_url or "",
        "success": success,
        "error_message": _safe_text(error_message) or "",
    }


def _stable_idempotency_key(data: TicketCreate) -> str | None:
    explicit = (data.idempotency_key or "").strip()
    if explicit:
        return explicit
    if not data.source_type or not data.source_id:
        return None
    source_tuple = json.dumps(
        [data.source_type.strip().upper(), data.source_id.strip(), data.instance_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"source:{hashlib.sha256(source_tuple.encode('utf-8')).hexdigest()}"


def create_ticket(db: Session, data: TicketCreate):
    config = get_config(db, data.config_id)
    if not config:
        raise ValueError("ITSM config not found")
    if not config.enabled:
        raise ValueError("ITSM config is disabled")

    idempotency_key = _stable_idempotency_key(data)
    payload = data.model_dump(exclude={"config_id"})
    payload["idempotency_key"] = idempotency_key
    request_payload = _safe_text(json.dumps(payload, ensure_ascii=False))
    ticket = ItsmTicket(
        provider_type=config.provider_type,
        config_id=config.id,
        source_type=data.source_type, source_id=data.source_id, source_ref_id=data.source_ref_id,
        idempotency_key=idempotency_key,
        instance_id=data.instance_id, instance_name=data.instance_name, db_type=data.db_type,
        title=data.title, description=_safe_text(data.description), priority=data.priority,
        severity=data.severity, category=data.category, sub_category=data.sub_category,
        mesguard_status="pending",
        assignee=data.assignee, reporter=data.reporter,
        create_payload=request_payload,
    )
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not idempotency_key:
            raise
        existing = db.query(ItsmTicket).filter(
            ItsmTicket.config_id == config.id,
            ItsmTicket.idempotency_key == idempotency_key,
        ).first()
        if not existing:
            raise
        _log_sync(
            db,
            existing.id,
            existing.external_ticket_id,
            "create",
            "success",
            existing.mesguard_status,
            existing.mesguard_status,
            request_payload,
            json.dumps({"idempotent": True, "ticket_id": existing.id}, ensure_ascii=False),
            None,
        )
        return _ticket_result(existing, existing.mesguard_status != "failed")
    db.refresh(ticket)

    try:
        adapter = get_adapter(config)
        result = adapter.create_ticket(payload)
    except Exception as exc:
        ticket.mesguard_status = "failed"
        db.commit()
        _log_sync(
            db,
            ticket.id,
            ticket.external_ticket_id,
            "create",
            "failed",
            "pending",
            "failed",
            request_payload,
            None,
            str(exc),
        )
        raise

    ticket.external_ticket_id = result.external_id or None
    ticket.external_ticket_url = result.external_url or None
    ticket.mesguard_status = _map_external_status(
        config.provider_type,
        result.status,
        "created" if result.success else "failed",
    ) if result.success else "failed"
    ticket.external_status = result.status or None
    ticket.external_response = _safe_text(result.response_body, 4000)
    db.commit()

    _log_sync(
        db,
        ticket.id,
        result.external_id,
        "create",
        "success" if result.success else "failed",
        None,
        ticket.mesguard_status,
        request_payload,
        result.response_body,
        result.error_message,
    )

    return _ticket_result(ticket, result.success, result.error_message)


def list_tickets(db: Session, source_type: str = None, status: str = None, limit: int = 50):
    q = db.query(ItsmTicket)
    if source_type:
        q = q.filter(ItsmTicket.source_type == source_type)
    if status:
        q = q.filter(ItsmTicket.mesguard_status == status)
    return q.order_by(desc(ItsmTicket.id)).limit(limit).all()


def get_ticket(db: Session, ticket_id: int):
    return db.query(ItsmTicket).filter(ItsmTicket.id == ticket_id).first()


def sync_ticket(db: Session, ticket_id: int) -> ItsmTicket:
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise ValueError("Ticket not found")
    if not ticket.external_ticket_id:
        raise ValueError("No external ticket ID")

    config = get_config(db, ticket.config_id)
    if not config:
        raise ValueError("Config not found")

    old_status = ticket.mesguard_status
    request_payload = json.dumps({"external_ticket_id": ticket.external_ticket_id}, ensure_ascii=False)
    try:
        adapter = get_adapter(config)
        result = adapter.get_ticket(ticket.external_ticket_id)
    except Exception as exc:
        ticket.mesguard_status = "failed"
        db.commit()
        _log_sync(
            db,
            ticket.id,
            ticket.external_ticket_id,
            "sync",
            "failed",
            old_status,
            "failed",
            request_payload,
            None,
            str(exc),
        )
        raise

    new_internal = _map_external_status(config.provider_type, result.status, "failed")

    if result.success:
        ticket.external_status = result.status
        ticket.mesguard_status = new_internal
        ticket.update_time = datetime.now()
        if new_internal in {"resolved", "closed"}:
            ticket.close_time = datetime.now()
    else:
        ticket.mesguard_status = "failed"
        new_internal = "failed"
    db.commit()

    _log_sync(
        db,
        ticket.id,
        ticket.external_ticket_id,
        "sync",
        "success" if result.success else "failed",
        old_status,
        new_internal,
        request_payload,
        result.response_body,
        result.error_message,
    )
    db.refresh(ticket)
    return ticket


def sync_ticket_status(db: Session, ticket_id: int):
    before = get_ticket(db, ticket_id)
    old_status = before.mesguard_status if before else None
    ticket = sync_ticket(db, ticket_id)
    return {
        "success": ticket.mesguard_status != "failed",
        "old_status": old_status,
        "new_status": ticket.mesguard_status,
        "external_status": ticket.external_status,
    }


def add_comment(db: Session, ticket_id: int, data: TicketComment):
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise ValueError("Ticket not found")
    config = get_config(db, ticket.config_id)
    if not config:
        raise ValueError("Config not found")

    adapter = get_adapter(config)
    result = adapter.update_ticket(ticket.external_ticket_id, {"comment": data.comment})
    _log_sync(db, ticket.id, ticket.external_ticket_id, "comment", "success" if result.success else "failed", None, None, json.dumps({"comment": data.comment}), result.response_body, result.error_message)
    return {"success": result.success}


def close_ticket(
    db: Session,
    ticket_id: int,
    data: TicketClose | None = None,
    close_source: bool | None = None,
) -> ItsmTicket:
    ticket = get_ticket(db, ticket_id)
    if not ticket:
        raise ValueError("Ticket not found")
    config = get_config(db, ticket.config_id)
    if not config:
        raise ValueError("Config not found")

    data = data or TicketClose()
    should_close_source = data.close_source if close_source is None else close_source
    old_status = ticket.mesguard_status
    request_payload = json.dumps(
        {"resolution": data.resolution, "close_source": should_close_source},
        ensure_ascii=False,
    )
    try:
        adapter = get_adapter(config)
        result = adapter.close_ticket(ticket.external_ticket_id, {"resolution": data.resolution})
    except Exception as exc:
        ticket.mesguard_status = "failed"
        db.commit()
        _log_sync(
            db,
            ticket.id,
            ticket.external_ticket_id,
            "close",
            "failed",
            old_status,
            "failed",
            request_payload,
            None,
            str(exc),
        )
        raise

    outcome = getattr(result, "outcome", OUTCOME_UNVERIFIED) or OUTCOME_UNVERIFIED
    if result.success:
        ticket.mesguard_status = _map_external_status(config.provider_type, result.status, "closed")
        # Never substitute a status the adapter did not report: an empty answer used to be
        # written as "Closed", so a blocked close still looked closed on the ticket.
        ticket.external_status = result.status or ticket.external_status
        ticket.close_time = datetime.now()
        if should_close_source:
            _close_source_alert(db, ticket)
    else:
        ticket.mesguard_status = "failed"
    db.commit()

    _log_sync(
        db,
        ticket.id,
        ticket.external_ticket_id,
        "close",
        "success" if result.success else "failed",
        old_status,
        ticket.mesguard_status,
        request_payload,
        result.response_body,
        # The sync log has no outcome column; carrying it in the message keeps "external
        # accepted" distinguishable from "external proved closed" when the log is reviewed.
        _with_outcome(result.error_message, outcome),
    )
    db.refresh(ticket)
    return ticket


def _with_outcome(message: str, outcome: str) -> str:
    if outcome == OUTCOME_CONFIRMED:
        return message or ""
    return f"[outcome={outcome}] {message or ''}".strip()


def get_ticket_logs(db: Session, ticket_id: int):
    return db.query(ItsmTicketSyncLog).filter(ItsmTicketSyncLog.ticket_id == ticket_id).order_by(desc(ItsmTicketSyncLog.id)).limit(50).all()


def handle_webhook_callback(db: Session, data: WebhookCallback):
    ticket = db.query(ItsmTicket).filter(ItsmTicket.external_ticket_id == data.external_ticket_id).first()
    if not ticket:
        raise ValueError(f"Ticket not found for external_id: {data.external_ticket_id}")

    old_status = ticket.mesguard_status
    config = get_config(db, ticket.config_id) if ticket.config_id else None
    new_internal = _map_external_status(config.provider_type if config else None, data.status, "failed")

    ticket.external_status = data.status
    ticket.mesguard_status = new_internal
    if new_internal in ("closed", "resolved"):
        ticket.close_time = datetime.now()
    db.commit()

    _log_sync(db, ticket.id, data.external_ticket_id, "webhook_callback", "success", old_status, new_internal, None, json.dumps(data.model_dump()), None)
    return {"success": True, "ticket_id": ticket.id, "new_status": new_internal}


def list_rules(db: Session):
    return db.query(ItsmTicketRule).order_by(ItsmTicketRule.id).all()


def create_rule(db: Session, data: TicketRuleCreate):
    r = ItsmTicketRule(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def update_rule(db: Session, rule_id: int, data: TicketRuleUpdate):
    r = db.query(ItsmTicketRule).filter(ItsmTicketRule.id == rule_id).first()
    if not r:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    return r


def delete_rule(db: Session, rule_id: int):
    r = db.query(ItsmTicketRule).filter(ItsmTicketRule.id == rule_id).first()
    if not r:
        return False
    db.delete(r)
    db.commit()
    return True


def _log_sync(db: Session, ticket_id, external_id, sync_type, sync_status, old_status, new_status, request_payload, response_payload, error_message):
    db.add(ItsmTicketSyncLog(
        ticket_id=ticket_id, external_ticket_id=external_id,
        sync_type=sync_type, sync_status=sync_status,
        old_status=old_status, new_status=new_status,
        request_payload=_safe_text(request_payload, 4000),
        response_payload=_safe_text(response_payload, 4000),
        error_message=_safe_text(error_message, 2000),
    ))
    db.commit()


def _close_source_alert(db: Session, ticket: ItsmTicket) -> None:
    if (ticket.source_type or "").upper() != "ALERT":
        return
    alert_id = ticket.source_ref_id
    if alert_id is None and (ticket.source_id or "").isdigit():
        alert_id = int(ticket.source_id)
    if alert_id is None:
        return
    alert = db.query(DBAlertEvent).filter(DBAlertEvent.id == alert_id).first()
    if not alert:
        return

    now = datetime.now()
    alert.status = "closed"
    alert.resolved_at = alert.resolved_at or now
    alert.close_time = now
    alert.close_reason = f"Closed by ITSM ticket {ticket.external_ticket_id or ticket.id}"
    db.add(DBAlertActionLog(
        event_id=alert.id,
        action_type="itsm_close",
        action_status="success",
        target="itsm",
        request_json=json.dumps({"ticket_id": ticket.id}, ensure_ascii=False),
        response_json=json.dumps({"status": "closed"}, ensure_ascii=False),
    ))
