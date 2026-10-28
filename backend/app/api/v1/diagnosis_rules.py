from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.diagnosis_result import DiagnosisResult
from app.models.diagnosis_rule import DiagnosisRule
from app.schemas.diagnosis_rule import DiagnosisRuleCreate, DiagnosisRuleUpdate, DiagnosisRunRequest
from app.services.rule_engine import build_action_plan, ensure_default_diagnosis_rules, run_rule_diagnosis, serialize_result

router = APIRouter(prefix="/diagnosis", tags=["规则诊断中心"])


@router.get("/rules")
def list_rules(db: Session = Depends(get_db)):
    ensure_default_diagnosis_rules(db)
    rows = db.query(DiagnosisRule).order_by(DiagnosisRule.category, DiagnosisRule.rule_code).all()
    return {
        "items": [
            {
                "id": row.id,
                "rule_code": row.rule_code,
                "rule_name": row.rule_name,
                "category": row.category,
                "severity": row.severity,
                "enabled": row.enabled,
                "condition_json": row.condition_json,
                "action_json": row.action_json,
                "description": row.description,
            }
            for row in rows
        ]
    }


@router.post("/rules")
def create_rule(body: DiagnosisRuleCreate, db: Session = Depends(get_db)):
    exists = db.query(DiagnosisRule).filter(DiagnosisRule.rule_code == body.rule_code).first()
    if exists:
        raise HTTPException(status_code=400, detail="规则编码已存在")
    row = DiagnosisRule(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "rule_code": row.rule_code}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: DiagnosisRuleUpdate, db: Session = Depends(get_db)):
    row = db.query(DiagnosisRule).filter(DiagnosisRule.id == rule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="规则不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "rule_code": row.rule_code}


@router.post("/run")
def run_diagnosis(body: DiagnosisRunRequest, db: Session = Depends(get_db)):
    try:
        return run_rule_diagnosis(db, body.instance_id, persist=body.persist)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/results")
def list_results(
    instance_id: int | None = Query(None),
    severity: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(DiagnosisResult)
    if instance_id:
        query = query.filter(DiagnosisResult.instance_id == instance_id)
    if severity:
        query = query.filter(DiagnosisResult.severity == severity)
    rows = query.order_by(DiagnosisResult.create_time.desc()).limit(limit).all()
    return {"items": [serialize_result(row) for row in rows]}


@router.get("/results/{result_id}")
def get_result(result_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagnosisResult).filter(DiagnosisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="诊断结果不存在")
    return serialize_result(row)


@router.post("/results/{result_id}/create-ticket")
def create_ticket_plan(result_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagnosisResult).filter(DiagnosisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="诊断结果不存在")
    return {
        "status": "plan_only",
        "message": "已生成工单描述，请人工确认后提交。",
        "ticket": {
            "title": f"[{row.severity.upper()}] {row.rule_name} - {row.object_name or row.instance_name}",
            "description": "\n".join([
                f"实例：{row.instance_name}",
                f"对象：{row.object_type or '-'} / {row.object_name or '-'}",
                f"根因：{row.root_cause or '-'}",
                f"影响：{row.impact or '-'}",
                f"建议：{row.suggestion or '-'}",
                "证据：",
                *[f"- {item}" for item in (row.evidence_json or [])],
            ]),
        },
    }


@router.post("/results/{result_id}/action-plan")
def action_plan(result_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagnosisResult).filter(DiagnosisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="诊断结果不存在")
    return build_action_plan(row)


@router.post("/results/{result_id}/push-zabbix")
def push_zabbix_plan(result_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagnosisResult).filter(DiagnosisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="诊断结果不存在")
    return {
        "status": "plan_only",
        "message": "已生成 Zabbix 推送载荷，请确认后由告警联动模块推送。",
        "payload": {
            "alert_id": f"{row.rule_code}_{row.instance_id}_{row.object_name or 'instance'}",
            "status": 1 if row.severity in ("warning", "critical") else 0,
            "level": 5 if row.severity == "critical" else 3 if row.severity == "warning" else 0,
            "message": f"{row.instance_name} {row.rule_name}",
            "detail": {
                "rule_code": row.rule_code,
                "severity": row.severity,
                "object_type": row.object_type,
                "object_name": row.object_name,
                "suggestion": row.suggestion,
            },
        },
    }
