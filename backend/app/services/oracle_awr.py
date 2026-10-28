import logging
import os
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.oracle_awr import AwrReport
from app.services import instances as instance_service
from app.collectors.oracle.oracle_connection import oracle_connection

logger = logging.getLogger("app.services.oracle_awr")

REPORTS_DIR = os.path.join(os.getcwd(), "reports", "awr")
EXPORTS_DIR = os.path.join(os.getcwd(), "reports", "awr_exports")

EXPORT_FORMATS = ("md", "pdf", "txt", "doc")


def _get_instance(db: Session, instance_id: int) -> DBInstance:
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise ValueError("Instance not found")
    return obj


def get_awr_db_info(db: Session, instance_id: int) -> dict:
    instance = _get_instance(db, instance_id)
    result = {"db": {}, "instances": [], "error": None}
    try:
        with oracle_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT dbid, name, db_unique_name, database_role, open_mode FROM v$database")
                row = cur.fetchone()
                if row:
                    result["db"] = {
                        "dbid": row[0], "name": row[1],
                        "db_unique_name": row[2], "database_role": row[3], "open_mode": row[4],
                    }

                cur.execute("SELECT instance_number, instance_name, host_name, version, status FROM gv$instance ORDER BY instance_number")
                for row in cur.fetchall():
                    result["instances"].append({
                        "instance_number": row[0], "instance_name": row[1],
                        "host_name": row[2], "version": row[3], "status": row[4],
                    })
    except Exception as e:
        result["error"] = str(e)
    return result


def list_awr_snapshots(db: Session, instance_id: int, days: int = 7) -> list:
    instance = _get_instance(db, instance_id)
    items = []
    try:
        with oracle_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.snap_id, s.dbid, s.instance_number,
                           TO_CHAR(s.begin_interval_time, 'YYYY-MM-DD HH24:MI:SS'),
                           TO_CHAR(s.end_interval_time, 'YYYY-MM-DD HH24:MI:SS')
                    FROM dba_hist_snapshot s
                    WHERE s.begin_interval_time >= SYSDATE - :days
                    ORDER BY s.snap_id DESC, s.instance_number
                """, {"days": days})
                for row in cur.fetchall():
                    items.append({
                        "snap_id": row[0], "dbid": row[1],
                        "instance_number": row[2],
                        "begin_time": row[3], "end_time": row[4],
                    })
    except Exception as e:
        logger.error("list_awr_snapshots failed: %s", e)
    return items


def export_awr_report(db: Session, instance_id: int, payload: dict) -> dict:
    instance = _get_instance(db, instance_id)
    dbid = payload["dbid"]
    inst_num = payload.get("instance_number", 1)
    begin_snap = payload["begin_snap"]
    end_snap = payload["end_snap"]
    fmt = payload.get("format", "HTML").upper()

    if begin_snap >= end_snap:
        return {"status": "error", "message": "begin_snap must be less than end_snap"}

    report_type = payload.get("report_type", "INSTANCE")
    is_rac_global = report_type == "RAC_GLOBAL"

    if is_rac_global:
        func_name = "AWR_GLOBAL_REPORT_HTML" if fmt == "HTML" else "AWR_GLOBAL_REPORT_TEXT"
    else:
        func_name = "AWR_REPORT_HTML" if fmt == "HTML" else "AWR_REPORT_TEXT"
    ext = "html" if fmt == "HTML" else "txt"

    record = AwrReport(
        instance_id=instance_id, dbid=str(dbid), instance_number=inst_num if not is_rac_global else 0,
        report_type=report_type, report_format=fmt,
        begin_snap=begin_snap, end_snap=end_snap,
        status="exporting",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    try:
        content_lines = []
        with oracle_connection(instance) as conn:
            with conn.cursor() as cur:
                if is_rac_global:
                    cur.execute("""
                        SELECT output FROM TABLE(
                            DBMS_WORKLOAD_REPOSITORY.AWR_GLOBAL_REPORT_HTML(
                                :dbid, :inst, :bsnap, :esnap
                            )
                        )
                    """, {"dbid": int(dbid), "inst": None, "bsnap": begin_snap, "esnap": end_snap})
                else:
                    cur.execute(f"""
                        SELECT output FROM TABLE(
                            DBMS_WORKLOAD_REPOSITORY.{func_name}(:dbid, :inst, :bsnap, :esnap)
                        )
                    """, {"dbid": dbid, "inst": inst_num, "bsnap": begin_snap, "esnap": end_snap})
                for row in cur.fetchall():
                    if row[0]:
                        content_lines.append(row[0])

        content = "\n".join(content_lines)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_name = instance.instance_name or "db"
        if is_rac_global:
            file_name = f"AWR_{db_name}_RAC_GLOBAL_{begin_snap}_{end_snap}_{ts}.{ext}"
        else:
            file_name = f"AWR_{db_name}_inst{inst_num}_{begin_snap}_{end_snap}_{ts}.{ext}"

        os.makedirs(REPORTS_DIR, exist_ok=True)
        file_path = os.path.join(REPORTS_DIR, file_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        file_size = os.path.getsize(file_path)
        record.db_name = db_name
        record.instance_name = f"inst{inst_num}"
        record.file_name = file_name
        record.file_path = file_path
        record.file_size = file_size
        record.status = "success"
        db.commit()
        db.refresh(record)

        return {
            "status": "success",
            "message": "AWR report exported",
            "report_id": record.id,
            "file_name": file_name,
            "download_url": f"/api/v1/oracle/{instance_id}/awr/reports/{record.id}/download",
        }
    except Exception as e:
        record.status = "failed"
        record.error_message = str(e)
        db.commit()
        return {"status": "error", "message": f"Export failed: {e}"}


def list_awr_reports(db: Session, instance_id: int) -> list:
    rows = db.query(AwrReport).filter(
        AwrReport.instance_id == instance_id
    ).order_by(AwrReport.id.desc()).limit(50).all()
    return [{
        "id": r.id, "db_name": r.db_name, "dbid": r.dbid,
        "instance_number": r.instance_number, "instance_name": r.instance_name,
        "report_type": r.report_type, "report_format": r.report_format,
        "begin_snap": r.begin_snap, "end_snap": r.end_snap,
        "file_name": r.file_name, "file_size": r.file_size,
        "performance_score": r.performance_score,
        "cpu_score": r.cpu_score,
        "memory_score": r.memory_score,
        "io_score": r.io_score,
        "status": r.status, "error_message": r.error_message,
        "create_time": str(r.create_time) if r.create_time else "",
    } for r in rows]


def get_awr_report_file(db: Session, instance_id: int, report_id: int) -> dict:
    record = db.query(AwrReport).filter(
        AwrReport.id == report_id,
        AwrReport.instance_id == instance_id,
    ).first()
    if not record:
        return {"error": "Report not found"}
    if record.status != "success":
        return {"error": f"Report status: {record.status}"}
    if not os.path.exists(record.file_path):
        return {"error": "File not found on disk"}
    return {"file_path": record.file_path, "file_name": record.file_name}


def delete_awr_report(db: Session, instance_id: int, report_id: int) -> dict:
    record = db.query(AwrReport).filter(
        AwrReport.id == report_id,
        AwrReport.instance_id == instance_id,
    ).first()
    if not record:
        return {"error": "Report not found"}
    if record.file_path and os.path.exists(record.file_path):
        os.remove(record.file_path)
    db.delete(record)
    db.commit()
    return {"status": "deleted"}


def _ensure_analysis(db: Session, record: AwrReport) -> tuple:
    """Return (analysis_results, recommendations) for a report, running and
    persisting the local analysis engine if they are not stored yet."""
    if record.analysis_results and record.recommendations:
        return record.analysis_results, record.recommendations

    import json
    from app.services.awr import (
        AWRReportParser, AWRAnalysisEngine, AWRRecommendationGenerator,
    )

    if not record.file_path or not os.path.exists(record.file_path):
        raise ValueError("AWR 报告文件不存在，无法分析导出")

    parsed = AWRReportParser(record.file_path).parse()
    analysis = AWRAnalysisEngine(parsed).analyze()
    recommendations = AWRRecommendationGenerator(analysis).generate_recommendations()

    record.performance_score = float(analysis.get("performance_score", 0) or 0)
    record.cpu_score = float((analysis.get("cpu_analysis") or {}).get("score", 0) or 0)
    record.memory_score = float((analysis.get("memory_analysis") or {}).get("score", 0) or 0)
    record.io_score = float((analysis.get("io_analysis") or {}).get("score", 0) or 0)
    record.analysis_results = json.loads(json.dumps(analysis, default=str, ensure_ascii=False))
    record.recommendations = json.loads(json.dumps(recommendations, default=str, ensure_ascii=False))
    db.commit()
    db.refresh(record)
    return record.analysis_results, record.recommendations


def export_analysis_report(db: Session, instance_id: int, report_id: int, fmt: str = "md") -> dict:
    """Export the analysis report to md/pdf/txt/doc using AWRReportExporter.
    Returns {file_path, file_name} or {error}."""
    record = db.query(AwrReport).filter(
        AwrReport.id == report_id, AwrReport.instance_id == instance_id
    ).first()
    if not record:
        return {"error": "Report not found"}
    if record.status != "success":
        return {"error": f"Report status: {record.status}"}

    fmt = (fmt or "md").lower()
    if fmt not in EXPORT_FORMATS:
        return {"error": f"不支持的导出格式: {fmt}"}

    try:
        analysis, recommendations = _ensure_analysis(db, record)
    except Exception as e:
        logger.error("export_analysis_report analyze failed: %s", e)
        return {"error": str(e)}

    from app.services.awr import AWRReportExporter

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(record.file_name or f"AWR_{report_id}"))[0]
    filename = f"{base}_analysis"
    try:
        path = AWRReportExporter(analysis, recommendations).export(fmt, EXPORTS_DIR, filename)
    except Exception as e:
        logger.error("export_analysis_report export failed: %s", e)
        return {"error": f"导出失败: {e}"}

    if not path or not os.path.exists(path):
        return {"error": "导出未生成文件"}
    return {"file_path": path, "file_name": os.path.basename(path)}


def _num(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "").replace("%", "").strip()
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_metrics(ar: dict) -> dict:
    ar = ar or {}
    summ = ar.get("summary") or {}
    cpu = ar.get("cpu_analysis") or {}
    mem = ar.get("memory_analysis") or {}
    io = ar.get("io_analysis") or {}
    return {
        "performance_score": _num(ar.get("performance_score")),
        "cpu_score": _num(cpu.get("score")),
        "cpu_utilization": _num(cpu.get("cpu_utilization")),
        "memory_score": _num(mem.get("score")),
        "buffer_hit_ratio": _num(mem.get("buffer_hit_ratio")),
        "logical_reads_per_sec": _num(mem.get("logical_reads_per_sec")),
        "io_score": _num(io.get("score")),
        "physical_reads_per_sec": _num(io.get("physical_reads_per_sec")),
        "total_io_requests_per_sec": _num(io.get("total_io_requests_per_sec")),
        "db_time": _num(summ.get("db_time")),
        "aas": _num(summ.get("db_load")),
    }


_METRIC_LABELS = {
    "performance_score": ("综合评分", "higher"),
    "cpu_score": ("CPU评分", "higher"),
    "cpu_utilization": ("CPU利用率%", "lower"),
    "memory_score": ("内存评分", "higher"),
    "buffer_hit_ratio": ("缓冲命中率%", "higher"),
    "logical_reads_per_sec": ("逻辑读/秒", "lower"),
    "io_score": ("IO评分", "higher"),
    "physical_reads_per_sec": ("物理读/秒", "lower"),
    "total_io_requests_per_sec": ("IO请求/秒", "lower"),
    "db_time": ("DB Time(s)", "lower"),
    "aas": ("AAS(平均活跃会话)", "lower"),
}


def _sql_ids(ar: dict) -> set:
    sa = (ar or {}).get("sql_analysis") or {}
    ids = set()
    for key in ("top_elapsed_time_sql", "top_cpu_sql", "top_buffer_gets_sql"):
        for row in sa.get(key, []) or []:
            if isinstance(row, dict) and row.get("sql_id"):
                ids.add(row["sql_id"])
    return ids


def _wait_events(ar: dict) -> dict:
    wa = (ar or {}).get("wait_analysis") or {}
    out = {}
    for ev in wa.get("top_events", []) or []:
        if isinstance(ev, dict) and ev.get("event_name"):
            out[ev["event_name"]] = _num(ev.get("db_time_pct")) or _num(ev.get("wait_time")) or 0
    return out


def compare_reports(db: Session, instance_id: int, base_id: int, target_id: int) -> dict:
    base = db.query(AwrReport).filter(AwrReport.id == base_id, AwrReport.instance_id == instance_id).first()
    target = db.query(AwrReport).filter(AwrReport.id == target_id, AwrReport.instance_id == instance_id).first()
    if not base or not target:
        return {"error": "Report not found"}
    try:
        base_analysis, _ = _ensure_analysis(db, base)
        target_analysis, _ = _ensure_analysis(db, target)
    except Exception as e:
        return {"error": f"对比的报告需先完成分析（本地分析/导出报告）：{e}"}

    mb, mt = _extract_metrics(base_analysis), _extract_metrics(target_analysis)
    metrics = []
    for key, (label, better) in _METRIC_LABELS.items():
        b, t = mb.get(key), mt.get(key)
        delta = (t - b) if (b is not None and t is not None) else None
        pct = (delta / b * 100) if (delta is not None and b) else None
        if delta is None:
            direction = "flat"
        elif abs(delta) < 1e-9:
            direction = "flat"
        else:
            improved = (delta > 0) if better == "higher" else (delta < 0)
            direction = "up" if improved else "down"
        metrics.append({"key": key, "label": label, "base": b, "target": t,
                        "delta": round(delta, 2) if delta is not None else None,
                        "pct": round(pct, 1) if pct is not None else None,
                        "direction": direction})

    base_sql, target_sql = _sql_ids(base_analysis), _sql_ids(target_analysis)
    we_b, we_t = _wait_events(base_analysis), _wait_events(target_analysis)
    wait_changes = []
    for ev in sorted(set(we_b) | set(we_t)):
        b, t = we_b.get(ev), we_t.get(ev)
        wait_changes.append({"event": ev, "base": b, "target": t,
                             "delta": round((t or 0) - (b or 0), 2)})

    sb = mb.get("performance_score")
    st = mt.get("performance_score")
    verdict = "评分数据不足"
    if sb is not None and st is not None:
        d = st - sb
        if abs(d) < 1:
            verdict = f"性能基本持平（{sb:.0f} → {st:.0f}）"
        else:
            verdict = f"性能{'上升' if d > 0 else '下降'} {abs(d):.0f} 分（{sb:.0f} → {st:.0f}）"
    new_sql = sorted(target_sql - base_sql)
    if new_sql:
        verdict += f"；新增 Top SQL {len(new_sql)} 条（如 {new_sql[0]}）"

    def info(r):
        return {"id": r.id, "file_name": r.file_name, "begin_snap": r.begin_snap,
                "end_snap": r.end_snap, "performance_score": r.performance_score,
                "create_time": str(r.create_time) if r.create_time else ""}

    return {
        "base": info(base), "target": info(target),
        "verdict": verdict, "metrics": metrics, "wait_changes": wait_changes,
        "sql_added": new_sql, "sql_removed": sorted(base_sql - target_sql),
    }


def get_report_detail(db: Session, instance_id: int, report_id: int) -> dict:
    """Return full stored analysis_results + recommendations for a report."""
    record = db.query(AwrReport).filter(
        AwrReport.id == report_id, AwrReport.instance_id == instance_id
    ).first()
    if not record:
        return {"error": "Report not found"}
    return {
        "id": record.id,
        "file_name": record.file_name,
        "status": record.status,
        "db_name": record.db_name,
        "instance_name": record.instance_name,
        "host_name": record.host_name,
        "report_format": record.report_format,
        "begin_snap": record.begin_snap,
        "end_snap": record.end_snap,
        "performance_score": record.performance_score,
        "cpu_score": record.cpu_score,
        "memory_score": record.memory_score,
        "io_score": record.io_score,
        "analysis_results": record.analysis_results,
        "recommendations": record.recommendations,
        "create_time": str(record.create_time) if record.create_time else "",
    }
