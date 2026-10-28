from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
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
from app.notify import service

router = APIRouter(prefix="/notify", tags=["notify"])


@router.get("/channels")
def api_list_channels(channel_type: str = None, db: Session = Depends(get_db)):
    return [service.serialize_channel(channel) for channel in service.list_channels(db, channel_type)]


@router.post("/channels")
def api_create_channel(data: ChannelCreate, db: Session = Depends(get_db)):
    return service.serialize_channel(service.create_channel(db, data))


@router.get("/channels/{channel_id}")
def api_get_channel(channel_id: int, db: Session = Depends(get_db)):
    ch = service.get_channel(db, channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")
    return service.serialize_channel(ch)


@router.put("/channels/{channel_id}")
def api_update_channel(channel_id: int, data: ChannelUpdate, db: Session = Depends(get_db)):
    ch = service.update_channel(db, channel_id, data)
    if not ch:
        raise HTTPException(404, "Channel not found")
    return service.serialize_channel(ch)


@router.delete("/channels/{channel_id}")
def api_delete_channel(channel_id: int, db: Session = Depends(get_db)):
    if not service.delete_channel(db, channel_id):
        raise HTTPException(404, "Channel not found")
    return {"ok": True}


@router.post("/channels/test")
def api_test_channel(data: ChannelTestMessage, db: Session = Depends(get_db)):
    try:
        return service.test_channel(db, data)
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.get("/policies")
def api_list_policies(db: Session = Depends(get_db)):
    return service.list_policies(db)


@router.post("/policies")
def api_create_policy(data: PolicyCreate, db: Session = Depends(get_db)):
    return service.create_policy(db, data)


@router.get("/policies/{policy_id}")
def api_get_policy(policy_id: int, db: Session = Depends(get_db)):
    p = service.get_policy(db, policy_id)
    if not p:
        raise HTTPException(404, "Policy not found")
    return p


@router.put("/policies/{policy_id}")
def api_update_policy(policy_id: int, data: PolicyUpdate, db: Session = Depends(get_db)):
    p = service.update_policy(db, policy_id, data)
    if not p:
        raise HTTPException(404, "Policy not found")
    return p


@router.delete("/policies/{policy_id}")
def api_delete_policy(policy_id: int, db: Session = Depends(get_db)):
    if not service.delete_policy(db, policy_id):
        raise HTTPException(404, "Policy not found")
    return {"ok": True}


@router.get("/templates")
def api_list_templates(channel_type: str = None, db: Session = Depends(get_db)):
    return service.list_templates(db, channel_type)


@router.post("/templates")
def api_create_template(data: TemplateCreate, db: Session = Depends(get_db)):
    return service.create_template(db, data)


@router.put("/templates/{template_id}")
def api_update_template(template_id: int, data: TemplateUpdate, db: Session = Depends(get_db)):
    t = service.update_template(db, template_id, data)
    if not t:
        raise HTTPException(404, "Template not found")
    return t


@router.delete("/templates/{template_id}")
def api_delete_template(template_id: int, db: Session = Depends(get_db)):
    if not service.delete_template(db, template_id):
        raise HTTPException(404, "Template not found")
    return {"ok": True}


@router.get("/recipients")
def api_list_recipients(db: Session = Depends(get_db)):
    return service.list_recipients(db)


@router.post("/recipients")
def api_create_recipient(data: RecipientCreate, db: Session = Depends(get_db)):
    return service.create_recipient(db, data)


@router.delete("/recipients/{recipient_id}")
def api_delete_recipient(recipient_id: int, db: Session = Depends(get_db)):
    if not service.delete_recipient(db, recipient_id):
        raise HTTPException(404, "Recipient not found")
    return {"ok": True}


@router.get("/events")
def api_list_events(limit: int = 50, db: Session = Depends(get_db)):
    return [service.serialize_event(event) for event in service.list_events(db, limit)]


@router.post("/events")
def api_create_event(data: EventCreate, db: Session = Depends(get_db)):
    try:
        return service.serialize_event(service.create_event(db, data))
    except ValueError as exc:
        raise HTTPException(400, service.redact_sensitive_text(str(exc)))


@router.post("/events/{event_id}/dispatch")
def api_dispatch_event(event_id: int, db: Session = Depends(get_db)):
    try:
        return service.dispatch_event(db, event_id)
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.get("/logs")
def api_list_logs(channel_id: int = None, status: str = None, limit: int = 50, db: Session = Depends(get_db)):
    return [service.serialize_log(log) for log in service.list_logs(db, channel_id, status, limit)]


@router.post("/logs/{log_id}/retry")
def api_retry_failed(log_id: int, db: Session = Depends(get_db)):
    try:
        return service.retry_failed(db, log_id)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))
