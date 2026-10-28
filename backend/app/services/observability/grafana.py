from __future__ import annotations

from typing import Any


TEMPLATE_KEYS = ("overview", "oracle_tablespace", "mysql_replication", "archive_jobs", "zabbix_push_failures")
_REFRESH_SECONDS = {"5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h"}
_TEMPLATES = {
    "overview": ("TraceMind Database Overview", "mesguard-db-overview", [("Database availability", "mesguard_database_up"), ("Slow SQL count", "mesguard_slow_sql_count")]),
    "oracle_tablespace": ("Oracle Tablespace", "mesguard-oracle-tablespace", [("Tablespace used %", "mesguard_oracle_tablespace_used_pct"), ("FRA used %", "mesguard_oracle_fra_used_pct")]),
    "mysql_replication": ("MySQL Replication", "mesguard-mysql-replication", [("Replication lag", "mesguard_mysql_replication_lag_seconds"), ("Replication healthy", "mesguard_mysql_replication_healthy")]),
    "archive_jobs": ("Archive Jobs", "mesguard-archive-jobs", [("Archive jobs", "mesguard_archive_job_status"), ("Backup jobs", "mesguard_backup_job_status")]),
    "zabbix_push_failures": ("Zabbix Push Failures", "mesguard-zabbix-push-failures", [("Push failures", "mesguard_zabbix_push_failed_total")]),
}


def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "uid": value[1],
            "title": value[0],
            "version": 1,
            "required_metrics": [metric for _, metric in value[2]],
        }
        for key, value in _TEMPLATES.items()
    ]


def build_dashboard(template_key: str, datasource_uid: str, refresh: str = "30s") -> dict[str, Any]:
    if template_key not in _TEMPLATES:
        raise ValueError("unknown dashboard template")
    if refresh not in _REFRESH_SECONDS:
        raise ValueError("refresh must be between 5s and 1h")
    if not datasource_uid or any(word in datasource_uid.lower() for word in ("password", "token", "secret")):
        raise ValueError("datasource uid is invalid")
    title, uid, panels = _TEMPLATES[template_key]
    return {
        "uid": uid,
        "title": title,
        "schemaVersion": 39,
        "version": 1,
        "timezone": "browser",
        "refresh": refresh,
        "tags": ["mesguard", "database", template_key],
        "panels": [_panel(index, panel_title, metric, datasource_uid) for index, (panel_title, metric) in enumerate(panels, start=1)],
    }


def _panel(index: int, title: str, metric: str, datasource_uid: str) -> dict[str, Any]:
    return {
        "id": index,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": datasource_uid},
        "gridPos": {"h": 8, "w": 12, "x": 0 if index % 2 else 12, "y": ((index - 1) // 2) * 8},
        "targets": [{"expr": metric, "legendFormat": "{{instance}}", "refId": "A"}],
    }
