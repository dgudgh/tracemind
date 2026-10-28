from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_bearer_token, get_token_info
from app.core.database import get_db
from app.models.inspection_job import DBInspectionJob
from app.models.inspection_policy import DBInspectionPolicy
from app.services import instances as instance_service
from app.services import run_status
from app.services.access_control import resolve_scope
from app.services.inspection import (
    custom_metric_service,
    dispatch,
    jobs,
    policy_service,
    report_builder,
    result_writer,
    run_readback,
    runner,
    template_exchange,
    template_runtime,
    template_versions,
)

router = APIRouter(prefix="/inspection", tags=["Unified Inspection Center"])
MAX_IDEMPOTENCY_KEY_LENGTH = 200


def _allowed_instance_ids(request: Request, db: Session) -> set[int] | None:
    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("role") == "admin":
        return None
    scope = resolve_scope(db, int(auth.get("user_id") or 0), minimum_access="read")
    return set(scope.native_instance_ids) if scope.restricted else None


def _requested_by(request: Request | None, db: Session) -> int | None:
    """The authenticated identity behind a manual run.

    This used to be ``None`` for every interactive run, which silently disabled
    ``jobs.validate_actor``: a user whose permission had been revoked could keep running
    inspections, and no run recorded who asked for it. The identity comes from the verified
    token (via ``AuthMiddleware``), never from the request body.

    ``request`` may be ``None`` when a scheduler or a test calls the function directly; an
    unknown actor stays ``None``, which is the pre-existing behaviour rather than a guess.
    """
    if request is None:
        return None
    auth = getattr(request.state, "auth", {}) or {}
    user_id = auth.get("user_id")
    if user_id in (None, "", 0, "0"):
        token = get_bearer_token(request.headers.get("Authorization", ""))
        info = get_token_info(token, db=db) if token else None
        user_id = (info or {}).get("user_id")
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        return None
    # 0 is the ephemeral MES identity: authenticated but not a platform actor.
    return value if value > 0 else None


def _load_job_for_request(db: Session, request: Request, job_id: str) -> DBInspectionJob:
    """404 for "no such run", 403 for "not yours". Never an empty object."""
    row = db.get(DBInspectionJob, job_id)
    allowed = _allowed_instance_ids(request, db)
    if row is None:
        raise HTTPException(status_code=404, detail="巡检运行不存在")
    if allowed is not None and row.instance_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该巡检运行")
    return row



class InspectionPolicyPayload(BaseModel):
    policy_key: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    db_types: list[str] = Field(default_factory=list)
    enabled: bool = True
    schedule: str | None = None
    schedule_type: str | None = Field(default=None, pattern="^(manual|daily|weekdays|weekly|monthly|quarterly)$")
    schedule_day: int | None = Field(default=None, ge=1, le=31)
    schedule_days: list[int] | None = None
    schedule_month: int | None = Field(default=None, ge=1, le=3)
    schedule_time: str | None = Field(default=None, max_length=5)
    instance_ids: list[int] | None = None
    categories: list[str] = Field(default_factory=list)
    item_config: list[dict[str, Any]] = Field(default_factory=list)
    threshold_config: dict[str, Any] = Field(default_factory=dict)
    action_config: list[str] = Field(default_factory=list)
    report_config: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class InspectionPolicyUpdate(BaseModel):
    policy_key: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    db_types: list[str] | None = None
    enabled: bool | None = None
    schedule: str | None = None
    schedule_type: str | None = Field(default=None, pattern="^(manual|daily|weekdays|weekly|monthly|quarterly)$")
    schedule_day: int | None = Field(default=None, ge=1, le=31)
    schedule_days: list[int] | None = None
    schedule_month: int | None = Field(default=None, ge=1, le=3)
    schedule_time: str | None = Field(default=None, max_length=5)
    instance_ids: list[int] | None = None
    categories: list[str] | None = None
    item_config: list[dict[str, Any]] | None = None
    threshold_config: dict[str, Any] | None = None
    action_config: list[str] | None = None
    report_config: dict[str, Any] | None = None
    remark: str | None = None


class InspectionPolicyClone(BaseModel):
    policy_key: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)


class InspectionPolicyExportBatch(BaseModel):
    policy_ids: list[int] = Field(..., min_length=1, max_length=100)


class CustomMetricTestPayload(BaseModel):
    instance_id: int = Field(..., gt=0)
    metric: dict[str, Any]


class InspectionRunPayload(BaseModel):
    policy_key: str | None = Field(default=None, min_length=1, max_length=100)


def _raise_exchange_error(exc: template_exchange.TemplateExchangeError) -> None:
    too_large_codes = {
        "json_too_large",
        "zip_too_large",
        "zip_expanded_too_large",
        "too_many_templates",
    }
    raise HTTPException(status_code=413 if exc.code in too_large_codes else 400, detail=str(exc))


def _raise_custom_metric_error(exc: custom_metric_service.CustomMetricError) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_template_runtime_error(exc: template_runtime.TemplateRuntimeError) -> None:
    raise HTTPException(
        status_code=400,
        detail={
            "code": exc.code,
            "message": str(exc),
            "runtime_summary": exc.summary,
        },
    ) from exc


def _download_response(artifact: template_exchange.ExportArtifact) -> Response:
    encoded_filename = quote(artifact.filename)
    fallback_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", artifact.filename).strip("._-")
    fallback_filename = fallback_filename or "inspection-template-download"
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{encoded_filename}"
            )
        },
    )


def _validate_selection_ids(values: list[str] | None) -> set[str] | None:
    if values is None:
        return None
    if len(values) > template_exchange.MAX_TEMPLATE_FILES:
        raise HTTPException(status_code=400, detail="选择的模板数量超过限制")
    normalized = set(values)
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in normalized):
        raise HTTPException(status_code=400, detail="导入选择项格式无效，请重新预览")
    return normalized


async def _read_template_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if len(files) > template_exchange.MAX_TEMPLATE_FILES:
        for upload in files:
            await upload.close()
        raise HTTPException(
            status_code=413,
            detail=f"单次最多上传 {template_exchange.MAX_TEMPLATE_FILES} 个模板文件",
        )
    uploads: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        for upload in files:
            filename = upload.filename or "unnamed"
            limit = (
                template_exchange.MAX_ZIP_BYTES
                if filename.lower().endswith(".zip")
                else template_exchange.MAX_JSON_BYTES
            )
            content = await upload.read(limit + 1)
            total_bytes += len(content)
            if total_bytes > template_exchange.MAX_EXPANDED_BYTES:
                raise HTTPException(status_code=413, detail="上传文件总大小不能超过 50 MB")
            uploads.append((filename, content))
    finally:
        for upload in files:
            await upload.close()
    return uploads


@router.post("/{db_type}/{instance_id}/run")
def api_run_inspection(
    db_type: str,
    instance_id: int,
    request: Request,
    payload: InspectionRunPayload | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    instance = _get_instance(db, db_type, instance_id)
    idempotency_key = _text(idempotency_key)
    if idempotency_key is not None and len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(status_code=400, detail="Idempotency-Key 过长")
    policy = None
    if payload and payload.policy_key:
        policy = policy_service.get_policy_model_by_key(db, payload.policy_key)
        if not policy:
            raise HTTPException(status_code=404, detail="巡检模板不存在")
        if not policy.enabled:
            raise HTTPException(status_code=400, detail="巡检模板已停用")
        applicable_db_types = {
            str(value).strip().lower()
            for value in (policy.db_types_json or [])
            if str(value).strip()
        }
        if db_type not in applicable_db_types:
            raise HTTPException(
                status_code=400,
                detail=f"巡检模板“{policy.name}”不适用于 {db_type}",
            )
        try:
            template_runtime.require_template_runtime(
                policy.db_types_json or [],
                policy.item_config_json or [],
                policy.report_config_json or {},
            )
        except template_runtime.TemplateRuntimeError as exc:
            _raise_template_runtime_error(exc)
    try:
        return dispatch.run_through_queue(
            db, instance, policy=policy, trigger_type="manual",
            requested_by=_requested_by(request, db),
            idempotency_key=(idempotency_key or None),
        )
    except jobs.JobError as exc:
        # A refusal is a client-visible conflict, not an unhandled 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


JOB_STATUSES = (
    "queued", "running", "success", "partial", "failed",
    "cancelled", "timed_out", "interrupted", "expired",
)


def _text(value) -> str | None:
    """Coerce a FastAPI parameter default (``Query(...)``/``Header(...)``) to ``None``.

    Direct callers - tests, the scheduler, scripts - invoke these handlers without the HTTP
    layer, so an unset parameter is still the marker object rather than ``None``. Treating a
    marker as a real value would silently filter on a stringified ``Query`` object.
    """
    return value if isinstance(value, str) else None


def _csv(value) -> list[str]:
    return [item.strip() for item in (_text(value) or "").split(",") if item.strip()]


@router.get("/jobs")
def api_list_inspection_jobs(
    request: Request,
    instance_id: int | None = None,
    db_type: str | None = Query(default=None, pattern="^(oracle|mysql|postgresql|sqlserver|host)$"),
    status: str | None = None,
    outcome: str | None = None,
    trigger_type: str | None = Query(default=None, max_length=20),
    include_legacy: bool = False,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """The run queue: job rows are the source, so zero-result runs are visible."""
    if page < 1:
        raise HTTPException(status_code=400, detail="page 必须大于等于 1")
    if not 1 <= page_size <= 200:
        raise HTTPException(status_code=400, detail="page_size 必须在 1 到 200 之间")
    statuses = _csv(status)
    invalid_statuses = [value for value in statuses if value not in JOB_STATUSES]
    if invalid_statuses:
        # 400 rather than a silent empty list: a typo in a filter must not look like "no runs".
        raise HTTPException(
            status_code=400,
            detail=f"未知的任务状态：{invalid_statuses}；可用值 {'/'.join(JOB_STATUSES)}",
        )
    outcomes = _csv(outcome)
    invalid_outcomes = [value for value in outcomes if value not in run_status.OUTCOMES]
    if invalid_outcomes:
        raise HTTPException(
            status_code=400,
            detail=f"未知的运行结论：{invalid_outcomes}；可用值 {'/'.join(sorted(run_status.OUTCOMES))}",
        )
    allowed = _allowed_instance_ids(request, db)
    rows, total = jobs.list_jobs(
        db, instance_ids=allowed, instance_id=instance_id, db_type=_text(db_type),
        statuses=statuses, outcomes=outcomes, trigger_type=_text(trigger_type),
        page=page, page_size=page_size,
    )
    job_ids = [row.id for row in rows]
    result_counts = result_writer.count_by_task_ids(db, job_ids)
    severity_counts = result_writer.severity_counts_by_task_ids(db, job_ids)
    items = [
        run_readback.run_summary(
            db, row, result_counts=result_counts, severity_counts=severity_counts,
        )
        for row in rows
    ]
    if include_legacy:
        known = set(job_ids)
        legacy = [
            run_readback.legacy_summary(item)
            for item in result_writer.list_tasks(
                db, instance_id, db_type, page_size + len(known),
                allowed_instance_ids=allowed,
            )
            if item.get("task_id") not in known
        ]
        items.extend(legacy[:page_size])
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "outcome_vocabulary": sorted(run_status.OUTCOMES),
        "declared_outcomes_are_explicit": sorted(run_status.DECLARED_OUTCOMES),
    }


@router.get("/jobs/{job_id}")
def api_get_inspection_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """One run, complete: plan snapshot, steps, results, evidence and report index."""
    _load_job_for_request(db, request, job_id)
    payload = run_readback.build_run_read(db, job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="巡检运行不存在")
    return payload


@router.post("/jobs/{job_id}/cancel")
def api_cancel_inspection_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Request cancellation. A running job keeps running; only the request is recorded."""
    row = _load_job_for_request(db, request, job_id)
    try:
        jobs.cancel(db, row, requested_by=_requested_by(request, db))
    except jobs.JobError as exc:
        raise HTTPException(
            status_code=409 if exc.code == "not_cancellable" else 400, detail=str(exc)
        ) from exc
    payload = run_readback.build_run_read(db, job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="巡检运行不存在")
    return payload


@router.post("/jobs/{job_id}/retry")
def api_retry_inspection_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Queue one more attempt, inheriting the parent's frozen template snapshot."""
    row = _load_job_for_request(db, request, job_id)
    try:
        child = jobs.retry(db, row, requested_by=_requested_by(request, db))
    except jobs.JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = run_readback.build_run_read(db, child.id)
    if payload is None:
        raise HTTPException(status_code=404, detail="巡检运行不存在")
    payload["retry_of"] = row.id
    return payload


@router.get("/tasks")
def api_list_tasks(
    request: Request,
    instance_id: int | None = None,
    db_type: str | None = Query(default=None, pattern="^(oracle|mysql|postgresql|sqlserver|host)$"),
    limit: int = 50,
    db: Session = Depends(get_db),
):
    return {"items": result_writer.list_tasks(
        db, instance_id, db_type, limit, allowed_instance_ids=_allowed_instance_ids(request, db),
    )}


@router.get("/tasks/{task_id}")
def api_get_task(task_id: str, instance_id: int | None = None, db: Session = Depends(get_db)):
    result = report_builder.build_report_payload(db, task_id, instance_id)
    if not result:
        raise HTTPException(status_code=404, detail="Inspection task not found")
    return result


@router.get("/results")
def api_list_results(
    request: Request,
    instance_id: int | None = None,
    db_type: str | None = Query(default=None, pattern="^(oracle|mysql|postgresql|sqlserver|host)$"),
    status: str | None = Query(default=None, pattern="^(critical|warning|info|ok|normal)$"),
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    items, total = result_writer.list_results(
        db, instance_id, db_type, status, page, page_size,
        allowed_instance_ids=_allowed_instance_ids(request, db),
    )
    return {"items": [result_writer.row_to_dict(item) for item in items], "total": total}


@router.get("/report/{task_id}")
def api_get_report(task_id: str, instance_id: int | None = None, db: Session = Depends(get_db)):
    result = report_builder.build_report_payload(db, task_id, instance_id)
    if not result:
        raise HTTPException(status_code=404, detail="Inspection report not found")
    return result


class TemplateVersionRestorePayload(BaseModel):
    #: Optimistic lock: restores are only safe against the revision the caller actually saw.
    expected_revision: int


def _get_instance(db: Session, db_type: str, instance_id: int):
    if db_type not in runner.INSPECTION_DB_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported db_type: {db_type}")
    instance = instance_service.get_by_id(db, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if db_type == "host":
        return runner.host_inspection_instance(instance)
    if instance.db_type != db_type:
        raise HTTPException(status_code=400, detail=f"Instance is not {db_type}")
    return instance
