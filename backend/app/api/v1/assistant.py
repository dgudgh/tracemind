"""Authenticated capability discovery and explicit read workflows, not arbitrary tool dispatch."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import json
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.transaction_lock import lock_transaction
from app.core.request_limits import ASSISTANT_BODY_LIMIT, RequestBodyTooLarge, bounded_body
from app.models.capability_definition import DBCapabilityDefinition
from app.models.db_instance import DBInstance
from app.models.access_control import SecurityAuditEvent
from app.models.capability_execution import DBCapabilityBatch, DBCapabilityExecution
from app.services import access_control, capability_registry as registry, plan_service as plans
from app.services import plan_governance as governance
from app.services import plan_admission, plan_queue, plan_control, capability_dispatch
from app.services import workflow_library


class DispatchIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=2000)
    instance_id: StrictInt | None = Field(default=None, gt=0)
    request_id: str | None = Field(default=None, min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    answers: dict[str, str | int | bool] = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def bounded_answers(self):
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key)
               or isinstance(value, str) and len(value) > 2000 for key, value in self.answers.items()):
            raise ValueError("invalid clarification answer")
        return self


class AssistantRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                _reject(request, "invalid_request", 422)

        return handle


router = APIRouter(prefix="/assistant", tags=["Capability Plans"], route_class=AssistantRoute)
logger = logging.getLogger(__name__)
MAX_BODY_BYTES = ASSISTANT_BODY_LIMIT
ENGINE_PATTERN = "^(oracle|mysql|postgresql|sqlserver|host)$"
PLAN_PATTERN = re.compile(r"^pl[a-f0-9]{26}$")
BATCH_PATTERN = re.compile(r"^CB[a-f0-9]{28}$")


class PlanInputRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: StrictInt = Field(ge=1, le=20)
    source_schema: str = Field(alias="schema", min_length=1, max_length=100)
    field: str = Field(min_length=1, max_length=100)
    type: Literal["integer", "string", "boolean"]


class PlanStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seq: StrictInt = Field(ge=1, le=20)
    capability_id: str = Field(min_length=1, max_length=64, pattern=registry.ID_PATTERN.pattern)
    definition_version: StrictInt = Field(ge=1, le=2147483647)
    title: str = Field(default="Task", min_length=1, max_length=200)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[StrictInt] = Field(default_factory=list, max_length=20)
    input_refs: dict[str, PlanInputRef] = Field(default_factory=dict)
    fanout: StrictInt = Field(default=1, ge=1, le=1)
    retries: StrictInt = Field(default=0, ge=0, le=0)


class PlanCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability_id: str = Field(min_length=1, max_length=64, pattern=registry.ID_PATTERN.pattern)
    definition_version: StrictInt = Field(ge=1, le=2147483647)
    instance_id: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    params: dict[str, Any] = Field(default_factory=dict)
    steps: list[PlanStepInput] | None = Field(default=None, min_length=1, max_length=20)
    parent_plan_id: str | None = Field(default=None, pattern=PLAN_PATTERN.pattern)
    trigger_type: Literal["manual", "assistant"] = "manual"
    valid_for_seconds: StrictInt | None = Field(default=None, ge=60, le=86400)
    request_approval: StrictBool = False


class PlanRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=1, le=2147483646)
    capability_id: str = Field(min_length=1, max_length=64, pattern=registry.ID_PATTERN.pattern)
    definition_version: StrictInt = Field(ge=1, le=2147483647)
    instance_id: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    params: dict[str, Any] = Field(default_factory=dict)
    steps: list[PlanStepInput] | None = Field(default=None, min_length=1, max_length=20)
    valid_for_seconds: StrictInt = Field(default=3600, ge=60, le=86400)
    request_approval: StrictBool = False


class PlanDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=1, le=2147483646)


class PlanApprovalInput(PlanDecisionInput):
    expected_binding_hash: str = Field(pattern="^[a-f0-9]{64}$")
    valid_for_seconds: StrictInt = Field(default=900, ge=60, le=3600)


class BatchTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instance_id: StrictInt = Field(ge=1, le=2**63 - 1)
    params: dict[str, Any] = Field(default_factory=dict)


class BatchPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability_id: str = Field(min_length=1, max_length=64, pattern=registry.ID_PATTERN.pattern)
    definition_version: StrictInt = Field(ge=1, le=2147483647)
    targets: list[BatchTargetInput] = Field(min_length=1, max_length=100)
    steps: list[PlanStepInput] | None = Field(default=None, min_length=1, max_length=20)
    valid_for_seconds: StrictInt = Field(default=3600, ge=60, le=86400)
    request_approval: StrictBool = False


class BatchEnqueueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_ids: list[str] = Field(min_length=1, max_length=100)
    deadline_seconds: StrictInt = Field(default=3600, ge=1, le=86400)


class ControlDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_control_version: StrictInt = Field(ge=0, le=2**63 - 2)
    reason: str = Field(min_length=1, max_length=300)


class WorkflowTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instance_id: StrictInt = Field(ge=1, le=2**63 - 1)
    slots: dict[str, Any] = Field(default_factory=dict)


class WorkflowScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["global", "explicit"]
    targets: list[WorkflowTargetInput] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def explicit_scope(self):
        if ((self.mode == "global" and "targets" in self.model_fields_set)
                or (self.mode == "explicit" and self.targets is None)):
            raise ValueError("Explicit targets or a target-free global scope required")
        return self


class WorkflowPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["prepare", "rerun"] = "prepare"
    slots: dict[str, Any] = Field(default_factory=dict)
    scope: WorkflowScopeInput
    source_plan_id: str | None = Field(default=None, pattern=PLAN_PATTERN.pattern)


class WorkflowRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=1, le=2147483646)
    slots: dict[str, Any] = Field(default_factory=dict)
    scope: WorkflowScopeInput


class WorkflowMatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=4000)
    matcher_version: StrictInt = Field(default=1, ge=1, le=2147483647)


class WorkflowProposalInput(BaseModel):
    """Only the user's own intent and consent; the catalog and choices stay server-side."""
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=4000)
    scope: WorkflowScopeInput
    confirmed_policy_id: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)
    provider_config_id: StrictInt = Field(ge=1, le=2**63 - 1)
    provider_revision: str = Field(pattern="^[a-f0-9]{64}$")
    allow_model_context: StrictBool


BATCH_VERSION_PARAMETER = {"name": "X-Assistant-Batch-Version", "in": "header", "required": False,
                           "schema": {"type": "integer", "enum": [1, 2], "default": 1}}


def _request_schema(model: type[BaseModel]) -> dict:
    # Inline this bounded, non-recursive input graph for a raw-JSON dependency.
    # Parsing stays duplicate-key aware without hiding the request from OpenAPI.
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].removeprefix("#/$defs/")
                return expand({**definitions[name], **{key: item for key, item in value.items() if key != "$ref"}})
            return {key: expand(item) for key, item in value.items()}
        return value

    return expand(schema)


def _reject(request: Request, code: str, status: int = 400):
    info = getattr(request.state, "auth", {}) or {}
    user_id = info.get("user_id")
    route = getattr(request.scope.get("route"), "path", "/assistant")
    logger.warning("assistant_request_rejected actor_user_id=%s route=%s code=%s",
                   user_id if type(user_id) is int else None, route, code)
    raise HTTPException(status_code=status, detail={"code": code})


def _actor(request: Request, db: Session = Depends(get_db)) -> dict:
    info = getattr(request.state, "auth", {}) or {}
    if info.get("source") not in {"db", "identity"}:
        _reject(request, "authentication_required", 401)
    actor = {"user_id": info.get("user_id")}
    try:
        _, grants, _ = registry._principal(db, actor)
    except registry.CapabilityError as exc:
        _reject(request, exc.code, 401)
    except SQLAlchemyError:
        _reject(request, "plan_storage_unavailable", 503)
    if not access_control.action_matches(grants, "asset.read"):
        _reject(request, "permission_denied", 403)
    return actor


def _query(request: Request, allowed: set[str]):
    keys = list(request.query_params)
    if set(keys) - allowed or any(len(request.query_params.getlist(key)) != 1 for key in keys):
        _reject(request, "invalid_query")


def _guard(request: Request, db: Session, callback):
    try:
        return callback()
    except (plans.PlanError, registry.CapabilityError, plan_queue.QueueError, plan_control.ControlError,
            workflow_library.WorkflowError) as exc:
        _rollback(db)
        code = exc.code
        if code in {"permission_denied", "scope_denied", "scope_not_supported"}:
            status = 403
        elif code in {"authentication_required", "actor_inactive"}:
            status = 401
        elif code in {"plan_not_found", "batch_not_found", "preparation_not_found", "execution_not_found", "control_command_not_found", "unregistered_capability", "target_unavailable",
                      "workflow_not_found", "workflow_preparation_not_found", "workflow_source_not_found", "workflow_matcher_not_found",
                      "workflow_proposal_not_found"}:
            status = 404
        elif code in {"registry_storage_unavailable", "plan_storage_unavailable", "plan_audit_unavailable", "control_audit_unavailable", "queue_full",
                      "workflow_storage_unavailable", "workflow_audit_unavailable"}:
            status = 503
        elif code in {"plan_revision_conflict", "approval_required", "approval_revoked", "approval_expired",
                      "approval_binding_invalid", "approval_authority_revoked", "plan_expired",
                      "invalid_approval_state", "duplicate_approval", "self_approval_forbidden",
                      "approval_revoked_requires_revision", "executed_plan_requires_rerun", "plan_already_enqueued",
                      "plan_batch_binding_active", "plan_not_queueable", "batch_plan_changed", "preparation_binding_changed",
                      "batch_control_requires_v2", "control_version_conflict", "control_deadline_expired",
                      "execution_binding_changed", "execution_requires_reconciliation", "execution_not_controllable",
                      "execution_not_paused", "checkpoint_missing_or_changed", "checkpoint_changed",
                      "checkpoint_uncertain", "checkpoint_invalid", "checkpoint_evidence_missing",
                      "checkpoint_evidence_invalid", "execution_epoch_lost", "target_occupied", "control_unsupported",
                      "target_fence_unverified", "stop_request_is_irreversible", "batch_delivery_cancelled",
                      "workflow_definition_changed", "workflow_binding_invalid", "workflow_revision_requires_recipe",
                      "workflow_rerun_source_unsettled", "workflow_preparation_binding_changed",
                      "workflow_provider_not_authorized", "proposal_binding_changed"}:
            status = 409
        elif code in {"stale_definition_version", "definition_not_synced", "definition_out_of_sync", "capability_disabled",
                      "idempotency_conflict", "parent_plan_not_settled", "parent_plan_already_retried",
                      "rerun_request_mismatch", "plan_snapshot_invalid", "invalid_plan_state"}:
            status = 409
        elif code == "workflow_missing_input":
            status = 422
        elif code == "workflow_provider_context_not_approved" or code.startswith("workflow_proposal_"):
            # Structural refusals are the caller's to correct; the model text is never echoed.
            status = 422
        elif code == "upstream_unavailable":
            status = 502
        elif code == "workflow_provider_budget_exhausted":
            status = 429
        elif code == "workflow_provider_unavailable":
            status = 502
        else:
            status = 400
        _reject(request, code, status)
    except SQLAlchemyError:
        _rollback(db)
        _reject(request, "plan_storage_unavailable", 503)


def _rollback(db: Session):
    try:
        db.rollback()
    except SQLAlchemyError:
        logger.warning("assistant_rollback_failed")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


async def _body(request: Request) -> dict:
    if request.query_params:
        _reject(request, "invalid_query")
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != "application/json" and not media.endswith("+json"):
        _reject(request, "json_required", 415)
    try:
        raw = await bounded_body(request, limit=MAX_BODY_BYTES)
    except RequestBodyTooLarge:
        _reject(request, "request_too_large", 413)
    try:
        def invalid_constant(_value):
            raise ValueError("non-finite number")
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("object required")
        registry.validate_params(value)
        return value
    except registry.CapabilityError as exc:
        _reject(request, exc.code)
    except (ValueError, UnicodeError, RecursionError):
        _reject(request, "invalid_json")


async def _create_body(request: Request) -> PlanCreateInput:
    value = await _body(request)
    try:
        return PlanCreateInput.model_validate(value)
    except ValidationError:
        # Never echo rejected input: it may contain a supplied credential or script.
        _reject(request, "invalid_plan_request")


async def _dispatch_body(request: Request) -> DispatchIntentRequest:
    value = await _body(request)
    try:
        return DispatchIntentRequest.model_validate(value)
    except ValidationError:
        _reject(request, "invalid_dispatch_request", 422)


async def _typed_body(request, model):
    value = await _body(request)
    try:
        return model.model_validate(value)
    except ValidationError:
        _reject(request, "invalid_plan_request")


async def _revision_body(request: Request) -> PlanRevisionInput:
    return await _typed_body(request, PlanRevisionInput)


async def _decision_body(request: Request) -> PlanDecisionInput:
    return await _typed_body(request, PlanDecisionInput)


async def _approval_body(request: Request) -> PlanApprovalInput:
    return await _typed_body(request, PlanApprovalInput)


async def _batch_prepare_body(request: Request) -> BatchPrepareInput:
    return await _typed_body(request, BatchPrepareInput)


async def _batch_enqueue_body(request: Request) -> BatchEnqueueInput:
    return await _typed_body(request, BatchEnqueueInput)


def _batch_key(request):
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1 or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", values[0]) is None:
        _reject(request, "invalid_idempotency_key")
    return values[0]


def _batch_version(request):
    values = request.headers.getlist("x-assistant-batch-version")
    if not values:
        return 1
    if len(values) != 1 or values[0] not in {"1", "2"}:
        _reject(request, "invalid_batch_schema_version")
    return int(values[0])


async def _control_body(request: Request):
    return await _typed_body(request, ControlDecisionInput)


async def _workflow_prepare_body(request: Request):
    return await _typed_body(request, WorkflowPrepareInput)


async def _workflow_revision_body(request: Request):
    return await _typed_body(request, WorkflowRevisionInput)


async def _workflow_match_body(request: Request):
    return await _typed_body(request, WorkflowMatchInput)


async def _workflow_proposal_body(request: Request):
    value = await _body(request)
    try:
        return WorkflowProposalInput.model_validate(value)
    except ValidationError:
        # A catalog, slots or target list in the body is a rejected field, not a hint.
        _reject(request, "workflow_proposal_invalid_request", 422)


def _workflow_service():
    from app.services import workflow_service
    return workflow_service


def _workflow_ref(request, workflow_id, workflow_version):
    if (len(workflow_id) > 64 or workflow_library.WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None
            or not 1 <= workflow_version <= 2147483647):
        _reject(request, "workflow_invalid_reference")


async def _execute_body(request: Request):
    if await _body(request):
        _reject(request, "execution_params_not_allowed")


def _plan_id(request, value):
    if not PLAN_PATTERN.fullmatch(value):
        _reject(request, "invalid_plan_id")


@router.get("/capabilities")
def capabilities(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db),
                 engine: str | None = Query(default=None, pattern=ENGINE_PATTERN),
                 kind: str | None = Query(default=None, pattern="^(read|query|collect|scan|refresh|sync|bind|plan|execute)$"),
                 execution: Literal["server", "client"] | None = None, include_disabled: bool = False) -> dict:
    _query(request, {"engine", "kind", "execution", "include_disabled"})
    def fetch():
        value = registry.list_capabilities(db, actor=actor, engine=engine)
        disabled = set() if include_disabled else {row[0] for row in db.query(DBCapabilityDefinition.capability_id).filter(DBCapabilityDefinition.enabled != 1)}
        value["items"] = [item for item in value["items"] if item["capability_id"] not in disabled
                          and (kind is None or item["kind"] == kind) and (execution is None or item["execution"] == execution)]
        value["total"] = len(value["items"])
        value["summary"] = {"count": value["total"], "by_kind": dict(Counter(item["kind"] for item in value["items"])),
                            "by_execution": dict(Counter(item["execution"] for item in value["items"]))}
        return value
    return _guard(request, db, fetch)


@router.post("/dispatch-intent", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(DispatchIntentRequest)}}}})
def dispatch_intent(request: Request, payload: DispatchIntentRequest = Depends(_dispatch_body),
                    actor: dict = Depends(_actor),
                    db: Session = Depends(get_db)) -> dict:
    """Understand a goal, clarify missing inputs, and dispatch registered capability steps."""
    def run():
        instance = None
        if payload.instance_id:
            instance = db.query(DBInstance).filter_by(id=payload.instance_id).first()
            if instance is None:
                raise plans.PlanError("target_unavailable")
        value = capability_dispatch.dispatch(db, actor=actor, text=payload.text, instance=instance,
                                             request_id=getattr(payload, "request_id", None),
                                             answers=getattr(payload, "answers", {}))
        value["schema"] = "assistant.dispatch/v1"
        return value
    return _guard(request, db, run)


@router.get("/dispatch-metrics")
def dispatch_metrics(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db),
                     days: int = Query(default=7, ge=1, le=90)) -> dict:
    if (getattr(request.state, "auth", {}) or {}).get("role") != "admin":
        _reject(request, "permission_denied", 403)
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.query(SecurityAuditEvent).filter(
        SecurityAuditEvent.category == "assistant",
        SecurityAuditEvent.action == "assistant.dispatch",
        SecurityAuditEvent.create_time >= since,
    ).all()
    actions, reasons, providers, durations = Counter(), Counter(), Counter(), []
    for row in rows:
        response = (row.details or {}).get("response") if isinstance(row.details, dict) else None
        if not isinstance(response, dict):
            continue
        actions[str(response.get("action"))] += 1
        reasons[str(response.get("reason_code") or "none")] += 1
        provider = response.get("provider") or {}
        if provider.get("name"):
            providers[f"{provider.get('name')}:{provider.get('model')}"] += 1
        if isinstance(response.get("duration_ms"), int):
            durations.append(response["duration_ms"])
    return {"schema": "assistant.dispatch_metrics/v1", "window_days": days, "total": len(rows),
            "by_action": dict(actions), "by_reason": dict(reasons), "by_provider": dict(providers),
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None}


@router.get("/dispatch-intent/{request_id}")
def dispatch_receipt(request_id: str, request: Request, actor: dict = Depends(_actor),
                     db: Session = Depends(get_db)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,120}", request_id or ""):
        _reject(request, "invalid_request_id", 422)
    value = capability_dispatch.read_receipt(db, actor=actor, request_id=request_id)
    if value is None:
        raise HTTPException(404, "请求回执不存在")
    value["schema"] = "assistant.dispatch/v1"
    return value


@router.get("/capabilities/{capability}")
def capability_detail(capability: str, request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    # Semantic IDs deliberately do not use an _id route parameter; they are not asset IDs.
    _query(request, set())
    def fetch():
        registry._entry(capability)
        value = registry.list_capabilities(db, actor=actor)
        for item in value["items"]:
            if item["capability_id"] == capability:
                return item
        raise registry.CapabilityError("permission_denied")
    return _guard(request, db, fetch)


@router.post("/plans", status_code=201,
             responses={200: {"description": "Existing idempotent plan"},
                        422: {"description": "Persisted needs_input plan, not execution success"}},
             openapi_extra={
                 "parameters": [{"name": "Idempotency-Key", "in": "header", "required": False,
                                 "schema": {"type": "string", "minLength": 1, "maxLength": 128,
                                            "pattern": "^[A-Za-z0-9._:-]+$"}}],
                 "requestBody": {"required": True, "content": {"application/json": {
                     "schema": _request_schema(PlanCreateInput)}}}})
def create_plan(request: Request, actor: dict = Depends(_actor), payload: PlanCreateInput = Depends(_create_body),
                db: Session = Depends(get_db)):
    if len(request.headers.getlist("idempotency-key")) > 1:
        _reject(request, "invalid_idempotency_key")
    value = _guard(request, db, lambda: plans.create_plan(
        db, actor=actor, **payload.model_dump(exclude_unset=True, by_alias=True),
        idempotency_key=request.headers.get("idempotency-key")))
    code = 422 if value["outcome"] == "needs_input" else 200 if value["reused"] else 201
    return JSONResponse(status_code=code, content=value)


@router.get("/plans")
def list_plans(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db),
               page: int = Query(default=1, ge=1, le=1000000), page_size: int = Query(default=50, ge=1, le=100),
               status: str | None = None, outcome: str | None = None,
               capability_id: str | None = Query(default=None, max_length=64),
               instance_id: int | None = Query(default=None, ge=1, le=2**63 - 1)) -> dict:
    _query(request, {"page", "page_size", "status", "outcome", "capability_id", "instance_id"})
    return _guard(request, db, lambda: plans.list_plans(db, actor=actor, page=page, page_size=page_size,
                                                      status=status, outcome=outcome,
                                                      capability_id=capability_id, instance_id=instance_id))


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: str, request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: _delivery_plan(db, actor, plans.read_plan(db, plan_id, actor=actor)))


def _delivery_plan(db, actor, value):
    """Queue acceptance is separate from the plan's persisted execution outcome."""
    if not any(registry.durable_effect_supported(
            step.get("capability_id"), step.get("definition_version"))
            for step in value.get("steps", [])):
        return value
    delivery = db.query(DBCapabilityExecution).join(
        DBCapabilityBatch, DBCapabilityBatch.batch_id == DBCapabilityExecution.batch_id).filter(
        DBCapabilityExecution.plan_id == value["plan_id"],
        DBCapabilityExecution.plan_revision == value["revision"],
        DBCapabilityExecution.binding_hash == value["binding_hash"],
        DBCapabilityBatch.requested_by == actor["user_id"],
    ).order_by(DBCapabilityExecution.id.desc()).first()
    if delivery:
        value["execution_batch_id"] = delivery.batch_id
        value["execution_queue_state"] = delivery.state
    return value


@router.post("/plans/{plan_id}/execute", openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "additionalProperties": False, "maxProperties": 0}}}}})
def execute_plan(plan_id: str, request: Request, actor: dict = Depends(_actor),
                 _empty=Depends(_execute_body), db: Session = Depends(get_db)) -> dict:
    _plan_id(request, plan_id)
    def execute():
        # Bind the revision-derived admission key under the same transaction lock
        # used by revisions, approvals and queue admission.
        lock_transaction(db, "capability_plan_state")
        saved = plans._load(db, plan_id, actor, owner_only=True)
        if "execution_identity" in saved.capability_snapshot_json or any(registry.durable_effect_supported(
                entry.get("capability_id"), entry.get("version"), entry.get("output_schema"))
                for entry in saved.capability_snapshot_json["definitions"]):
            value = _delivery_plan(db, actor, plans.read_plan(db, plan_id, actor=actor))
            # A committed delivery is authoritative even if its acceptance response
            # was lost or another admission endpoint created it.
            if value.get("execution_batch_id"):
                return value
            if value["status"] != "settled":
                plan_queue.enqueue_batch(db, [plan_id], actor=actor,
                                         idempotency_key=f"plan-execute:{plan_id}:{value['revision']}", schema_version=2)
            return _delivery_plan(db, actor, plans.read_plan(db, plan_id, actor=actor))
        return plans.execute_plan(db, plan_id, actor=actor)
    return _guard(request, db, execute)


@router.get("/plans/{plan_id}/validation")
def plan_validation(plan_id: str, request: Request, actor: dict = Depends(_actor),
                    db: Session = Depends(get_db)) -> dict:
    """Current read-only readiness. Execution always repeats authorization and checks."""
    _query(request, set())
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: plans.review_plan(db, plan_id, actor=actor))


@router.post("/plans/{plan_id}/revisions", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(PlanRevisionInput)}}}})
def revise_plan(plan_id: str, request: Request, actor: dict = Depends(_actor),
                payload: PlanRevisionInput = Depends(_revision_body), db: Session = Depends(get_db)) -> dict:
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: governance.revise_plan(db, plan_id, actor=actor,
                                                            **payload.model_dump(by_alias=True)))


@router.get("/plans/{plan_id}/approval")
def plan_approval(plan_id: str, request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: governance.read_approval(db, plan_id, actor=actor))


@router.post("/plans/{plan_id}/approval/request", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(PlanDecisionInput)}}}})
def request_plan_approval(plan_id: str, request: Request, actor: dict = Depends(_actor),
                          payload: PlanDecisionInput = Depends(_decision_body), db: Session = Depends(get_db)) -> dict:
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: governance.request_approval(db, plan_id, actor=actor,
                                                                 **payload.model_dump()))


@router.post("/plans/{plan_id}/approval/approve", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(PlanApprovalInput)}}}})
def approve_plan(plan_id: str, request: Request, actor: dict = Depends(_actor),
                 payload: PlanApprovalInput = Depends(_approval_body), db: Session = Depends(get_db)) -> dict:
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: governance.approve_plan(db, plan_id, actor=actor,
                                                             **payload.model_dump()))


@router.post("/plans/{plan_id}/approval/revoke", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(PlanDecisionInput)}}}})
def revoke_plan_approval(plan_id: str, request: Request, actor: dict = Depends(_actor),
                         payload: PlanDecisionInput = Depends(_decision_body), db: Session = Depends(get_db)) -> dict:
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: governance.revoke_approval(db, plan_id, actor=actor,
                                                                **payload.model_dump()))


@router.post("/batches/prepare", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(BatchPrepareInput)}}}})
def prepare_batch(request: Request, actor: dict = Depends(_actor),
                   payload: BatchPrepareInput = Depends(_batch_prepare_body), db: Session = Depends(get_db)) -> dict:
    key = _batch_key(request)
    return _guard(request, db, lambda: plan_admission.prepare_batch(
        db, actor=actor, idempotency_key=key, **payload.model_dump(by_alias=True)))


@router.post("/batches", status_code=202, openapi_extra={"parameters": [BATCH_VERSION_PARAMETER], "requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(BatchEnqueueInput)}}}})
def enqueue_batch(request: Request, actor: dict = Depends(_actor),
                   payload: BatchEnqueueInput = Depends(_batch_enqueue_body), db: Session = Depends(get_db)) -> dict:
    key = _batch_key(request)
    version = _batch_version(request)
    return _guard(request, db, lambda: plan_queue.enqueue_batch(
        db, actor=actor, idempotency_key=key, schema_version=version, **payload.model_dump()))


@router.get("/batches", openapi_extra={"parameters": [BATCH_VERSION_PARAMETER]})
def list_batches(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db),
                  page: int = Query(default=1, ge=1, le=1000000),
                  page_size: int = Query(default=50, ge=1, le=100)) -> dict:
    _query(request, {"page", "page_size"})
    return _guard(request, db, lambda: plan_queue.list_batches(db, actor=actor, page=page, page_size=page_size,
                                                            schema_version=_batch_version(request)))


@router.get("/batches/preparation", openapi_extra={"parameters": [{
    "name": "Idempotency-Key", "in": "header", "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}]})
def preparation_receipt(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    key = _batch_key(request)
    return _guard(request, db, lambda: plan_admission.read_preparation(db, actor=actor, idempotency_key=key))


@router.get("/batches/admission", openapi_extra={"parameters": [BATCH_VERSION_PARAMETER, {
    "name": "Idempotency-Key", "in": "header", "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}]})
def admission_receipt(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    key = _batch_key(request)
    return _guard(request, db, lambda: plan_admission.read_admission(db, actor=actor, idempotency_key=key,
                                                                 schema_version=_batch_version(request)))


@router.get("/batches/{batch_id}", openapi_extra={"parameters": [BATCH_VERSION_PARAMETER]})
def batch_detail(batch_id: str, request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    if not BATCH_PATTERN.fullmatch(batch_id):
        _reject(request, "invalid_batch_id")
    return _guard(request, db, lambda: plan_queue.read_batch(db, batch_id, actor=actor,
                                                         schema_version=_batch_version(request)))


@router.post("/batches/{batch_id}/cancel", openapi_extra={"parameters": [BATCH_VERSION_PARAMETER], "requestBody": {"required": True,
             "content": {"application/json": {"schema": {"type": "object", "additionalProperties": False, "maxProperties": 0}}}}})
def cancel_batch(batch_id: str, request: Request, actor: dict = Depends(_actor),
                   _empty=Depends(_execute_body), db: Session = Depends(get_db)) -> dict:
    if not BATCH_PATTERN.fullmatch(batch_id):
        _reject(request, "invalid_batch_id")
    return _guard(request, db, lambda: plan_queue.cancel_batch(db, batch_id, actor=actor,
                                                           schema_version=_batch_version(request)))


@router.get("/executions/{capability_execution_id}/control")
def execution_control(capability_execution_id: int, request: Request,
                      actor: dict = Depends(_actor), db: Session = Depends(get_db),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    _query(request, set())
    key = _batch_key(request) if request.headers.getlist("idempotency-key") else None
    return _guard(request, db, lambda: plan_control.read_control(
        db, capability_execution_id, actor=actor, idempotency_key=key))


def _control_request(request, db, actor, execution_id, action, payload):
    key = _batch_key(request)
    return _guard(request, db, lambda: plan_control.request_control(
        db, execution_id, actor=actor, action=action, idempotency_key=key, **payload.model_dump()))


@router.post("/executions/{capability_execution_id}/pause", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(ControlDecisionInput)}}}})
def pause_execution(capability_execution_id: int, request: Request, actor: dict = Depends(_actor),
                    payload: ControlDecisionInput = Depends(_control_body), db: Session = Depends(get_db)):
    return _control_request(request, db, actor, capability_execution_id, "pause", payload)


@router.post("/executions/{capability_execution_id}/resume", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(ControlDecisionInput)}}}})
def resume_execution(capability_execution_id: int, request: Request, actor: dict = Depends(_actor),
                     payload: ControlDecisionInput = Depends(_control_body), db: Session = Depends(get_db)):
    return _control_request(request, db, actor, capability_execution_id, "resume", payload)


@router.post("/executions/{capability_execution_id}/stop", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(ControlDecisionInput)}}}})
def stop_execution(capability_execution_id: int, request: Request, actor: dict = Depends(_actor),
                   payload: ControlDecisionInput = Depends(_control_body), db: Session = Depends(get_db)):
    return _control_request(request, db, actor, capability_execution_id, "stop", payload)


@router.post("/executions/{capability_execution_id}/probe", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(ControlDecisionInput)}}}})
def probe_execution(capability_execution_id: int, request: Request, actor: dict = Depends(_actor),
                    payload: ControlDecisionInput = Depends(_control_body), db: Session = Depends(get_db)):
    return _control_request(request, db, actor, capability_execution_id, "probe", payload)


@router.get("/workflows")
def workflow_catalog(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)):
    _query(request, set())
    return _guard(request, db, lambda: _workflow_service().list_workflows(db, actor=actor))


@router.get("/workflows/preparation", openapi_extra={"parameters": [{
    "name": "Idempotency-Key", "in": "header", "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}]})
def workflow_preparation(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)):
    _query(request, set())
    key = _batch_key(request)
    return _guard(request, db, lambda: _workflow_service().read_preparation(db, actor=actor, idempotency_key=key))


@router.get("/workflows/providers")
def workflow_providers(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)):
    """Consent targets for a proposal: name/model/revision only, never endpoints or keys."""
    _query(request, set())
    return _guard(request, db, lambda: _workflow_service().list_providers(db, actor=actor))


@router.post("/workflows/proposals", openapi_extra={
    "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128}}],
    "requestBody": {"required": True, "content": {"application/json": {
        "schema": _request_schema(WorkflowProposalInput)}}}})
def propose_workflow(request: Request, actor: dict = Depends(_actor),
                     payload: WorkflowProposalInput = Depends(_workflow_proposal_body),
                     db: Session = Depends(get_db)) -> dict:
    key = _batch_key(request)
    return _guard(request, db, lambda: _workflow_service().propose_workflow(
        db, actor=actor, idempotency_key=key, text=payload.text,
        scope=payload.scope.model_dump(exclude_none=True), confirmed_policy_id=payload.confirmed_policy_id,
        provider_config_id=payload.provider_config_id, provider_revision=payload.provider_revision,
        allow_model_context=payload.allow_model_context))


@router.get("/workflows/proposals", openapi_extra={"parameters": [{
    "name": "Idempotency-Key", "in": "header", "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}]})
def workflow_proposal(request: Request, actor: dict = Depends(_actor), db: Session = Depends(get_db)) -> dict:
    _query(request, set())
    key = _batch_key(request)
    return _guard(request, db, lambda: _workflow_service().read_proposal(db, actor=actor, idempotency_key=key))


@router.get("/workflows/{workflow_id}/versions/{workflow_version}")
def workflow_detail(workflow_id: str, workflow_version: int, request: Request,
                    actor: dict = Depends(_actor), db: Session = Depends(get_db)):
    _query(request, set())
    _workflow_ref(request, workflow_id, workflow_version)
    return _guard(request, db, lambda: _workflow_service().get_workflow(
        db, workflow_id, workflow_version, actor=actor))


@router.post("/workflows/match", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(WorkflowMatchInput)}}}})
def workflow_match(request: Request, actor: dict = Depends(_actor),
                   payload: WorkflowMatchInput = Depends(_workflow_match_body), db: Session = Depends(get_db)):
    return _guard(request, db, lambda: _workflow_service().match_workflow(db, actor=actor, **payload.model_dump()))


@router.post("/workflows/{workflow_id}/versions/{workflow_version}/prepare", openapi_extra={
    "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128}}],
    "requestBody": {"required": True, "content": {"application/json": {"schema": _request_schema(WorkflowPrepareInput)}}}})
def prepare_workflow(workflow_id: str, workflow_version: int, request: Request,
                     actor: dict = Depends(_actor), payload: WorkflowPrepareInput = Depends(_workflow_prepare_body),
                     db: Session = Depends(get_db)):
    _workflow_ref(request, workflow_id, workflow_version)
    key = _batch_key(request)
    return _guard(request, db, lambda: _workflow_service().prepare_workflow(
        db, workflow_id, workflow_version, actor=actor, idempotency_key=key,
        **payload.model_dump(exclude_none=True)))


@router.post("/workflows/plans/{plan_id}/revisions", openapi_extra={"requestBody": {"required": True,
             "content": {"application/json": {"schema": _request_schema(WorkflowRevisionInput)}}}})
def revise_workflow(plan_id: str, request: Request, actor: dict = Depends(_actor),
                    payload: WorkflowRevisionInput = Depends(_workflow_revision_body), db: Session = Depends(get_db)):
    _plan_id(request, plan_id)
    return _guard(request, db, lambda: _workflow_service().revise_workflow(
        db, plan_id, actor=actor, **payload.model_dump(exclude_none=True)))
