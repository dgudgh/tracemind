from datetime import datetime
from decimal import Decimal

from sqlalchemy import DECIMAL, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ObservabilitySource(Base):
    __tablename__ = "observability_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="none")
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    collect_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ObservabilityMetricMapping(Base):
    __tablename__ = "observability_metric_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_metric: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    standard_metric: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    metric_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ObservabilityInstanceBinding(Base):
    __tablename__ = "observability_instance_binding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    host_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    host_ip: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    zabbix_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    zabbix_hostid: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    zabbix_host_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    prometheus_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    prometheus_target: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    business_system: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    binding_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    match_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=80)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ObservabilityCurrentMetric(Base):
    __tablename__ = "observability_current_metric"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    host_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    standard_metric: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    metric_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 4), nullable=True)
    metric_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    collect_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ok", index=True)


class ObservabilityMetricHistory(Base):
    __tablename__ = "observability_metric_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    host_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    standard_metric: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    metric_value: Mapped[Decimal | None] = mapped_column(DECIMAL(18, 4), nullable=True)
    metric_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metric_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ObservabilityAlertEvent(Base):
    __tablename__ = "observability_alert_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    alert_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    host_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(30), default="warning", index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    metric_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="firing", index=True)
    first_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    recover_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)


class ObservabilityCollectPolicy(Base):
    __tablename__ = "observability_collect_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    collect_type: Mapped[str] = mapped_column(String(50), nullable=False, default="minute_monitor")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    retry_times: Mapped[int] = mapped_column(Integer, default=1)
    low_peak_only: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ObservabilityCollectTask(Base):
    __tablename__ = "observability_collect_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    task_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    collect_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    task_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ObservabilityCollectLog(Base):
    __tablename__ = "observability_collect_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    log_level: Mapped[str] = mapped_column(String(20), default="info", index=True)
    log_type: Mapped[str] = mapped_column(String(50), default="collect", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ObservabilityQueryAudit(Base):
    """Stores bounded query metadata only; external log and trace bodies are never persisted."""

    __tablename__ = "observability_query_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    query_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="success", index=True)
    query_metadata: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ObservabilityDataQualityPolicy(Base):
    __tablename__ = "observability_data_quality_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    policy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    freshness_warn_seconds: Mapped[int] = mapped_column(Integer, default=180)
    freshness_critical_seconds: Mapped[int] = mapped_column(Integer, default=600)
    completeness_window_minutes: Mapped[int] = mapped_column(Integer, default=60)
    min_completeness_pct: Mapped[int] = mapped_column(Integer, default=90)
    max_clock_skew_seconds: Mapped[int] = mapped_column(Integer, default=60)
    cardinality_budget: Mapped[int] = mapped_column(Integer, default=5000)
    hot_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    warm_retention_days: Mapped[int] = mapped_column(Integer, default=30)
    archive_retention_days: Mapped[int] = mapped_column(Integer, default=180)
    anomaly_sensitivity: Mapped[Decimal] = mapped_column(DECIMAL(5, 2), default=3.0)
    # Cadence for the periodic consumer. Without these the policy has no schedule and the
    # evaluation only ever runs when someone opens the page (ISS-22).
    evaluate_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    retention_interval_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    #: Deleting samples is destructive, so the periodic job plans by default and only executes
    #: for scopes that opted in explicitly.
    retention_auto_execute: Mapped[int] = mapped_column(Integer, default=0)
    last_retention_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ObservabilityDataQualitySnapshot(Base):
    __tablename__ = "observability_data_quality_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    freshness_status: Mapped[str] = mapped_column(String(30), default="unknown")
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    lag_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completeness_pct: Mapped[Decimal | None] = mapped_column(DECIMAL(7, 2), nullable=True)
    missing_ratio: Mapped[Decimal | None] = mapped_column(DECIMAL(7, 4), nullable=True)
    clock_skew_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cardinality: Mapped[int] = mapped_column(Integer, default=0)
    cardinality_budget: Mapped[int] = mapped_column(Integer, default=0)
    expired_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
