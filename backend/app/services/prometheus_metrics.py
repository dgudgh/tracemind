from collections import Counter

from sqlalchemy.orm import Session

from app.models.capacity_policy import DBCapacityAlert
from app.models.db_instance import DBInstance
from app.models.external_monitoring import PrometheusAlertCache, ZabbixProblemCache, ZabbixPushLog
from app.services.platform_observability import render_runtime_metrics


def _label_value(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(**kwargs) -> str:
    if not kwargs:
        return ""
    pairs = ",".join(f'{key}="{_label_value(value)}"' for key, value in kwargs.items())
    return "{" + pairs + "}"


def render_metrics(db: Session) -> str:
    lines: list[str] = []
    lines.extend([
        "# HELP mesguard_instance_total Database instances managed by TraceMind AI 智能运维平台.",
        "# TYPE mesguard_instance_total gauge",
    ])
    counts = Counter(row.db_type for row in db.query(DBInstance).all())
    for db_type, count in sorted(counts.items()):
        lines.append(f"mesguard_instance_total{_labels(db_type=db_type)} {count}")

    lines.extend([
        "# HELP mesguard_capacity_alerts_open Open capacity alerts.",
        "# TYPE mesguard_capacity_alerts_open gauge",
    ])
    alert_counts = Counter(
        (row.db_type, row.level)
        for row in db.query(DBCapacityAlert).filter(DBCapacityAlert.status == "open").all()
    )
    for (db_type, level), count in sorted(alert_counts.items()):
        lines.append(f"mesguard_capacity_alerts_open{_labels(db_type=db_type, level=level)} {count}")

    lines.extend([
        "# HELP mesguard_zabbix_problems_firing Firing Zabbix problems cached in TraceMind AI 智能运维平台.",
        "# TYPE mesguard_zabbix_problems_firing gauge",
    ])
    zbx_counts = Counter(row.severity_name or str(row.severity or 0) for row in db.query(ZabbixProblemCache).filter(ZabbixProblemCache.status == "firing").all())
    for severity, count in sorted(zbx_counts.items()):
        lines.append(f"mesguard_zabbix_problems_firing{_labels(severity=severity)} {count}")

    lines.extend([
        "# HELP mesguard_zabbix_push_failed_total Failed Zabbix push log count.",
        "# TYPE mesguard_zabbix_push_failed_total gauge",
    ])
    failed_push = db.query(ZabbixPushLog).filter(ZabbixPushLog.push_status == "failed").count()
    lines.append(f"mesguard_zabbix_push_failed_total {failed_push}")

    lines.extend([
        "# HELP mesguard_prometheus_alerts_firing Firing Alertmanager alerts cached in TraceMind AI 智能运维平台.",
        "# TYPE mesguard_prometheus_alerts_firing gauge",
    ])
    prom_counts = Counter(row.severity or "unknown" for row in db.query(PrometheusAlertCache).filter(PrometheusAlertCache.alert_status == "firing").all())
    for severity, count in sorted(prom_counts.items()):
        lines.append(f"mesguard_prometheus_alerts_firing{_labels(severity=severity)} {count}")

    return "\n".join(lines) + "\n" + render_runtime_metrics(db)
