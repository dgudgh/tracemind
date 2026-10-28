import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.models.oracle_awr import AwrReport
from app.services import oracle_awr
from app.services.ai_degradation import (
    AI_MODEL_FAILED,
    AI_MODEL_OK,
    AI_NOT_CONFIGURED,
    AI_NOT_SENT,
    degradation,
    manual_for,
)
from app.services.awr_environment_analyzer import collect_environment, get_environment_prompt, get_cpu_risk_level

logger = logging.getLogger("app.api.oracle_awr")
router = APIRouter(prefix="/oracle", tags=["Oracle AWR"])
DEFAULT_AWR_UPLOAD_MAX_MB = 50


def safe_upload_filename(filename: str | None) -> str:
    normalized = (filename or "awr_report.html").replace("\\", "/")
    basename = os.path.basename(normalized).strip()
    return basename if basename not in {"", ".", ".."} else "awr_report.html"


def awr_upload_limit_bytes() -> int:
    try:
        configured_mb = int(os.getenv("AWR_UPLOAD_MAX_MB", str(DEFAULT_AWR_UPLOAD_MAX_MB)))
    except ValueError:
        configured_mb = DEFAULT_AWR_UPLOAD_MAX_MB
    return max(1, min(configured_mb, 1024)) * 1024 * 1024


def copy_upload_with_limit(upload: UploadFile, target_path: str, max_bytes: int) -> int:
    total = 0
    try:
        with open(target_path, "wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"AWR 文件不能超过 {max_bytes // 1024 // 1024} MB")
                output.write(chunk)
        return total
    except Exception:
        if os.path.exists(target_path):
            os.remove(target_path)
        raise


class AwrExportRequest(BaseModel):
    dbid: str
    instance_number: int = 1
    begin_snap: int
    end_snap: int
    format: str = "HTML"
    report_type: str = "INSTANCE"


class AwrAnalyzeRequest(BaseModel):
    begin_snap: int
    end_snap: int


@router.get("/{instance_id}/awr/db-info")
def api_db_info(instance_id: int, db: Session = Depends(get_db)):
    return oracle_awr.get_awr_db_info(db, instance_id)


@router.get("/{instance_id}/awr/snapshots")
def api_snapshots(instance_id: int, days: int = 7, db: Session = Depends(get_db)):
    return {"items": oracle_awr.list_awr_snapshots(db, instance_id, days)}


@router.post("/{instance_id}/awr/export")
def api_export(instance_id: int, payload: AwrExportRequest, db: Session = Depends(get_db)):
    result = oracle_awr.export_awr_report(db, instance_id, payload.model_dump())
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Export failed"))
    return result


@router.get("/{instance_id}/awr/reports")
def api_list_reports(instance_id: int, db: Session = Depends(get_db)):
    return {"items": oracle_awr.list_awr_reports(db, instance_id)}


@router.get("/{instance_id}/awr/reports/{report_id}/download")
def api_download(instance_id: int, report_id: int, db: Session = Depends(get_db)):
    result = oracle_awr.get_awr_report_file(db, instance_id, report_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return FileResponse(
        path=result["file_path"],
        filename=result["file_name"],
        media_type="application/octet-stream",
    )


@router.get("/{instance_id}/awr/compare")
def api_awr_compare(instance_id: int, base_id: int, target_id: int, db: Session = Depends(get_db)):
    result = oracle_awr.compare_reports(db, instance_id, base_id, target_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class AwrCollectPolicyUpdate(BaseModel):
    enabled: Optional[int] = None
    interval_hours: Optional[int] = None
    days_window: Optional[int] = None
    report_format: Optional[str] = None
    report_type: Optional[str] = None
    auto_analyze: Optional[int] = None


def _serialize_policy(p, inst=None) -> dict:
    return {
        "id": p.id, "instance_id": p.instance_id,
        "instance_name": inst.instance_name if inst else None,
        "host": f"{inst.host}:{inst.port}" if inst else None,
        "enabled": p.enabled, "interval_hours": p.interval_hours,
        "days_window": p.days_window, "report_format": p.report_format,
        "report_type": p.report_type, "auto_analyze": p.auto_analyze,
        "last_run_time": str(p.last_run_time) if p.last_run_time else None,
        "next_run_time": str(p.next_run_time) if p.next_run_time else None,
        "last_status": p.last_status, "last_message": p.last_message,
    }


@router.put("/{instance_id}/awr/collect-policy")
def api_upsert_collect_policy(instance_id: int, payload: AwrCollectPolicyUpdate, db: Session = Depends(get_db)):
    from app.models.awr_collect_policy import DBAwrCollectPolicy
    from app.models.db_instance import DBInstance as _Inst
    inst = db.query(_Inst).filter(_Inst.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
    p = db.query(DBAwrCollectPolicy).filter(DBAwrCollectPolicy.instance_id == instance_id).first()
    if not p:
        p = DBAwrCollectPolicy(instance_id=instance_id)
        db.add(p)
    for field in ("enabled", "interval_hours", "days_window", "report_format", "report_type", "auto_analyze"):
        val = getattr(payload, field)
        if val is not None:
            setattr(p, field, val)
    if p.enabled and not p.next_run_time:
        p.next_run_time = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return _serialize_policy(p, inst)


@router.post("/{instance_id}/awr/collect-now")
def api_collect_now(instance_id: int, db: Session = Depends(get_db)):
    from app.models.db_instance import DBInstance as _Inst
    from app.services import awr_collect
    inst = db.query(_Inst).filter(_Inst.id == instance_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
    return awr_collect.run_collect(db, inst)


@router.get("/{instance_id}/awr/reports/{report_id}/detail")
def api_report_detail(instance_id: int, report_id: int, db: Session = Depends(get_db)):
    result = oracle_awr.get_report_detail(db, instance_id, report_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


_EXPORT_MEDIA = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "pdf": "application/pdf",
    "doc": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/{instance_id}/awr/reports/{report_id}/export")
def api_export_analysis(instance_id: int, report_id: int, format: str = "md", db: Session = Depends(get_db)):
    """Export the analysis report (md/pdf/txt/doc) and return it for download."""
    result = oracle_awr.export_analysis_report(db, instance_id, report_id, format)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    media_type = _EXPORT_MEDIA.get((format or "md").lower(), "application/octet-stream")
    return FileResponse(
        path=result["file_path"],
        filename=result["file_name"],
        media_type=media_type,
    )


@router.delete("/{instance_id}/awr/reports/{report_id}")
def api_delete_report(instance_id: int, report_id: int, db: Session = Depends(get_db)):
    result = oracle_awr.delete_awr_report(db, instance_id, report_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/{instance_id}/awr/environment")
def api_environment(instance_id: int, begin_snap: int = 0, end_snap: int = 0, db: Session = Depends(get_db)):
    from app.services import instances as instance_service
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    env = collect_environment(instance, begin_snap or None, end_snap or None)
    cpu_risk = get_cpu_risk_level(env)
    return {"environment": env, "cpu_risk": cpu_risk}


@router.post("/{instance_id}/awr/analyze")
def api_analyze(instance_id: int, payload: AwrAnalyzeRequest, db: Session = Depends(get_db)):
    from app.services import instances as instance_service
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    env = collect_environment(instance, payload.begin_snap, payload.end_snap)
    cpu_risk = get_cpu_risk_level(env)
    env_prompt = get_environment_prompt(env)

    analysis = {
        "environment": env,
        "cpu_risk": cpu_risk,
        "analysis_summary": {
            "overall_score": 100 - (90 - cpu_risk.get("score", 100)),
            "primary_bottleneck": "CPU" if cpu_risk["level"] in ("HIGH", "CRITICAL") else "待分析",
            "analysis_reliability": env.get("analysis_reliability", "MEDIUM"),
        },
        "degradation": degradation(AI_MODEL_OK, local_result_available=True),
    }

    # Read AWR report file content
    awr_content = ""
    try:
        report_record = db.query(AwrReport).filter(
            AwrReport.instance_id == instance_id,
            AwrReport.begin_snap == payload.begin_snap,
            AwrReport.end_snap == payload.end_snap,
            AwrReport.status == "success",
        ).order_by(AwrReport.id.desc()).first()
        if report_record and os.path.exists(report_record.file_path):
            with open(report_record.file_path, "r", encoding="utf-8", errors="ignore") as f:
                awr_content = f.read()
    except Exception as e:
        logger.warning("Read AWR file failed: %s", e)

    # Try AI analysis
    try:
        from app.services.ai_config import call_ai_provider, get_default_ai_config
        ai_config = get_default_ai_config(db)
        if ai_config and awr_content:
            # Truncate AWR content to ~30k chars for AI context
            awr_truncated = awr_content[:30000] if len(awr_content) > 30000 else awr_content
            prompt = f"""你是一个资深 Oracle DBA，擅长 AWR 报告分析和性能诊断。

请基于以下 AWR 报告内容和运行环境信息，输出专业诊断分析。

【运行环境】
{env_prompt}

【AWR 报告内容】
{awr_truncated}

请按以下结构输出：

1. 总体结论（性能概况）
2. 主要瓶颈（CPU / IO / 内存 / 等待事件）
3. Top SQL 分析（列出最耗资源的 SQL 及优化建议）
4. 等待事件分析（Top 等待事件及根因）
5. IO 分析（磁盘 IO 状况）
6. 内存分析（SGA/PGA 命中率）
7. 对业务的影响评估
8. 优化建议（按优先级排序）
9. 需要人工确认的风险点
10. 回滚方案

要求：
- 结合服务器类型（物理机/虚拟机）和 CPU 核数做归一化判断。
- 如果数据库刚启动，不要因 Buffer Hit 低直接判断为内存不足。
- 所有建议必须适合生产环境。
- 不要编造报告中不存在的数据。"""
            ai_result = call_ai_provider(ai_config, prompt)
            if ai_result:
                analysis["ai_analysis"] = ai_result
        elif not ai_config:
            analysis["ai_error"] = "AI 未配置，请在「AI 配置」中设置 AI 服务"
            analysis["degradation"] = degradation(AI_NOT_CONFIGURED, local_result_available=True)
        elif not awr_content:
            analysis["ai_error"] = "未找到对应的 AWR 报告文件，请先导出 AWR"
            analysis["degradation"] = degradation(AI_NOT_SENT, local_result_available=True,
                                                  reason_code="not_applicable")
    except Exception as e:
        logger.warning("AI analysis failed: %s", e)
        analysis["ai_error"] = "AI 分析失败，已保留本地 AWR 环境与风险结果。"
        analysis["degradation"] = degradation(AI_MODEL_FAILED, local_result_available=True,
                                              reason_code="provider_unavailable")

    # Reading the original AWR report is the model-free equivalent of this analysis.
    analysis["manual"] = manual_for(analysis["degradation"], "awr.report_read")
    return analysis


@router.post("/{instance_id}/awr/reports/{report_id}/local-analysis")
def api_local_analysis(instance_id: int, report_id: int, db: Session = Depends(get_db)):
    """Local AWR analysis using the built-in analysis engine (no AI needed)"""
    from app.services.awr import AWRReportParser, AWRAnalysisEngine, AWRRecommendationGenerator

    report_record = db.query(oracle_awr.AwrReport).filter(
        oracle_awr.AwrReport.id == report_id,
        oracle_awr.AwrReport.instance_id == instance_id,
        oracle_awr.AwrReport.status == "success",
    ).first()
    if not report_record:
        raise HTTPException(status_code=404, detail="Report not found")
    if not os.path.exists(report_record.file_path):
        raise HTTPException(status_code=404, detail="AWR file not found")

    try:
        # Step 1: Parse AWR HTML file
        parser = AWRReportParser(report_record.file_path)
        parsed_data = parser.parse()

        # Step 2: Analyze
        engine = AWRAnalysisEngine(parsed_data)
        analysis = engine.analyze()

        # Step 3: Generate recommendations
        recommender = AWRRecommendationGenerator(analysis)
        recommendations = recommender.generate_recommendations()

        # Step 4: Save results to database
        import json
        report_record.performance_score = float(analysis.get("performance_score", 0))
        report_record.cpu_score = float((analysis.get("cpu_analysis") or {}).get("score", 0))
        report_record.memory_score = float((analysis.get("memory_analysis") or {}).get("score", 0))
        report_record.io_score = float((analysis.get("io_analysis") or {}).get("score", 0))
        report_record.analysis_results = json.loads(json.dumps(analysis, default=str, ensure_ascii=False))
        report_record.recommendations = json.loads(json.dumps(recommendations, default=str, ensure_ascii=False))
        # Update db_info from parsed data
        db_info = parsed_data.get("database_info", {}) or {}
        snap_info = parsed_data.get("snapshot_info", {}) or {}
        if db_info.get("database_name"):
            report_record.db_name = db_info["database_name"]
        if db_info.get("instance_name"):
            report_record.instance_name = db_info["instance_name"]
        if db_info.get("host_name"):
            report_record.host_name = db_info["host_name"]
        db.commit()

        return {
            "status": "success",
            "report_id": report_id,
            "performance_score": analysis.get("performance_score", 0),
            "performance_level": analysis.get("performance_level", "Unknown"),
            "summary": analysis.get("summary", {}),
            "cpu_analysis": analysis.get("cpu_analysis", {}),
            "memory_analysis": analysis.get("memory_analysis", {}),
            "io_analysis": analysis.get("io_analysis", {}),
            "wait_analysis": analysis.get("wait_analysis", {}),
            "sql_analysis": analysis.get("sql_analysis", {}),
            "execution_plan_analysis": analysis.get("execution_plan_analysis", {}),
            "instance_efficiency_analysis": analysis.get("instance_efficiency_analysis", {}),
            "potential_issues": analysis.get("potential_issues", []),
            "recommendations": recommendations,
        }
    except Exception as e:
        logger.error("Local AWR analysis failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/awr/upload")
def upload_awr_file(instance_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload an AWR HTML/TXT file and auto-analyze"""
    from app.models import oracle_awr as model

    # Validate file extension
    from app.services import instances as instance_service

    if not instance_service.get_by_id(db, instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")

    filename = safe_upload_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "html"
    if ext not in ("html", "htm", "txt"):
        raise HTTPException(status_code=400, detail="Only HTML/HTM/TXT files supported")

    # Save file
    report_dir = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "awr")
    report_dir = os.path.normpath(report_dir)
    os.makedirs(report_dir, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    file_path = os.path.join(report_dir, unique_name)
    try:
        file_size = copy_upload_with_limit(file, file_path, awr_upload_limit_bytes())
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    # Create report record
    report = model.AwrReport(
        instance_id=instance_id,
        file_name=filename,
        file_path=file_path,
        file_size=file_size,
        status="success",
        report_format=ext.upper(),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    # Auto-analyze
    try:
        from app.services.awr import AWRReportParser, AWRAnalysisEngine, AWRRecommendationGenerator
        import json

        parser = AWRReportParser(file_path)
        parsed_data = parser.parse()
        engine = AWRAnalysisEngine(parsed_data)
        analysis = engine.analyze()
        recommender = AWRRecommendationGenerator(analysis)
        recommendations = recommender.generate_recommendations()

        report.performance_score = float(analysis.get("performance_score", 0))
        report.cpu_score = float((analysis.get("cpu_analysis") or {}).get("score", 0))
        report.memory_score = float((analysis.get("memory_analysis") or {}).get("score", 0))
        report.io_score = float((analysis.get("io_analysis") or {}).get("score", 0))
        report.analysis_results = json.loads(json.dumps(analysis, default=str, ensure_ascii=False))
        report.recommendations = json.loads(json.dumps(recommendations, default=str, ensure_ascii=False))
        db_info = parsed_data.get("database_info", {}) or {}
        if db_info.get("database_name"):
            report.db_name = db_info["database_name"]
        if db_info.get("instance_name"):
            report.instance_name = db_info["instance_name"]
        if db_info.get("host_name"):
            report.host_name = db_info["host_name"]
        db.commit()
        db.refresh(report)
    except Exception as e:
        logger.warning("Auto-analysis failed for uploaded file: %s", e)

    return {
        "id": report.id,
        "status": report.status,
        "performance_score": report.performance_score,
        "cpu_score": report.cpu_score,
        "memory_score": report.memory_score,
        "io_score": report.io_score,
    }


@router.get("/{instance_id}/awr/dashboard")
def awr_dashboard(instance_id: int, period_days: int = 30, db: Session = Depends(get_db)):
    """AWR analysis dashboard statistics"""
    from app.models import oracle_awr as model

    start_date = datetime.utcnow() - timedelta(days=min(period_days, 365))
    reports = db.query(model.AwrReport).filter(
        model.AwrReport.instance_id == instance_id,
        model.AwrReport.status == "success",
    ).all()

    analyzed = [r for r in reports if r.performance_score is not None]
    avg_score = sum(r.performance_score or 0 for r in analyzed) / len(analyzed) if analyzed else 0
    low_score = [r for r in analyzed if (r.performance_score or 0) < 70]

    trend = sorted(analyzed, key=lambda r: (r.create_time or datetime.min))
    trend_data = [
        {
            "date": str(r.create_time)[:19] if r.create_time else "",
            "file_name": r.file_name,
            "performance_score": r.performance_score,
            "cpu_score": r.cpu_score,
            "memory_score": r.memory_score,
            "io_score": r.io_score,
        }
        for r in trend
    ][-30:]

    return {
        "statistics": {
            "total_reports": len(reports),
            "analyzed_reports": len(analyzed),
            "avg_performance_score": round(avg_score, 1),
            "issues_count": len(low_score),
        },
        "trend_data": trend_data,
        "recent_issues": [
            {
                "id": r.id,
                "file_name": r.file_name,
                "performance_score": r.performance_score,
                "create_time": str(r.create_time) if r.create_time else None,
            }
            for r in sorted(low_score, key=lambda x: x.performance_score or 0)[:10]
        ],
    }
