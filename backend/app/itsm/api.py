from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
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
from app.itsm import service

router = APIRouter(prefix="/itsm", tags=["itsm"])


@router.get("/configs")
def api_list_configs(db: Session = Depends(get_db)):
    return [service.serialize_config(config) for config in service.list_configs(db)]


@router.post("/configs")
def api_create_config(data: ItsmConfigCreate, db: Session = Depends(get_db)):
    return service.serialize_config(service.create_config(db, data))


@router.get("/configs/{config_id}")
def api_get_config(config_id: int, db: Session = Depends(get_db)):
    c = service.get_config(db, config_id)
    if not c:
        raise HTTPException(404, "Config not found")
    return service.serialize_config(c)


@router.put("/configs/{config_id}")
def api_update_config(config_id: int, data: ItsmConfigUpdate, db: Session = Depends(get_db)):
    c = service.update_config(db, config_id, data)
    if not c:
        raise HTTPException(404, "Config not found")
    return service.serialize_config(c)


@router.delete("/configs/{config_id}")
def api_delete_config(config_id: int, db: Session = Depends(get_db)):
    if not service.delete_config(db, config_id):
        raise HTTPException(404, "Config not found")
    return {"ok": True}


@router.post("/configs/{config_id}/test")
def api_test_config(config_id: int, db: Session = Depends(get_db)):
    try:
        return service.test_config(db, config_id)
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.get("/configs/{config_id}/field-mappings")
def api_list_field_mappings(config_id: int, db: Session = Depends(get_db)):
    return service.list_field_mappings(db, config_id)


@router.post("/configs/{config_id}/field-mappings")
def api_create_field_mapping(config_id: int, data: FieldMappingCreate, db: Session = Depends(get_db)):
    return service.create_field_mapping(db, config_id, data)


@router.put("/field-mappings/{mapping_id}")
def api_update_field_mapping(mapping_id: int, data: FieldMappingUpdate, db: Session = Depends(get_db)):
    m = service.update_field_mapping(db, mapping_id, data)
    if not m:
        raise HTTPException(404, "Field mapping not found")
    return m


@router.post("/tickets/manual")
def api_create_ticket_manual(data: TicketCreate, db: Session = Depends(get_db)):
    try:
        return service.create_ticket(db, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.post("/tickets/from-alert")
def api_create_ticket_from_alert(data: TicketCreate, db: Session = Depends(get_db)):
    data.source_type = "ALERT"
    try:
        return service.create_ticket(db, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.post("/tickets/from-inspection")
def api_create_ticket_from_inspection(data: TicketCreate, db: Session = Depends(get_db)):
    data.source_type = "INSPECTION"
    try:
        return service.create_ticket(db, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.post("/tickets/from-ai-diagnosis")
def api_create_ticket_from_ai(data: TicketCreate, db: Session = Depends(get_db)):
    data.source_type = "AI_DIAGNOSIS"
    try:
        return service.create_ticket(db, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.get("/tickets")
def api_list_tickets(source_type: str = None, status: str = None, limit: int = 50, db: Session = Depends(get_db)):
    return [service.serialize_ticket(ticket) for ticket in service.list_tickets(db, source_type, status, limit)]


@router.get("/tickets/{ticket_id}")
def api_get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = service.get_ticket(db, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    return service.serialize_ticket(t)


@router.post("/tickets/{ticket_id}/sync-status")
def api_sync_ticket_status(ticket_id: int, db: Session = Depends(get_db)):
    try:
        return service.sync_ticket_status(db, ticket_id)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.post("/tickets/{ticket_id}/add-comment")
def api_add_comment(ticket_id: int, data: TicketComment, db: Session = Depends(get_db)):
    try:
        return service.add_comment(db, ticket_id, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.post("/tickets/{ticket_id}/close")
def api_close_ticket(ticket_id: int, data: TicketClose, db: Session = Depends(get_db)):
    try:
        ticket = service.close_ticket(db, ticket_id, data)
        return {"success": ticket.mesguard_status != "failed", "ticket": service.serialize_ticket(ticket)}
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))


@router.get("/tickets/{ticket_id}/logs")
def api_get_ticket_logs(ticket_id: int, db: Session = Depends(get_db)):
    return [service.serialize_sync_log(log) for log in service.get_ticket_logs(db, ticket_id)]


@router.get("/rules")
def api_list_rules(db: Session = Depends(get_db)):
    return service.list_rules(db)


@router.post("/rules")
def api_create_rule(data: TicketRuleCreate, db: Session = Depends(get_db)):
    return service.create_rule(db, data)


@router.put("/rules/{rule_id}")
def api_update_rule(rule_id: int, data: TicketRuleUpdate, db: Session = Depends(get_db)):
    r = service.update_rule(db, rule_id, data)
    if not r:
        raise HTTPException(404, "Rule not found")
    return r


@router.delete("/rules/{rule_id}")
def api_delete_rule(rule_id: int, db: Session = Depends(get_db)):
    if not service.delete_rule(db, rule_id):
        raise HTTPException(404, "Rule not found")
    return {"ok": True}


@router.post("/webhook/status-callback")
def api_webhook_callback(data: WebhookCallback, db: Session = Depends(get_db)):
    try:
        return service.handle_webhook_callback(db, data)
    except ValueError as e:
        raise HTTPException(400, service.redact_sensitive_text(str(e)))
    except Exception as e:
        raise HTTPException(500, service.redact_sensitive_text(str(e)))
