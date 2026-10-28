"""Shared inspection catalogue mapping and opt-in atomic source-index publication."""
from app.services.inspection import result_writer, run_readback


class ReportIndexPublicationError(RuntimeError):
    code = "inspection_report_index_failed"


def source_item(item: dict) -> dict:
    """The existing unified report catalogue representation, not another report generator."""
    outcome = item.get("outcome") or "unknown"
    result_count = int(item.get("result_count") or 0)
    return {
        "id": f"inspection:{item['job_id']}",
        "report_type": "inspection",
        "source_id": item["job_id"],
        "instance_id": item.get("instance_id"),
        "instance_name": item.get("instance_name"),
        "db_type": item.get("db_type"),
        "title": f"巡检报告 {item.get('instance_name') or item.get('instance_id')}",
        "status": outcome,
        "risk_level": item.get("risk_level"),
        "score": item.get("score"),
        "summary": (
            f"结论 {outcome}"
            f"（{item.get('outcome_kind') or '未分类'}）"
            f"，总项 {result_count}"
            f"，严重 {(item.get('counts') or {}).get('critical', 0)}"
            f"，告警 {(item.get('counts') or {}).get('warning', 0)}"
        ),
        "create_time": item.get("finished_at") or item.get("created_at"),
        "export_url": (
            f"/api/v1/reports/inspections/{item.get('instance_id')}/{item['job_id']}/export-word"
            if result_count else None
        ),
    }


def sync_job_artifact(db, row):
    """Index only a flushed, published row set, using the caller's fenced transaction."""
    from app.services import report_center

    db.refresh(row)  # The caller flushed its terminal state; honor SQL timestamp precision.
    if row.stage != "completed" or row.status not in {"success", "partial", "failed"} or not row.finished_at:
        return None
    counts = result_writer.count_by_task_ids(db, [row.id])
    if not counts.get(row.id):
        return None
    summary = run_readback.run_summary(db, row, result_counts=counts)
    if summary["outcome"] not in {"success", "partial", "failed"} or not summary["results_published"]:
        return None
    try:
        artifacts = report_center.sync_source_artifacts(
            db, [source_item(summary)], actor=f"inspection:{row.requested_by or 'system'}", commit=False,
        )
        if (len(artifacts) != 1 or artifacts[0].id is None
                or artifacts[0].source_key != f"source:inspection:{row.id}"
                or artifacts[0].source_id != row.id or artifacts[0].instance_id != row.instance_id):
            raise ValueError("Missing inspection source artifact")
        return artifacts[0]
    except Exception as exc:
        raise ReportIndexPublicationError("Inspection report index publication failed") from exc
