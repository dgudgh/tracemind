import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, case, desc, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password, encrypt_password
from app.core.http_security import require_http_url
from app.notify.adapters.base import NotifyResult
from app.notify.adapters.dingtalk import DingTalkAdapter
from app.notify.adapters.feishu import FeishuAdapter
from app.notify.adapters.sms import SmsAdapter
from app.notify.adapters.smtp import SmtpAdapter
from app.notify.adapters.wecom import WeComAdapter
from app.notify.adapters.webhook import WebhookAdapter
from app.notify.models import (
    NotifyChannel,
    NotifyEvent,
    NotifyLog,
    NotifyPolicy,
    NotifyPolicyChannel,
    NotifyRecipient,
    NotifyTemplate,
    NotifyThrottle,
)
from app.notify.schemas import (
    ChannelCreate,
    ChannelTestMessage,
    ChannelUpdate,
    EventCreate,
    PolicyCreate,
    PolicyUpdate,
    RecipientCreate,
    TemplateCreate,
    TemplateUpdate,
)

logger = logging.getLogger("app.notify.service")

ADAPTER_MAP = {
    "wecom": WeComAdapter,
    "dingtalk": DingTalkAdapter,
    "feishu": FeishuAdapter,
    "sms": SmsAdapter,
    "smtp": SmtpAdapter,
    "webhook": WebhookAdapter,
}

SECRET_CONFIG_KEYS = {
    "access_token",
    "api_key",
    "app_secret",
    "authorization",
    "bearer_token",
    "client_secret",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook_secret",
    "webhook_url",
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


def decode_channel_config(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Notification channel config must be a JSON object")
    try:
        text = decrypt_password(raw)
    except ValueError:
        text = raw
    try:
        result = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Notification channel config must be valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Notification channel config must be a JSON object")
    return result


def encode_channel_config(raw: str | dict) -> str:
    config = decode_channel_config(raw)
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encrypt_password(serialized)


def _is_secret_key(key: str) -> bool:
    normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in SECRET_CONFIG_KEYS or any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _redact_text(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            pass
        else:
            return json.dumps(_redact_config_value(parsed), ensure_ascii=False, separators=(",", ":"))
    return SECRET_TEXT_PATTERN.sub(lambda match: f'{match.group(1)}"***"', value)


def _redact_config_value(value, key: str = ""):
    if _is_secret_key(key) and value not in (None, ""):
        return "***"
    if isinstance(value, dict):
        return {item_key: _redact_config_value(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_config_value(item) for item in value]
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


def redact_channel_config(raw: str | dict) -> dict:
    return _redact_config_value(decode_channel_config(raw))


def _has_redacted_secret(value) -> bool:
    if value == "***":
        return True
    if isinstance(value, dict):
        return any(_has_redacted_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_redacted_secret(item) for item in value)
    return False


def _merge_channel_config(current: dict, incoming: dict) -> dict:
    merged = dict(current)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            merged[key] = _merge_channel_config(current[key], value)
        elif key.lower() in SECRET_CONFIG_KEYS and value in (None, "", "***"):
            continue
        else:
            merged[key] = value
    return merged


def serialize_channel(channel: NotifyChannel) -> dict:
    config = redact_channel_config(channel.config_json)
    return {
        "id": channel.id,
        "channel_name": channel.channel_name,
        "channel_type": channel.channel_type,
        "config_json": json.dumps(config, ensure_ascii=False),
        "has_secret": _has_redacted_secret(config),
        "enabled": channel.enabled,
        "description": channel.description,
        "create_time": channel.create_time,
        "update_time": channel.update_time,
    }


def serialize_event(event: NotifyEvent) -> dict:
    return {
        column.name: _redact_config_value(getattr(event, column.name), column.name)
        for column in event.__table__.columns
    }


def serialize_log(log: NotifyLog) -> dict:
    return {
        column.name: _redact_config_value(getattr(log, column.name), column.name)
        for column in log.__table__.columns
    }


def get_adapter(channel_type: str, config: dict):
    _validate_channel_url(channel_type, config)
    cls = ADAPTER_MAP.get(channel_type)
    if not cls:
        raise ValueError(f"Unsupported channel type: {channel_type}")
    return cls(config)


def _validate_channel_url(channel_type: str, config: dict) -> None:
    field = {
        "wecom": "webhook_url",
        "dingtalk": "webhook_url",
        "feishu": "webhook_url",
        "sms": "url",
        "webhook": "url",
    }.get(channel_type)
    if field and config.get(field):
        require_http_url(str(config[field]))


def list_channels(db: Session, channel_type: str = None):
    q = db.query(NotifyChannel)
    if channel_type:
        q = q.filter(NotifyChannel.channel_type == channel_type)
    return q.order_by(NotifyChannel.id).all()


def get_channel(db: Session, channel_id: int):
    return db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()


def create_channel(db: Session, data: ChannelCreate):
    payload = data.model_dump()
    config = decode_channel_config(payload["config_json"])
    _validate_channel_url(payload["channel_type"], config)
    payload["config_json"] = encode_channel_config(config)
    ch = NotifyChannel(**payload)
    db.add(ch)
    db.commit()
    db.refresh(ch)
    return ch


def update_channel(db: Session, channel_id: int, data: ChannelUpdate):
    ch = get_channel(db, channel_id)
    if not ch:
        return None
    payload = data.model_dump(exclude_unset=True)
    if payload.get("config_json") is not None:
        current = decode_channel_config(ch.config_json)
        incoming = decode_channel_config(payload["config_json"])
        merged = _merge_channel_config(current, incoming)
        _validate_channel_url(payload.get("channel_type") or ch.channel_type, merged)
        payload["config_json"] = encode_channel_config(merged)
    for k, v in payload.items():
        setattr(ch, k, v)
    db.commit()
    db.refresh(ch)
    return ch


def delete_channel(db: Session, channel_id: int):
    ch = get_channel(db, channel_id)
    if not ch:
        return False
    db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.channel_id == channel_id).delete()
    db.delete(ch)
    db.commit()
    return True


def test_channel(db: Session, data: ChannelTestMessage):
    ch = get_channel(db, data.channel_id)
    if not ch:
        raise ValueError("Channel not found")
    config = decode_channel_config(ch.config_json)
    adapter = get_adapter(ch.channel_type, config)
    errors = adapter.validate_config()
    if errors:
        return {"success": False, "errors": errors}
    result = adapter.send("", data.title, data.content)
    return {"success": result.success, "status_code": result.status_code,
            "response_body": _safe_text(result.response_body),
            "error_message": _safe_text(result.error_message)}


def list_policies(db: Session):
    policies = db.query(NotifyPolicy).order_by(NotifyPolicy.id).all()
    result = []
    for p in policies:
        links = db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.policy_id == p.id).all()
        result.append({
            "id": p.id, "policy_name": p.policy_name, "level_filter": p.level_filter,
            "metric_filter": p.metric_filter, "time_filter": p.time_filter,
            "throttle_minutes": p.throttle_minutes, "enabled": p.enabled,
            "channel_ids": [l.channel_id for l in links],
            "create_time": str(p.create_time), "update_time": str(p.update_time),
        })
    return result


def get_policy(db: Session, policy_id: int):
    p = db.query(NotifyPolicy).filter(NotifyPolicy.id == policy_id).first()
    if not p:
        return None
    links = db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.policy_id == policy_id).all()
    return {**p.__dict__, "channel_ids": [l.channel_id for l in links]}


def create_policy(db: Session, data: PolicyCreate):
    channel_ids = data.channel_ids
    d = data.model_dump(exclude={"channel_ids"})
    p = NotifyPolicy(**d)
    db.add(p)
    db.commit()
    db.refresh(p)
    for cid in channel_ids:
        db.add(NotifyPolicyChannel(policy_id=p.id, channel_id=cid))
    db.commit()
    return p


def update_policy(db: Session, policy_id: int, data: PolicyUpdate):
    p = db.query(NotifyPolicy).filter(NotifyPolicy.id == policy_id).first()
    if not p:
        return None
    channel_ids = data.channel_ids
    for k, v in data.model_dump(exclude_unset=True, exclude={"channel_ids"}).items():
        setattr(p, k, v)
    if channel_ids is not None:
        db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.policy_id == policy_id).delete()
        for cid in channel_ids:
            db.add(NotifyPolicyChannel(policy_id=policy_id, channel_id=cid))
    db.commit()
    db.refresh(p)
    return p


def delete_policy(db: Session, policy_id: int):
    p = db.query(NotifyPolicy).filter(NotifyPolicy.id == policy_id).first()
    if not p:
        return False
    db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.policy_id == policy_id).delete()
    db.query(NotifyThrottle).filter(NotifyThrottle.policy_id == policy_id).delete()
    db.delete(p)
    db.commit()
    return True


def list_templates(db: Session, channel_type: str = None):
    q = db.query(NotifyTemplate)
    if channel_type:
        q = q.filter(NotifyTemplate.channel_type == channel_type)
    return q.order_by(NotifyTemplate.id).all()


def create_template(db: Session, data: TemplateCreate):
    t = NotifyTemplate(**data.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def update_template(db: Session, template_id: int, data: TemplateUpdate):
    t = db.query(NotifyTemplate).filter(NotifyTemplate.id == template_id).first()
    if not t:
        return None
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return t


def delete_template(db: Session, template_id: int):
    t = db.query(NotifyTemplate).filter(NotifyTemplate.id == template_id).first()
    if not t:
        return False
    db.delete(t)
    db.commit()
    return True


def list_recipients(db: Session):
    return db.query(NotifyRecipient).order_by(NotifyRecipient.id).all()


def create_recipient(db: Session, data: RecipientCreate):
    r = NotifyRecipient(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def delete_recipient(db: Session, recipient_id: int):
    r = db.query(NotifyRecipient).filter(NotifyRecipient.id == recipient_id).first()
    if not r:
        return False
    db.delete(r)
    db.commit()
    return True


def list_events(db: Session, limit: int = 50):
    return db.query(NotifyEvent).order_by(desc(NotifyEvent.id)).limit(limit).all()


def create_event(db: Session, data: EventCreate):
    if (data.event_key or "").strip().startswith("report-artifact:"):
        raise ValueError("Report subscription events can only be created by the report service")
    return _create_event(db, data)


def _create_event(db: Session, data: EventCreate):
    event_key = (data.event_key or "").strip()
    if event_key:
        existing = db.query(NotifyEvent).filter(NotifyEvent.event_key == event_key).first()
        if existing:
            return existing

    payload = data.model_dump()
    payload["event_key"] = event_key or None
    payload["body"] = _safe_text(payload.get("body"))
    payload["extra_json"] = _safe_text(payload.get("extra_json"))
    e = NotifyEvent(**payload)
    db.add(e)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not event_key:
            raise
        existing = db.query(NotifyEvent).filter(NotifyEvent.event_key == event_key).first()
        if not existing:
            raise
        return existing
    db.refresh(e)
    return e


def _dispatch_counts(db: Session, log_ids: list[int]) -> dict:
    # Count only this invocation's new deliveries, using persisted outcomes.
    counts = dict(sent_count=0, attempted_count=0, pending_count=0,
                  failed_count=0, cancelled_count=0, unknown_count=0)
    status_counters = {"sent": "sent_count", "pending": "pending_count", "retrying": "pending_count",
                       "failed": "failed_count", "cancelled": "cancelled_count", "unknown": "unknown_count"}
    if log_ids:
        for status, attempts in db.query(NotifyLog.status, NotifyLog.attempt_count).filter(NotifyLog.id.in_(log_ids)):
            counts["attempted_count"] += int(attempts > 0)
            if status in status_counters:
                counts[status_counters[status]] += 1
    return counts


def dispatch_event(db: Session, event_id: int):
    event = db.query(NotifyEvent).filter(NotifyEvent.id == event_id).first()
    if not event:
        raise ValueError("Event not found")
    if (event.event_key or "").startswith("report-artifact:"):
        raise ValueError("Report subscription events require trusted subscription dispatch")

    policies = db.query(NotifyPolicy).filter(NotifyPolicy.enabled == 1).all()
    log_ids = []

    for policy in policies:
        if not _match_policy(policy, event):
            continue
        if not _check_throttle(db, policy, event):
            continue

        links = db.query(NotifyPolicyChannel).filter(NotifyPolicyChannel.policy_id == policy.id).all()
        for link in links:
            channel = get_channel(db, link.channel_id)
            if not channel or not channel.enabled:
                continue

            recipients = db.query(NotifyRecipient).filter(NotifyRecipient.channel_type == channel.channel_type).all()
            target_list = [r.address for r in recipients] if recipients else [""]

            claimed_delivery = False
            for recipient in target_list:
                log = NotifyLog(
                    event_id=event.id, policy_id=policy.id, channel_id=channel.id,
                    channel_type=channel.channel_type, recipient=recipient,
                    title=event.title, content=event.body,
                    status="pending",
                    attempt_count=0,
                    retry_count=0, max_retries=3,
                )
                db.add(log)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    continue

                claimed_delivery = True
                log_ids.append(log.id)
                _deliver_log(db, log.id)

            if claimed_delivery:
                _update_throttle(db, policy, event)
                db.commit()

    return {"event_id": event_id, **_dispatch_counts(db, log_ids)}


def _subscription_matches(subscription, schedule, artifact) -> bool:
    if not subscription.enabled or subscription.schedule_id not in (None, schedule.id):
        return False
    if subscription.report_types and "all" not in subscription.report_types and artifact.report_type not in subscription.report_types:
        return False
    for field in ("tenant_id", "organization_id"):
        if getattr(subscription, field) is not None and getattr(subscription, field) != getattr(artifact, field):
            return False
    if not artifact.source_key.startswith(f"schedule:{schedule.schedule_uid}:"):
        return False
    instance_ids = (subscription.filter_json or {}).get("instance_ids")
    if instance_ids:
        from app.services.report_center import artifact_instance_scope

        allowed = {int(value) for value in instance_ids if str(value).isdigit()}
        scope = artifact_instance_scope(artifact)
        if not scope or not scope.issubset(allowed):
            return False
    return True


def _subscription_channels(db: Session, subscription, event) -> list[NotifyChannel]:
    channels = db.query(NotifyChannel).order_by(NotifyChannel.id).all()
    selectors = {str(value).strip() for value in subscription.channels or []}
    if selectors:
        # The existing UI accepts IDs and aliases, including email for SMTP.
        selectors = {"smtp" if value == "email" else value for value in selectors}
        known = {value for channel in channels for value in (str(channel.id), channel.channel_name, channel.channel_type)}
        if selectors - known:
            raise ValueError("Subscription references an unknown notification channel")
        return [channel for channel in channels if channel.enabled == 1 and selectors.intersection(
            {str(channel.id), channel.channel_name, channel.channel_type}
        )]
    policy_ids = [policy.id for policy in db.query(NotifyPolicy).filter(NotifyPolicy.enabled == 1).all()
                  if _match_policy(policy, event)]
    channel_ids = {link.channel_id for link in db.query(NotifyPolicyChannel).filter(
        NotifyPolicyChannel.policy_id.in_(policy_ids)
    ).all()}
    return [channel for channel in channels if channel.enabled == 1 and channel.id in channel_ids]


def _subscription_targets(subscription, channel: NotifyChannel) -> list[str]:
    targets = sorted({value.strip() for value in subscription.recipients or []
                      if isinstance(value, str) and value.strip()})
    if channel.channel_type == "smtp":
        return [value for value in targets if re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+", value)]
    if channel.channel_type == "sms":
        return [value for value in targets if re.fullmatch(r"\+?[0-9]{5,20}", value)]
    # Robot channels address the configured room; recipients are mentions, not
    # a private audience. Empty targets are allowed only for explicit channels.
    return targets or ([""] if subscription.channels else [])


def dispatch_report_subscription(db: Session, subscription, schedule, artifact):
    from app.models.report_center import ReportArtifact, ReportSchedule, ReportSubscription

    # Re-read persistent objects. No EventCreate/extra_json field grants routing authority.
    subscription = db.query(ReportSubscription).populate_existing().filter(ReportSubscription.id == subscription.id).one()
    schedule = db.query(ReportSchedule).populate_existing().filter(ReportSchedule.id == schedule.id).one()
    artifact = db.query(ReportArtifact).populate_existing().filter(ReportArtifact.id == artifact.id).one()
    if not _subscription_matches(subscription, schedule, artifact):
        return _dispatch_counts(db, [])
    data = EventCreate(
        event_key=f"report-artifact:{artifact.artifact_uid}:subscription:{subscription.id}",
        event_type="report_generated", level="info",
        title=f"报告已生成：{artifact.title}",
        body=f"报告编号 {artifact.artifact_uid}，校验摘要 {artifact.content_sha256}。",
        extra_json=json.dumps({"artifact_id": artifact.id, "subscription_id": subscription.id}),
    )
    event = _create_event(db, data)
    if (event.event_type, event.title, event.body) != (data.event_type, data.title, data.body):
        raise ValueError("Existing report event does not match the trusted artifact")
    channels = _subscription_channels(db, subscription, event)
    targets = {channel.id: _subscription_targets(subscription, channel) for channel in channels}
    requested = {value.strip() for value in subscription.recipients or [] if isinstance(value, str) and value.strip()}
    resolved = {target for items in targets.values() for target in items}
    if channels and requested - resolved:
        raise ValueError("Subscription contains recipients unsupported by its notification channels")
    log_ids = []
    for channel in channels:
        for recipient in targets[channel.id]:
            # policy_id has no FK. Negative subscription IDs are disjoint from
            # real policies and non-NULL, preserving uq_notify_log_delivery.
            log = NotifyLog(event_id=event.id, policy_id=-subscription.id,
                            channel_id=channel.id, channel_type=channel.channel_type,
                            recipient=recipient, title=event.title, content=event.body,
                            status="pending", attempt_count=0, retry_count=0, max_retries=3)
            try:
                with db.begin_nested():
                    db.add(log)
                    db.flush()
                log_ids.append(log.id)
            except IntegrityError:
                continue
    # Persist the entire audience before any external side effect. A later tick
    # can finish pending targets after an interrupted dispatch.
    db.commit()
    for log_id in log_ids:
        _deliver_log(db, log_id)
    return {"event_id": event.id, **_dispatch_counts(db, log_ids)}


def _match_policy(policy: NotifyPolicy, event: NotifyEvent) -> bool:
    if policy.level_filter and policy.level_filter != "all":
        levels = [l.strip() for l in policy.level_filter.split(",")]
        if event.level not in levels:
            return False
    if policy.metric_filter:
        metrics = [m.strip() for m in policy.metric_filter.split(",")]
        if not event.metric_name or event.metric_name not in metrics:
            return False
    return True


def _check_throttle(db: Session, policy: NotifyPolicy, event: NotifyEvent) -> bool:
    key = f"{policy.id}:{event.event_type}:{event.level}"
    t = db.query(NotifyThrottle).filter(
        NotifyThrottle.policy_id == policy.id,
        NotifyThrottle.throttle_key == key,
    ).first()
    if t and t.last_sent_time:
        if datetime.now() - t.last_sent_time < timedelta(minutes=policy.throttle_minutes):
            return False
    return True


def _update_throttle(db: Session, policy: NotifyPolicy, event: NotifyEvent):
    key = f"{policy.id}:{event.event_type}:{event.level}"
    t = db.query(NotifyThrottle).filter(
        NotifyThrottle.policy_id == policy.id,
        NotifyThrottle.throttle_key == key,
    ).first()
    if t:
        t.last_sent_time = datetime.now()
    else:
        db.add(NotifyThrottle(policy_id=policy.id, throttle_key=key, last_sent_time=datetime.now()))


def list_logs(db: Session, channel_id: int = None, status: str = None, limit: int = 50):
    q = db.query(NotifyLog)
    if channel_id:
        q = q.filter(NotifyLog.channel_id == channel_id)
    if status:
        q = q.filter(NotifyLog.status == status)
    return q.order_by(desc(NotifyLog.id)).limit(limit).all()


MAX_ATTEMPTS = 3
RETRY_BATCH_SIZE = 100
IN_FLIGHT_TIMEOUT = timedelta(minutes=10)


def _delivery_block_reason(db: Session, log: NotifyLog, channel) -> str | None:
    if channel is None or channel.enabled != 1 or channel.channel_type != log.channel_type:
        return "Channel missing, disabled or changed"
    event = db.get(NotifyEvent, log.event_id) if log.event_id else None
    is_report = event is not None and (event.event_key or "").startswith("report-artifact:")
    if log.policy_id is not None and log.policy_id < 0:
        from app.models.report_center import ReportArtifact, ReportSchedule, ReportScheduleRun, ReportSubscription

        subscription = db.query(ReportSubscription).populate_existing().filter(ReportSubscription.id == -log.policy_id).first()
        if subscription is None or not is_report:
            return "Subscription or event missing"
        suffix = f":subscription:{subscription.id}"
        if not event.event_key.endswith(suffix):
            return "Subscription event mismatch"
        uid = event.event_key[len("report-artifact:"):-len(suffix)]
        artifact = db.query(ReportArtifact).filter(ReportArtifact.artifact_uid == uid).first()
        run = db.query(ReportScheduleRun).filter(ReportScheduleRun.artifact_id == artifact.id).first() if artifact else None
        schedule = db.get(ReportSchedule, run.schedule_id) if run else None
        if not schedule or not _subscription_matches(subscription, schedule, artifact):
            return "Subscription disabled or report outside subscription scope"
        try:
            selected_ids = {item.id for item in _subscription_channels(db, subscription, event)}
        except ValueError:
            return "Subscription channel selection is invalid"
        if channel.id not in selected_ids:
            return "Channel no longer selected by subscription"
        if log.recipient not in _subscription_targets(subscription, channel):
            return "Recipient no longer selected by subscription"
    elif is_report:
        # Legacy report logs were routed to global recipients. Never replay them.
        return "Legacy report delivery requires trusted subscription redispatch"
    elif log.policy_id is not None:
        policy = db.get(NotifyPolicy, log.policy_id)
        link = db.query(NotifyPolicyChannel).filter(
            NotifyPolicyChannel.policy_id == log.policy_id, NotifyPolicyChannel.channel_id == log.channel_id,
        ).first()
        if not policy or not policy.enabled or not link or not event or not _match_policy(policy, event):
            return "Notification policy no longer permits delivery"
        recipients = db.query(NotifyRecipient).filter(NotifyRecipient.channel_type == channel.channel_type).all()
        if log.recipient not in ([row.address for row in recipients] if recipients else [""]):
            return "Recipient no longer selected by policy"
    return None


def _deliver_log(db: Session, log_id: int, *, now: datetime | None = None, due_only: bool = False) -> bool:
    current = now or datetime.now()
    eligible = or_(
        and_(NotifyLog.status == "pending", NotifyLog.attempt_count == 0),
        and_(NotifyLog.status == "failed", NotifyLog.attempt_count < NotifyLog.max_retries,
             NotifyLog.attempt_count < MAX_ATTEMPTS),
    )
    query = db.query(NotifyLog).filter(NotifyLog.id == log_id, eligible)
    if due_only:
        query = query.filter(or_(NotifyLog.next_retry_time.is_(None), NotifyLog.next_retry_time <= current))
    # MySQL evaluates SET expressions left-to-right; read the old attempt count
    # before incrementing it so the first send does not count as a retry.
    claimed = query.update([
        (NotifyLog.retry_count, case((NotifyLog.attempt_count > 0, NotifyLog.retry_count + 1), else_=0)),
        (NotifyLog.attempt_count, NotifyLog.attempt_count + 1),
        (NotifyLog.status, "retrying"),
        (NotifyLog.next_retry_time, current + IN_FLIGHT_TIMEOUT),
    ], synchronize_session=False, update_args={"preserve_parameter_order": True})
    db.commit()
    if claimed != 1:
        return False
    log = db.query(NotifyLog).populate_existing().filter(NotifyLog.id == log_id).one()
    attempt = log.attempt_count
    channel = db.query(NotifyChannel).populate_existing().filter(NotifyChannel.id == log.channel_id).first()
    reason = _delivery_block_reason(db, log, channel)
    if reason:
        result = NotifyResult(False, error_message=reason)
        status = "cancelled"
    else:
        try:
            adapter = get_adapter(channel.channel_type, decode_channel_config(channel.config_json))
            result = adapter.send(log.recipient, log.title or "", log.content or "")
        except Exception as exc:
            result = NotifyResult(False, error_message=str(exc))
        status = "sent" if result.success else "failed"
    finished = now or datetime.now()
    retry_at = None
    if status == "failed" and attempt < min(log.max_retries, MAX_ATTEMPTS):
        retry_at = finished + timedelta(minutes=5 * 2 ** (attempt - 1))
    db.query(NotifyLog).filter(
        NotifyLog.id == log_id, NotifyLog.status == "retrying", NotifyLog.attempt_count == attempt,
    ).update({
        NotifyLog.status: status,
        NotifyLog.response_code: result.status_code,
        NotifyLog.response_body: _safe_text(result.response_body, 2000),
        NotifyLog.error_message: _safe_text(result.error_message, 2000),
        NotifyLog.sent_time: finished if result.success else None,
        NotifyLog.next_retry_time: retry_at,
    }, synchronize_session=False)
    db.commit()
    return True


def consume_due_notifications(db: Session, *, now: datetime | None = None, limit: int = RETRY_BATCH_SIZE) -> dict:
    current = now or datetime.now()
    limit = max(0, min(int(limit), RETRY_BATCH_SIZE))
    due = or_(NotifyLog.next_retry_time.is_(None), NotifyLog.next_retry_time <= current)
    query = db.query(NotifyLog.id, NotifyLog.status, NotifyLog.attempt_count).filter(or_(
        and_(NotifyLog.status == "pending", NotifyLog.attempt_count == 0, due),
        and_(NotifyLog.status == "failed", NotifyLog.attempt_count < NotifyLog.max_retries,
             NotifyLog.attempt_count < MAX_ATTEMPTS, due),
        and_(NotifyLog.status.in_(["pending", "retrying"]), NotifyLog.attempt_count > 0,
             or_(NotifyLog.next_retry_time <= current,
                 and_(NotifyLog.next_retry_time.is_(None), NotifyLog.update_time <= current - IN_FLIGHT_TIMEOUT))),
    )).order_by(NotifyLog.next_retry_time, NotifyLog.id).limit(limit)
    for attempt in range(5):
        try:
            candidates = query.all()
            db.commit()
            break
        except OperationalError as exc:
            db.rollback()
            code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or attempt == 4:
                raise
            # Shared-cache SQLite raises SQLITE_LOCKED even on SELECT and does
            # not honor busy_timeout. Retry only the side-effect-free snapshot.
            time.sleep(0.02 * 2 ** attempt)
    processed = 0
    for log_id, snapshot_status, snapshot_attempts in candidates:
        try:
            # An expired in-flight send has an ambiguous external outcome. Do not
            # steal it and risk duplicate delivery; operator reconciliation is required.
            unknown = 0
            if snapshot_status in {"pending", "retrying"} and snapshot_attempts > 0:
                unknown = db.query(NotifyLog).filter(
                    NotifyLog.id == log_id, NotifyLog.status.in_(["pending", "retrying"]),
                    NotifyLog.attempt_count > 0,
                    or_(NotifyLog.next_retry_time <= current,
                        and_(NotifyLog.next_retry_time.is_(None), NotifyLog.update_time <= current - IN_FLIGHT_TIMEOUT)),
                ).update({NotifyLog.status: "unknown", NotifyLog.next_retry_time: None,
                          NotifyLog.error_message: "Interrupted delivery; reconcile external outcome before resending"},
                         synchronize_session=False)
                db.commit()
            processed += int(bool(unknown) or _deliver_log(db, log_id, now=now, due_only=True))
        except Exception:
            db.rollback()
            logger.exception("Notification consumer failed | log_id=%s", log_id)
    return {"processed": processed}


def retry_log(db: Session, log_id: int) -> NotifyLog:
    log = db.query(NotifyLog).filter(NotifyLog.id == log_id).first()
    if not log:
        raise ValueError("Log not found")
    if log.status != "failed":
        raise ValueError("Only failed logs can be retried")
    if log.attempt_count >= min(log.max_retries, MAX_ATTEMPTS):
        raise ValueError("重试次数已达上限")
    if not _deliver_log(db, log_id):
        raise ValueError("Only failed logs can be retried")
    db.refresh(log)
    return log


def retry_failed(db: Session, log_id: int):
    log = retry_log(db, log_id)
    return {"success": log.status == "sent", "retry_count": log.retry_count}
