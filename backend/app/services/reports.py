import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.check_result import DBCheckResult

logger = logging.getLogger("app.services.reports")


def generate_markdown_report(db: Session, task_id: str, instance_id: int | None = None) -> str:
    logger.info("生成巡检报告 | task_id=%s | instance_id=%s", task_id, instance_id)
    query = db.query(DBCheckResult).filter(DBCheckResult.task_id == task_id)
    if instance_id:
        query = query.filter(DBCheckResult.instance_id == instance_id)
    rows = query.order_by(DBCheckResult.check_status.desc(), DBCheckResult.id.asc()).all()

    lines = [
        "# TraceMind AI 智能运维平台 巡检报告",
        "",
        f"- 任务ID：{task_id}",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 巡检项数：{len(rows)}",
        "",
        "| 分类 | 检查项 | 状态 | 指标 | 阈值 | 发现 | 建议 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {category} | {item} | {status} | {metric}={value} | {threshold} | {finding} | {suggestion} |".format(
                category=row.check_category or "",
                item=row.check_item or "",
                status=row.check_status or "",
                metric=row.metric_name or "",
                value=row.metric_value or "",
                threshold=row.threshold_value or "",
                finding=(row.finding or "").replace("|", "/"),
                suggestion=(row.suggestion or "").replace("|", "/"),
            )
        )
    return "\n".join(lines)
