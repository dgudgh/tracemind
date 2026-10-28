import json
import urllib.parse
import urllib.request
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.http_security import require_http_url
from app.models.observability import ObservabilitySource
from app.models.db_instance import DBInstance
from app.schemas.observability import ApmEvidenceRequest, FaultTimelineRequest, GrafanaExportRequest, LogQueryRequest, TraceQueryRequest
from app.services.observability import (
    alert_ingest_service,
    binding_service,
    collect_policy_service,
    data_quality_service,
    linkage_health_service,
    metric_mapping_service,
    prometheus_standard_service,
    source_service,
    zabbix_collector,
)
from app.services.observability import apm, grafana, log_query
from app.services.observability.collector_runner import run_collect_task
from app.services.observability.exporter_service import list_exporter_types
from app.services.observability.metric_normalizer import seed_default_mappings

router = APIRouter(prefix="/observability", tags=["可观测与采集中心"])


class SourcePayload(BaseModel):
    source_name: str
    source_type: str
    api_url: Optional[str] = None
    auth_type: Optional[str] = "none"
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    api_key: Optional[str] = None
    enabled: int = 1
    timeout_seconds: int = 10
    collect_interval_seconds: int = 60
    remark: Optional[str] = None

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return value
        return require_http_url(value).rstrip("/")


class MappingPayload(BaseModel):
    source_type: str = "custom"
    source_metric: str
    standard_metric: str
    metric_unit: Optional[str] = None
    description: Optional[str] = None
    enabled: int = 1


class BindingPayload(BaseModel):
    instance_id: int
    host_identifier: Optional[str] = None
    host_ip: Optional[str] = None
    zabbix_source_id: Optional[int] = None
    zabbix_hostid: Optional[str] = None
    zabbix_host_name: Optional[str] = None
    prometheus_source_id: Optional[int] = None
    prometheus_target: Optional[str] = None
    business_system: Optional[str] = None
    binding_status: str = "active"
    match_rule: Optional[str] = None
    confidence: int = 80
    enabled: int = 1
    raw_data: Optional[dict[str, Any]] = None


class PolicyPayload(BaseModel):
    policy_name: str
    source_type: str
    collect_type: str = "manual"
    interval_seconds: int = 300
    timeout_seconds: int = 10
    max_concurrency: int = 2
    enabled: int = 1
    retry_times: int = 1
    low_peak_only: int = 0


class RunTaskPayload(BaseModel):
    source_id: Optional[int] = None
    policy_id: Optional[int] = None
    task_name: Optional[str] = None
    collect_type: str = "manual"
    hostid: Optional[str] = None
    limit: Optional[int] = None


class PromQueryPayload(BaseModel):
    query: str
    time: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    step: Optional[str] = None
    source_id: Optional[int] = None
    store: bool = True


class MetricIngestPayload(BaseModel):
    source_type: str = "custom"
    source_id: Optional[int] = None
    instance_id: Optional[int] = None
    host_id: Optional[str] = None
    source_metric: Optional[str] = None
    standard_metric: Optional[str] = None
    metric_value: Optional[float | str] = None
    metric_unit: Optional[str] = None
    metric_time: Optional[str] = None
    status: str = "ok"


class DataQualityPolicyPayload(BaseModel):
    policy_name: str = "数据质量策略"
    source_id: Optional[int] = None
    source_type: Optional[str] = None
    freshness_warn_seconds: int = Field(default=180, ge=15, le=86400)
    freshness_critical_seconds: int = Field(default=600, ge=30, le=604800)
    completeness_window_minutes: int = Field(default=60, ge=5, le=10080)
    min_completeness_pct: int = Field(default=90, ge=1, le=100)
    max_clock_skew_seconds: int = Field(default=60, ge=0, le=86400)
    cardinality_budget: int = Field(default=5000, ge=1, le=100000000)
    hot_retention_days: int = Field(default=7, ge=1, le=3650)
    warm_retention_days: int = Field(default=30, ge=1, le=3650)
    archive_retention_days: int = Field(default=180, ge=1, le=3650)
    anomaly_sensitivity: float = Field(default=3.0, ge=0.5, le=10)
    enabled: int = Field(default=1, ge=0, le=1)


class DataQualityEvaluatePayload(BaseModel):
    source_id: Optional[int] = None
    persist: bool = True


class DataQualityRetentionPayload(BaseModel):
    source_id: Optional[int] = None
    dry_run: bool = True
    confirmation: Optional[str] = None


def _get_source_or_404(db: Session, source_id: int) -> ObservabilitySource:
    row = source_service.get_source(db, source_id)
    if not row:
        raise HTTPException(status_code=404, detail="observability source not found")
    return row


def _require_enabled_source(source: ObservabilitySource, allowed_types: set[str], label: str) -> None:
    if not source.enabled:
        raise HTTPException(status_code=400, detail=f"{label} source is disabled")
    if source.source_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"source is not a supported {label} provider")


@router.post("/fault-timeline")
def fault_timeline(payload: FaultTimelineRequest):
    try:
        return {"items": log_query.build_fault_timeline(payload.instance_id, payload.start_time, payload.end_time, payload.events)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    seed_default_mappings(db)
    sources = source_service.list_sources(db)
    alerts = alert_ingest_service.list_alerts(db, status="firing", limit=20)
    tasks = collect_policy_service.list_tasks(db, limit=20)
    current_metrics = metric_mapping_service.list_current_metrics(db, limit=20)
    return {
        "source_count": len(sources),
        "enabled_source_count": len([item for item in sources if item.get("enabled")]),
        "active_alert_count": len(alerts),
        "recent_task_count": len(tasks),
        "current_metric_count": len(current_metrics),
        "sources": sources[:10],
        "alerts": alerts[:10],
        "tasks": tasks[:10],
        "current_metrics": current_metrics[:10],
    }


@router.get("/data-quality")
def data_quality_overview(
    source_id: Optional[int] = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    try:
        return data_quality_service.evaluate_data_quality(db, source_id=source_id, persist=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/data-quality/evaluate")
def evaluate_data_quality(payload: DataQualityEvaluatePayload, db: Session = Depends(get_db)):
    try:
        return data_quality_service.evaluate_data_quality(
            db,
            source_id=payload.source_id,
            persist=payload.persist,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/data-quality/policies")
def data_quality_policies(db: Session = Depends(get_db)):
    return {"items": data_quality_service.list_policies(db)}


@router.put("/data-quality/policies")
def save_data_quality_policy(payload: DataQualityPolicyPayload, db: Session = Depends(get_db)):
    try:
        return data_quality_service.upsert_policy(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data-quality/history")
def data_quality_history(
    source_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    return {"items": data_quality_service.list_snapshots(db, source_id=source_id, limit=limit)}


@router.post("/data-quality/retention")
def execute_data_quality_retention(payload: DataQualityRetentionPayload, db: Session = Depends(get_db)):
    try:
        return data_quality_service.run_retention(
            db,
            source_id=payload.source_id,
            dry_run=payload.dry_run,
            confirmation=payload.confirmation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources")
def list_sources(
    source_type: Optional[str] = None,
    enabled: Optional[int] = Query(None, ge=0, le=1),
    db: Session = Depends(get_db),
):
    return {"items": source_service.list_sources(db, source_type=source_type, enabled=enabled)}


@router.post("/sources")
def create_source(payload: SourcePayload, db: Session = Depends(get_db)):
    return source_service.upsert_source(db, payload.model_dump())


@router.put("/sources/{source_id}")
def update_source(source_id: int, payload: SourcePayload, db: Session = Depends(get_db)):
    return source_service.upsert_source(db, payload.model_dump(exclude_unset=True), _get_source_or_404(db, source_id))


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source_service.delete_source(db, _get_source_or_404(db, source_id))
    return {"status": "success"}


@router.post("/sources/{source_id}/test")
def test_source(source_id: int, db: Session = Depends(get_db)):
    return source_service.test_source(db, _get_source_or_404(db, source_id))


@router.get("/metric-mappings")
def list_metric_mappings(source_type: Optional[str] = None, db: Session = Depends(get_db)):
    seed_default_mappings(db)
    return {"items": metric_mapping_service.list_mappings(db, source_type=source_type)}


@router.get("/standard-metrics")
def list_standard_metrics():
    return {"items": metric_mapping_service.standard_metrics()}


@router.post("/metric-mappings")
def create_metric_mapping(payload: MappingPayload, db: Session = Depends(get_db)):
    return metric_mapping_service.create_mapping(db, payload.model_dump())


@router.get("/bindings")
def list_instance_bindings(
    instance_id: Optional[int] = None,
    enabled: Optional[int] = Query(None, ge=0, le=1),
    db: Session = Depends(get_db),
):
    return {"items": binding_service.list_bindings(db, instance_id=instance_id, enabled=enabled)}


@router.post("/bindings")
def create_instance_binding(payload: BindingPayload, db: Session = Depends(get_db)):
    return binding_service.upsert_binding(db, payload.model_dump())


@router.put("/bindings/{binding_id}")
def update_instance_binding(binding_id: int, payload: BindingPayload, db: Session = Depends(get_db)):
    row = binding_service.get_binding(db, binding_id)
    if not row:
        raise HTTPException(status_code=404, detail="observability binding not found")
    return binding_service.upsert_binding(db, payload.model_dump(exclude_unset=True), row)


@router.delete("/bindings/{binding_id}")
def delete_instance_binding(binding_id: int, db: Session = Depends(get_db)):
    row = binding_service.get_binding(db, binding_id)
    if not row:
        raise HTTPException(status_code=404, detail="observability binding not found")
    binding_service.delete_binding(db, row)
    return {"status": "success"}


@router.post("/bindings/auto")
def auto_instance_bindings(db: Session = Depends(get_db)):
    return binding_service.auto_bind(db)


@router.get("/linkage-health")
def linkage_health(
    instance_id: int,
    probe_sources: int = Query(0, ge=0, le=1),
    probe_timeout_seconds: float = Query(2.0, ge=0.5, le=5.0),
    db: Session = Depends(get_db),
):
    try:
        return linkage_health_service.get_linkage_health(
            db,
            instance_id=instance_id,
            probe_sources=bool(probe_sources),
            probe_timeout_seconds=probe_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/metrics/current")
def current_metrics(
    source_type: Optional[str] = None,
    standard_metric: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    return {"items": metric_mapping_service.list_current_metrics(db, source_type=source_type, standard_metric=standard_metric, limit=limit)}


@router.get("/metrics/history")
def metric_history(
    standard_metric: Optional[str] = None,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    return {"items": metric_mapping_service.list_metric_history(db, standard_metric=standard_metric, limit=limit)}


@router.post("/metrics/ingest")
def ingest_metric(payload: MetricIngestPayload, db: Session = Depends(get_db)):
    return metric_mapping_service.ingest_metric(db, payload.model_dump())


@router.post("/metrics/ingest-batch")
async def ingest_metric_batch(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")
    return {"items": metric_mapping_service.ingest_metrics(db, items), "count": len(items)}


@router.get("/alerts")
def list_alerts(status: str = "all", limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    return {"items": alert_ingest_service.list_alerts(db, status=status, limit=limit)}


@router.post("/alerts/ingest")
async def ingest_alert(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    return alert_ingest_service.ingest_alert(db, payload)


@router.get("/policies")
def list_policies(source_type: Optional[str] = None, db: Session = Depends(get_db)):
    return {"items": collect_policy_service.list_policies(db, source_type=source_type)}


@router.post("/policies")
def create_policy(payload: PolicyPayload, db: Session = Depends(get_db)):
    return collect_policy_service.upsert_policy(db, payload.model_dump())


@router.put("/policies/{policy_id}")
def update_policy(policy_id: int, payload: PolicyPayload, db: Session = Depends(get_db)):
    row = collect_policy_service.get_policy(db, policy_id)
    if not row:
        raise HTTPException(status_code=404, detail="collect policy not found")
    return collect_policy_service.upsert_policy(db, payload.model_dump(exclude_unset=True), row)


@router.get("/tasks")
def list_tasks(limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    return {"items": collect_policy_service.list_tasks(db, limit=limit)}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    row = collect_policy_service.get_task(db, task_id)
    if not row:
        raise HTTPException(status_code=404, detail="collect task not found")
    return row


@router.post("/tasks/run")
def run_task(payload: RunTaskPayload, db: Session = Depends(get_db)):
    return run_collect_task(db, payload.model_dump())


@router.get("/exporters")
def exporter_types():
    return {"items": list_exporter_types()}


@router.get("/zabbix/config")
def zabbix_config(db: Session = Depends(get_db)):
    items = source_service.list_sources(db, source_type="zabbix")
    return items[0] if items else {}


@router.post("/zabbix/config")
async def save_zabbix_config(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    payload["source_type"] = "zabbix"
    payload["source_name"] = payload.get("source_name") or payload.get("config_name") or "Zabbix"
    payload["api_url"] = payload.get("api_url") or payload.get("base_url")
    return source_service.upsert_source(db, payload)


@router.post("/zabbix/test")
def zabbix_test(source_id: Optional[int] = None, db: Session = Depends(get_db)):
    row = _source_by_type(db, "zabbix", source_id)
    return source_service.test_source(db, row)


@router.post("/zabbix/sync-hosts")
def zabbix_sync_hosts(source_id: Optional[int] = None, db: Session = Depends(get_db)):
    row = _source_by_type(db, "zabbix", source_id)
    return run_collect_task(db, {"source_id": row.id, "task_name": "Zabbix Host sync", "collect_type": "zabbix_hosts"})


@router.post("/zabbix/sync-problems")
def zabbix_sync_problems(
    source_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    row = _source_by_type(db, "zabbix", source_id)
    return run_collect_task(
        db,
        {"source_id": row.id, "task_name": "Zabbix Problem sync", "collect_type": "zabbix_problems", "limit": limit},
    )


@router.post("/zabbix/sync-metrics")
def zabbix_sync_metrics(
    source_id: Optional[int] = None,
    hostid: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    row = _source_by_type(db, "zabbix", source_id)
    return run_collect_task(
        db,
        {"source_id": row.id, "task_name": "Zabbix Item metric sync", "collect_type": "zabbix_metrics", "hostid": hostid, "limit": limit},
    )


@router.get("/zabbix/hosts")
def zabbix_hosts(source_id: Optional[int] = None, db: Session = Depends(get_db)):
    try:
        row = _source_by_type(db, "zabbix", source_id)
    except HTTPException as exc:
        if source_id is None and exc.status_code == 404:
            return {"items": [], "warnings": ["Zabbix source not configured"]}
        raise
    try:
        hosts = zabbix_collector.api_call(
            row,
            "host.get",
            {
                "output": ["hostid", "host", "name", "status"],
                "selectInterfaces": ["ip", "dns", "port", "type", "main"],
                "sortfield": "name",
            },
            timeout_seconds=min(float(row.timeout_seconds or 10), 1.5),
        )
        return {"items": hosts or []}
    except Exception as exc:
        return {
            "items": [],
            "status": "degraded",
            "warnings": [f"Zabbix API read failed: {exc}"],
        }


@router.get("/zabbix/problems")
def zabbix_problems(db: Session = Depends(get_db)):
    return {"items": alert_ingest_service.list_alerts(db, status="all", limit=200, source_type="zabbix")}


@router.get("/zabbix/metrics")
def zabbix_metrics(db: Session = Depends(get_db)):
    return {"items": metric_mapping_service.list_current_metrics(db, source_type="zabbix", limit=200)}


@router.get("/prometheus/config")
def prometheus_config(db: Session = Depends(get_db)):
    items = source_service.list_sources(db, source_type="prometheus")
    return items[0] if items else {}


@router.post("/prometheus/config")
async def save_prometheus_config(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    payload["source_type"] = "prometheus"
    payload["source_name"] = payload.get("source_name") or payload.get("config_name") or "Prometheus"
    payload["api_url"] = payload.get("api_url") or payload.get("prometheus_url")
    return source_service.upsert_source(db, payload)


@router.post("/prometheus/test")
def prometheus_test(source_id: Optional[int] = None, db: Session = Depends(get_db)):
    row = _source_by_type(db, "prometheus", source_id)
    return source_service.test_source(db, row)


@router.post("/prometheus/query")
def prometheus_query(payload: PromQueryPayload, db: Session = Depends(get_db)):
    return _prometheus_readonly_query(db, payload, "query")


@router.post("/prometheus/query-range")
def prometheus_query_range(payload: PromQueryPayload, db: Session = Depends(get_db)):
    return _prometheus_readonly_query(db, payload, "query_range")


@router.post("/prometheus/sync-standard-metrics")
def prometheus_sync_standard_metrics(source_id: Optional[int] = None, db: Session = Depends(get_db)):
    row = _source_by_type(db, "prometheus", source_id)
    return prometheus_standard_service.sync_standard_metrics(db, row)


@router.post("/alertmanager/webhook")
async def alertmanager_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    return alert_ingest_service.ingest_alertmanager(db, payload)


@router.get("/alertmanager/alerts")
def alertmanager_alerts(db: Session = Depends(get_db)):
    return {"items": alert_ingest_service.list_alerts(db, status="all", limit=200)}


def _source_by_type(db: Session, source_type: str, source_id: int | None = None) -> ObservabilitySource:
    if source_id:
        row = _get_source_or_404(db, source_id)
        if row.source_type != source_type:
            raise HTTPException(status_code=400, detail=f"source_id {source_id} is {row.source_type}, expected {source_type}")
        return row
    row = db.query(ObservabilitySource).filter(ObservabilitySource.source_type == source_type).order_by(ObservabilitySource.id.desc()).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"{source_type} source not configured")
    return row


def _prometheus_readonly_query(db: Session, payload: PromQueryPayload, api_name: str) -> dict[str, Any]:
    source = _source_by_type(db, "prometheus", payload.source_id)
    if not source.api_url:
        raise HTTPException(status_code=400, detail="Prometheus api_url not configured")
    params = {"query": payload.query}
    if api_name == "query" and payload.time:
        params["time"] = payload.time
    if api_name == "query_range":
        params.update({"start": payload.start or "", "end": payload.end or "", "step": payload.step or "60s"})
    try:
        url = require_http_url(f"{source.api_url.rstrip('/')}/api/v1/{api_name}?{urllib.parse.urlencode(params)}")
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=source.timeout_seconds or 10) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
            stored = _store_prometheus_result(db, source, payload, result) if payload.store else []
            if stored:
                result["stored_metrics"] = stored
            return result
    except Exception as exc:
        return {"status": "failed", "message": str(exc), "query": payload.query}


def _store_prometheus_result(db: Session, source: ObservabilitySource, payload: PromQueryPayload, result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") or {}
    rows = data.get("result") or []
    stored: list[dict[str, Any]] = []
    for row in rows[:200]:
        metric_labels = row.get("metric") or {}
        source_metric = metric_labels.get("__name__") or payload.query
        host_id = metric_labels.get("instance") or metric_labels.get("host") or metric_labels.get("job")
        if row.get("value"):
            ts, value = row["value"]
            stored.append(metric_mapping_service.ingest_metric(db, {
                "source_type": "prometheus",
                "source_id": source.id,
                "host_id": host_id,
                "source_metric": source_metric,
                "metric_value": value,
                "metric_time": ts,
                "status": "ok",
            }))
        for pair in (row.get("values") or [])[-5:]:
            if len(pair) != 2:
                continue
            ts, value = pair
            stored.append(metric_mapping_service.ingest_metric(db, {
                "source_type": "prometheus",
                "source_id": source.id,
                "host_id": host_id,
                "source_metric": source_metric,
                "metric_value": value,
                "metric_time": ts,
                "status": "ok",
            }))
    return stored
