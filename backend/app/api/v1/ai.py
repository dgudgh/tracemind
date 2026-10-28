from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analyzers.ai_diagnosis import diagnose
from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.schemas.ai import AIDiagnosisRequest, AIDiagnosisResponse
from app.services.ai_degradation import AI_NOT_CONFIGURED, degradation, from_error_code, manual_for
from app.services.ai_diagnosis_contract import (
    inspection_row_to_dict as _inspection_row_to_dict,
    metric_evidence as _metric_evidence,
    model_diagnosis as _model_diagnosis,
)

router = APIRouter(prefix="/ai", tags=["AI 诊断"])


class EvidenceDiagnosisRequest(BaseModel):
    instance_id: int
    user_question: str | None = None
    limit: int = 20


@router.post("/diagnose", response_model=AIDiagnosisResponse)
def ai_diagnose(data: AIDiagnosisRequest):
    return diagnose(data)


def _fallback_local_result(data: AIDiagnosisRequest, inspection_error: str | None = None) -> dict:
    result = diagnose(data).model_dump()
    if inspection_error:
        known_risk = result["risk_level"] in {"warning", "critical"}
        result.update(risk_level=result["risk_level"] if known_risk else "unknown",
                      execution_status="partial" if known_risk else "failed",
                      data_quality="partial" if known_risk else "missing")
        result["summary"] = f"{data.instance} 巡检失败；仅保留输入中的已知风险 {result['risk_level']}，不能确认整体健康。"
        result["limitations"].append("实例巡检失败，未取得完整的本次巡检证据。")
        result["inspection_error"] = inspection_error
    return result


@router.post("/diagnose-ai")
def ai_diagnose_with_model(data: AIDiagnosisRequest, db: Session = Depends(get_db)):
    from app.models.ai_report import AIReport
    from app.services.ai_config import get_default_ai_config
    from app.services.inspection import dispatch as inspection_dispatch
    from app.services.operation_ai import _local_diagnose

    inspection_summary = None
    inspection_results = []
    inspection_error = None
    local_result = None
    instance_id = data.instance_id

    instance = None
    if instance_id is not None:
        instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
        if instance is None:
            raise HTTPException(status_code=404, detail="实例不存在")
        if data.db_type != instance.db_type:
            raise HTTPException(status_code=400, detail="诊断数据库类型与实例不匹配")
        if not instance.enabled:
            raise HTTPException(status_code=400, detail="实例已停用")
        data = data.model_copy(update={"instance": instance.instance_name, "db_type": instance.db_type})

    if instance:
        try:
            task_id, rows = inspection_dispatch.run_collecting_rows(
                db, instance, trigger_type="ai_diagnose",
            )
            total = len(rows)
            warning = len([r for r in rows if r.check_status == "warning"])
            critical = len([r for r in rows if r.check_status == "critical"])
            inspection_summary = {
                "task_id": task_id,
                "total": total,
                "warning": warning,
                "critical": critical,
            }
            inspection_results = [_inspection_row_to_dict(r) for r in rows]
            truth = _fallback_local_result(data.model_copy(update={"metrics": {"inspection": inspection_results}}))
            if truth["data_quality"] != "good":
                local_result = truth
            else:
                local_result = _local_diagnose(instance, total, warning, critical, rows)
                local_result.update({key: truth[key] for key in ("risk_level", "execution_status", "data_quality", "limitations")})
        except Exception as exc:
            inspection_error = str(exc)
            local_result = None

    if local_result is None:
        local_result = _fallback_local_result(data, inspection_error)

    ai_config = get_default_ai_config(db)
    ai_model_result = None
    ai_error = None
    ai_configured = ai_config is not None
    ai_inference = None

    if ai_config:
        prompt = f"""你是一名资深数据库运维专家，熟悉 Oracle、MySQL 和 MES 系统数据库诊断。
请基于以下诊断请求、本地规则诊断结果和结构化巡检明细，给出更深入的分析。

【数据库类型】{data.db_type}
【实例名称】{data.instance}
【时间范围】{data.time_range or "最近"}
【用户问题】{data.user_question or "数据库健康诊断"}
【输入指标】{data.metrics}
【巡检摘要】{inspection_summary or "未执行实例巡检"}
【巡检失败】{inspection_error or "无"}
【巡检异常】{[r for r in inspection_results if r.get("check_status") in ("warning", "critical")][:50]}
【本地规则诊断结果】{local_result}

请输出：
1. 总体结论
2. 风险等级（critical/warning/normal）
3. 根因分析
4. 对 MES 业务的影响
5. 处理建议
6. SQL 优化建议
7. 操作步骤
8. 回滚方案

要求：不要编造数据中不存在的指标。高风险操作只给建议，不要自动执行。"""
        evidence = _metric_evidence({"inspection": inspection_results} if inspection_summary else data.metrics)
        ai_model_result, ai_error, ai_inference = _model_diagnosis(ai_config, prompt, evidence, local_result)

    ai_state = (from_error_code(ai_error, local_result_available=True) if ai_configured
                else degradation(AI_NOT_CONFIGURED, local_result_available=True))

    ai_report = AIReport(
        instance_id=instance_id,
        db_type=data.db_type,
        title=f"AI诊断 - {data.instance}",
        user_question=data.user_question,
        summary=local_result.get("summary"),
        risk_level=local_result.get("risk_level"),
        diagnosis={
            "local_result": local_result,
            "inspection_summary": inspection_summary,
            "inspection_results": inspection_results,
            "inspection_error": inspection_error,
            "ai_model_result": ai_model_result,
            "ai_error": ai_error,
            "ai_configured": ai_configured,
            "ai_inference": ai_inference,
            "ai_provider": ai_config.provider_name if ai_config else None,
            "model_name": ai_config.model_name if ai_config else None,
        },
    )
    db.add(ai_report)
    db.commit()
    db.refresh(ai_report)

    return {
        "local_result": local_result,
        "inspection_summary": inspection_summary,
        "inspection_results": inspection_results,
        "inspection_error": inspection_error,
        "ai_model_result": ai_model_result,
        "ai_error": ai_error,
        "ai_configured": ai_configured,
        "ai_inference": ai_inference,
        "ai_provider": ai_config.provider_name if ai_config else None,
        "model_name": ai_config.model_name if ai_config else None,
        "degradation": ai_state,
        "manual": manual_for(ai_state, "diagnosis.evidence_review"),
        "ai_report_id": ai_report.id,
    }


@router.get("/evidence/{instance_id}")
def ai_evidence_chain(instance_id: int, limit: int = 20, db: Session = Depends(get_db)):
    from app.services.ai_evidence import build_evidence_chain

    chain = build_evidence_chain(db, instance_id, limit)
    if not chain:
        raise HTTPException(status_code=404, detail="实例不存在")
    return chain


@router.post("/evidence-diagnose")
def ai_evidence_diagnose(payload: EvidenceDiagnosisRequest, db: Session = Depends(get_db)):
    from app.models.ai_report import AIReport
    from app.services.ai_config import get_default_ai_config
    from app.services.ai_evidence import build_ai_prompt, build_evidence_chain, build_local_conclusion, is_supporting_evidence

    chain = build_evidence_chain(db, payload.instance_id, payload.limit)
    if not chain:
        raise HTTPException(status_code=404, detail="实例不存在")
    local_result = build_local_conclusion(chain, payload.user_question)
    evidence = {f"section-{section_index}-item-{index}": item
                for section_index, section in enumerate(chain.get("sections") or [])
                for index, item in enumerate(section.get("items") or [])
                if isinstance(item, dict) and is_supporting_evidence(item)}

    ai_config = get_default_ai_config(db)
    ai_model_result = None
    ai_error = None
    ai_inference = None
    if ai_config:
        ai_model_result, ai_error, ai_inference = _model_diagnosis(
            ai_config, build_ai_prompt(chain, local_result, payload.user_question), evidence, local_result,
        )

    inst = chain.get("instance") or {}
    ai_report = AIReport(
        instance_id=payload.instance_id,
        db_type=inst.get("db_type"),
        title=f"AI证据链诊断 - {inst.get('instance_name')}",
        user_question=payload.user_question,
        summary=local_result.get("summary"),
        risk_level=local_result.get("risk_level"),
        diagnosis={
            "local_result": local_result,
            "evidence_chain": chain,
            "ai_model_result": ai_model_result,
            "ai_error": ai_error,
            "ai_configured": ai_config is not None,
            "ai_inference": ai_inference,
            "ai_provider": ai_config.provider_name if ai_config else None,
            "model_name": ai_config.model_name if ai_config else None,
        },
    )
    db.add(ai_report)
    db.commit()
    db.refresh(ai_report)

    return {
        "local_result": local_result,
        "evidence_chain": chain,
        "ai_model_result": ai_model_result,
        "ai_error": ai_error,
        "ai_configured": ai_config is not None,
        "ai_inference": ai_inference,
        "ai_provider": ai_config.provider_name if ai_config else None,
        "model_name": ai_config.model_name if ai_config else None,
        "ai_report_id": ai_report.id,
    }
