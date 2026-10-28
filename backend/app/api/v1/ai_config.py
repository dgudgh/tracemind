from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.ai_config import AIProviderConfigCreate, AIProviderConfigUpdate, AIProviderTestRequest
from app.services.ai_config import (
    list_ai_configs,
    create_ai_config,
    update_ai_config,
    delete_ai_config,
    get_ai_config_by_id,
    test_ai_provider,
)

router = APIRouter(prefix="/ai-config", tags=["AI配置"])


@router.get("")
def list_configs(db: Session = Depends(get_db)):
    return {"items": list_ai_configs(db)}


@router.post("")
def create_config(payload: AIProviderConfigCreate, db: Session = Depends(get_db)):
    return create_ai_config(db, payload.model_dump())


@router.put("/{config_id}")
def update_config(config_id: int, payload: AIProviderConfigUpdate, db: Session = Depends(get_db)):
    obj = get_ai_config_by_id(db, config_id)
    if not obj:
        raise HTTPException(status_code=404, detail="AI config not found")
    return update_ai_config(db, obj, payload.model_dump(exclude_unset=True))


@router.delete("/{config_id}")
def delete_config(config_id: int, db: Session = Depends(get_db)):
    obj = get_ai_config_by_id(db, config_id)
    if not obj:
        raise HTTPException(status_code=404, detail="AI config not found")
    delete_ai_config(db, obj)
    return {"message": "AI config deleted"}


@router.post("/test")
def test_config(payload: AIProviderTestRequest):
    return test_ai_provider(payload.model_dump())
