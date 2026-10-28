import time
import logging
import os
import ipaddress
import re
import secrets
import json

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api.v1 import routers
from app.core.auth import AuthenticationUnavailable, get_bearer_token, get_token_info
from app.core import edition
from app.core.config import settings
from app.core.database import engine, initialize_schema, get_db
from app.core.request_limits import ASSISTANT_BODY_LIMIT, RequestBodyTooLarge, bounded_body
from app.migrations.policy import SchemaUpgradeRequired
from app.migrations.inspection_result_integrity import InspectionResultIntegrityError
from app.core.database_options import database_target_summary, redact_database_error
from app.core.logging import setup_logging
from app.services.prometheus_metrics import render_metrics
from app.services.platform_observability import liveness, readiness, route_template, runtime_metrics
# Import model modules so SQLAlchemy metadata is populated before create_all().
from app import models

logger = setup_logging(debug=settings.DEBUG)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        status_code = 500
        runtime_metrics.request_started()
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.monotonic() - start
            route = route_template(request)
            runtime_metrics.request_finished(request.method, route, status_code, elapsed)
            logging.getLogger("app.access").info(
                "%s %s %s %.1fms",
                request.method,
                route,
                status_code,
                elapsed * 1000,
            )


AUTH_WHITELIST = {"/health", "/health/live", "/health/ready", "/health/ready/platform", "/api/v1/setup/license"}
LICENSE_WHITELIST = {
    "/health",
    "/health/live",
    "/health/ready",
    "/health/ready/platform",
    "/favicon.ico",
    "/api/v1/setup/license",
    "/api/v1/setup/license/upload",
}
PROTECTED_NON_API_PATHS = {"/metrics", "/docs", "/openapi.json", "/redoc"}
LICENSE_PREFIX_WHITELIST = ("/api/v1/auth/", "/assets/")
AUTH_LOGIN_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/external-login",
    "/api/v1/auth/mes-login",
    "/api/v1/auth/mfa/verify",
    "/api/v1/auth/jwks",
}
BOOTSTRAP_STATUS_PATHS = {"/api/v1/setup/status", "/api/v1/system/init/status"}
WEBHOOK_PATHS = {
    "/api/v1/zabbix/webhook",
    "/api/v1/prometheus/alertmanager/webhook",
    "/api/v1/observability/alertmanager/webhook",
    "/api/v1/observability/alerts/ingest",
    "/api/v1/itsm/webhook/status-callback",
}
ADMIN_ONLY_PREFIXES = (
    "/api/v1/setup",
    "/api/v1/system/init",
    "/api/v1/users",
    "/api/v1/modules",
    "/api/v1/settings",
    "/api/v1/ssh",
    "/api/v1/recovery",
    "/api/v1/ai-config",
    "/api/v1/zabbix",
    "/api/v1/zabbix-push",
    "/api/v1/prometheus",
    "/api/v1/observability",
    "/api/v1/ecosystem",
    "/api/v1/notify",
    "/api/v1/itsm",
    "/api/v1/cmdb",
    "/api/v1/assets",
    "/api/v1/automation",
    "/api/v1/identity",
    "/api/v1/connection-security",
    "/api/v1/security",
)
PROXY_CLIENT_IP_HEADER = "X-TraceMind-Proxy-Client-IP"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_auth_self_service(method: str, path: str) -> bool:
    path = path.rstrip("/")
    routes = {
        ("GET", "/api/v1/auth/info"),
        ("GET", "/api/v1/auth/current-user"),
        ("GET", "/api/v1/auth/sessions"),
        ("GET", "/api/v1/auth/mfa/status"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/mfa/enroll"),
        ("POST", "/api/v1/auth/mfa/confirm"),
        ("POST", "/api/v1/auth/mfa/disable"),
    }
    return (method.upper(), path) in routes or (
        method.upper() == "DELETE" and re.fullmatch(r"/api/v1/auth/sessions/\d+", path) is not None
    )


def _is_loopback_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address((value or "").strip().split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return value in {"localhost"}


def _is_local_request(request: Request) -> bool:
    if request.client is None or not _is_loopback_address(request.client.host):
        return False
    proxied_client = request.headers.get(PROXY_CLIENT_IP_HEADER, "").strip()
    return not proxied_client or _is_loopback_address(proxied_client)


def _requires_admin(method: str, path: str) -> bool:
    if path.startswith(ADMIN_ONLY_PREFIXES):
        return True
    normalized_method = method.upper()
    if normalized_method == "GET" and re.fullmatch(
        r"/api/v1/incident-snapshots/[^/]+/download",
        path,
    ):
        return True
    if normalized_method in MUTATING_METHODS and path.startswith(
        ("/api/v1/backup-center", "/api/v1/data-governance/object-storage")
    ):
        return True
    if normalized_method == "POST" and path in {
        "/api/v1/inspection/custom-metrics/test",
        "/api/v1/inspection/policies/import",
        "/api/v1/inspection/policies/import/preview",
    }:
        return True
    if normalized_method not in MUTATING_METHODS:
        return False
    if path.startswith((
        "/api/v1/incidents/",
        "/api/v1/incident-snapshots/",
        "/api/v1/incident-agents/",
        "/api/v1/incident-trigger-rules",
        "/api/v1/states/definitions",
        "/api/v1/states/observations",
        "/api/v1/states/replay",
        "/api/v1/fault-patterns",
        "/api/v1/scene-captures",
        "/api/v1/probe-templates",
        "/api/v1/probe-leases",
        "/api/v1/kernel-observability",
    )):
        return True
    if path.startswith("/api/v1/instances") or path.startswith("/api/v1/scheduler"):
        return True
    if path in {"/api/v1/setup/restart", "/api/v1/setup/shutdown"}:
        return True
    return bool(
        re.fullmatch(
            r"/api/v1/oracle/[^/]+/(?:ssh-test|ssh-diagnose|ssh-execute|tablespaces/deploy-autoextend|archive/deploy-cleanup)",
            path,
        )
    )


def _required_license_features(path: str) -> set[str]:
    normalized = path.rstrip("/") or "/"
    prefix_features = (
        ("/api/v1/diagnosis-center", "diagnosis_center"),
        ("/api/v1/incidents", "diagnosis_center"),
        ("/api/v1/incident-snapshots", "diagnosis_center"),
        ("/api/v1/incident-agents", "diagnosis_center"),
        ("/api/v1/incident-trigger-rules", "diagnosis_center"),
        ("/api/v1/states", "diagnosis_center"),
        ("/api/v1/fault-patterns", "diagnosis_center"),
        ("/api/v1/scene-captures", "diagnosis_center"),
        ("/api/v1/probe-templates", "diagnosis_center"),
        ("/api/v1/probe-leases", "diagnosis_center"),
        ("/api/v1/kernel-observability", "diagnosis_center"),
        ("/api/v1/ai", "ai_diagnosis"),
        ("/api/v1/oracle", "oracle_check"),
        ("/api/v1/mysql-ops", "mysql_check"),
        ("/api/v1/mysql", "mysql_check"),
        ("/api/v1/operations/mysql", "mysql_check"),
        ("/api/v1/postgresql", "postgresql_check"),
        ("/api/v1/operations/postgresql", "postgresql_check"),
        ("/api/v1/sqlserver", "sqlserver_check"),
        ("/api/v1/operations/sqlserver", "sqlserver_check"),
    )
    required = {
        feature
        for prefix, feature in prefix_features
        if normalized == prefix or normalized.startswith(f"{prefix}/")
    }
    if normalized.startswith("/api/v1/reports/") and normalized.endswith(("/export", "/download")):
        required.add("report_export")
    return required


def _is_bootstrap_request(request: Request) -> bool:
    if not _is_local_request(request):
        return False
    path = request.url.path
    if request.method == "GET" and path in BOOTSTRAP_STATUS_PATHS:
        return True
    if path.startswith("/api/v1/system/init/"):
        from app.services.sqlite_init_service import is_initialized, is_sqlite_mode

        return is_sqlite_mode() and not is_initialized() and path == "/api/v1/system/init/setup"
    if path.startswith("/api/v1/setup/"):
        from app.api.v1.setup import is_configured

        return not is_configured() and path in {
            "/api/v1/setup/license",
            "/api/v1/setup/license/upload",
            "/api/v1/setup/test-connection",
            "/api/v1/setup/init-database",
        }
    return False


def _webhook_authorized(request: Request) -> bool:
    expected = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
    supplied = request.headers.get("X-Mesguard-Webhook-Token", "")
    return bool(expected and supplied) and secrets.compare_digest(supplied, expected)


def _requires_authentication(path: str) -> bool:
    return path.startswith("/api/") or path in PROTECTED_NON_API_PATHS


def _is_incident_agent_transport(request: Request) -> bool:
    path = request.url.path
    method = request.method.upper()
    if method == "POST" and path == "/api/v1/incident-agents/enroll":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/heartbeat", path) and method == "POST":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/config", path) and method == "GET":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/identity/rotate", path) and method == "POST":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/commands/next", path) and method == "GET":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/commands/[^/]+/ack", path) and method == "POST":
        return True
    if re.fullmatch(r"/api/v1/incident-agents/[^/]+/triggers", path) and method == "POST":
        return True
    if re.fullmatch(
        r"/api/v1/kernel-observability/agents/[^/]+/(?:capabilities|health|events|stacks)",
        path,
    ) and method == "POST":
        return True
    if path == "/api/v1/incident-snapshots/uploads" and method == "POST":
        return True
    if re.fullmatch(r"/api/v1/incident-snapshots/uploads/[^/]+", path) and method == "GET":
        return True
    if re.fullmatch(r"/api/v1/incident-snapshots/uploads/[^/]+/resume", path) and method == "POST":
        return True
    if re.fullmatch(r"/api/v1/incident-snapshots/uploads/[^/]+/(?:chunks/\d+|complete)", path) and method in {"PUT", "POST"}:
        return True
    return False


def _metrics_token_authorized(request: Request) -> bool:
    expected = os.getenv("METRICS_BEARER_TOKEN", "").strip()
    supplied = get_bearer_token(request.headers.get("Authorization", ""))
    return bool(expected and supplied) and secrets.compare_digest(supplied, expected)


class LicenseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 开源版没有需要授权的付费能力，授权中间件属企业版机制。
        # 若在 OSS 保留，克隆仓库的人不提供 license.json 就连 API 都调不通——
        # 那不是"未授权"，而是产品根本跑不起来。
        if edition.BASIC_ONLY:
            return await call_next(request)
        path = request.url.path
        if path in LICENSE_WHITELIST:
            return await call_next(request)
        if any(path.startswith(p) for p in LICENSE_PREFIX_WHITELIST):
            return await call_next(request)
        if _is_bootstrap_request(request):
            return await call_next(request)
        if path == "/" or path.endswith((".css", ".js", ".map", ".ico", ".png", ".jpg", ".svg", ".woff", ".woff2")):
            return await call_next(request)

        try:
            from app.license.verify import get_license_info
            info = await run_in_threadpool(get_license_info)
            if not info.get("valid"):
                return JSONResponse(status_code=403, content={
                    "detail": "授权未激活",
                    "reason": info.get("reason"),
                    "machine_code": info.get("machine_code"),
                })
            required_features = _required_license_features(path)
            licensed_features = set(info.get("features") or [])
            missing_features = required_features - licensed_features
            if missing_features:
                return JSONResponse(status_code=403, content={
                    "detail": "当前授权未包含所需功能",
                    "reason": "feature_not_licensed",
                    "features": sorted(missing_features),
                })
        except Exception as exc:
            logger.warning("license check failed: %s", exc)
            return JSONResponse(status_code=503, content={"detail": "授权校验服务不可用"})

        return await call_next(request)


def _authorize_request(request: Request, auth_info: dict, body: bytes):
    """Keep policy queries and the complete Session lifetime in one worker."""
    from app.core.database import SessionLocal
    from app.services.access_control import (
        additional_actions_for_request,
        action_for_request,
        is_action_allowed,
    )
    from app.services.request_scope import scope_allows_request_sync

    path = request.url.path
    with SessionLocal() as db:
        # 模块清单属于"读自己可见范围"，任何已认证用户都应能读。
        # 若把它归到 platform.settings，受限用户会 403 → 前端拿不到清单 →
        # 退回"显示全部菜单"（fail-open），等于权限形同虚设。
        # 返回内容仍按用户实际授权过滤（见 module_config.list_modules）。
        if request.method.upper() == "GET" and path.rstrip("/") == "/api/v1/modules":
            request.state.required_action = "profile.modules"
            return None
        required_action = action_for_request(request.method, path)
        action_allowed, explicit_policy = is_action_allowed(
            db, int(auth_info.get("user_id") or 0), required_action,
        )
        if not action_allowed:
            return JSONResponse(status_code=403, content={
                "detail": "当前角色没有执行此操作的权限",
                "required_action": required_action,
            })
        if _requires_admin(request.method, path) and not explicit_policy:
            return JSONResponse(status_code=403, content={"detail": "需要管理员权限"})
        if request.method in {"POST", "PUT", "PATCH"} and (
            path.rstrip("/") == "/api/v1/instances" or path.startswith("/api/v1/instances/")
        ):
            try:
                payload = json.loads(body) if body else {}
            except (ValueError, UnicodeError):
                return JSONResponse(status_code=400, content={"detail": "请求体必须是有效 JSON"})
            for additional in additional_actions_for_request(request.method, path, payload):
                if not is_action_allowed(db, int(auth_info["user_id"]), additional)[0]:
                    return JSONResponse(status_code=403, content={
                        "detail": "修改连接或凭据需要独立的凭据管理权限",
                        "required_action": additional,
                    })
        if not scope_allows_request_sync(db, int(auth_info.get("user_id") or 0), request, body):
            return JSONResponse(status_code=403, content={"detail": "目标资产不在当前用户的数据范围内"})
        request.state.required_action = required_action
    return None


async def authentication_unavailable_response(request: Request, exc: AuthenticationUnavailable):
    return JSONResponse(status_code=503, content={"detail": "认证存储服务不可用，请稍后重试"})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in AUTH_WHITELIST:
            return await call_next(request)
        if path in AUTH_LOGIN_PATHS or path.startswith("/api/v1/auth/oidc/"):
            return await call_next(request)
        if _is_bootstrap_request(request):
            return await call_next(request)
        if _is_incident_agent_transport(request):
            return await call_next(request)
        if path in WEBHOOK_PATHS:
            if _webhook_authorized(request):
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "Webhook 身份校验失败"})
        if path == "/metrics" and _metrics_token_authorized(request):
            return await call_next(request)
        if not _requires_authentication(path):
            return await call_next(request)

        token = get_bearer_token(request.headers.get("Authorization", ""))
        try:
            auth_info = await run_in_threadpool(get_token_info, token) if token else None
        except AuthenticationUnavailable as exc:
            return await authentication_unavailable_response(request, exc)
        if not auth_info:
            return JSONResponse(status_code=401, content={"detail": "未登录或 Token 已过期，请重新登录"})
        request.state.auth = auth_info
        # 版本门禁：开源版只开放基础模块。独立于权限判断（对 admin 同样生效），
        # 因为这是产品版本能力而非账号授权；enterprise 版下这里恒为放行。
        if not edition.path_allowed(path):
            logger.info("edition_denied | edition=%s | path=%s", edition.EDITION, path)
            return JSONResponse(status_code=403, content={"detail": edition.denial(path)})
        if request.method == "POST" and re.fullmatch(
            re.escape(settings.API_V1_PREFIX) + r"/assistant/(?:dispatch-intent|plans(?:/[^/]+/(?:execute|revisions|approval/(?:request|approve|revoke)))?|batches(?:/prepare|/[^/]+/cancel)?|executions/[^/]+/(?:pause|resume|stop|probe)|workflows/(?:match|[^/]+/versions/[^/]+/prepare|plans/[^/]+/revisions))/?", path,
        ):
            try:
                await bounded_body(request, limit=ASSISTANT_BODY_LIMIT)
            except RequestBodyTooLarge:
                logger.warning("assistant_request_rejected code=request_too_large")
                return JSONResponse(status_code=413, content={"detail": {"code": "request_too_large"}})
            except ClientDisconnect:
                return JSONResponse(status_code=400, content={"detail": {"code": "request_body_interrupted"}})
        self_service = _is_auth_self_service(request.method, path)
        local_identity = auth_info.get("source") in {"db", "identity"} and int(auth_info.get("user_id") or 0) > 0
        if not local_identity:
            if (request.method, path.rstrip("/")) not in {
                ("GET", "/api/v1/auth/info"),
                ("GET", "/api/v1/auth/current-user"),
                ("POST", "/api/v1/auth/logout"),
            }:
                return JSONResponse(status_code=403, content={"detail": "当前认证身份没有平台业务权限，请使用已授权的本地或企业账号登录"})
        elif auth_info.get("role") != "admin" and not self_service:
            try:
                # Cache on the event loop for downstream replay before opening a Session.
                body = await request.body()
                denial = await run_in_threadpool(_authorize_request, request, auth_info, body)
                if denial is not None:
                    return denial
            except ClientDisconnect:
                return JSONResponse(status_code=400, content={"detail": "请求体读取中断"})
            except SQLAlchemyError:
                logger.warning("authorization policy storage unavailable")
                return JSONResponse(status_code=503, content={"detail": "权限校验服务不可用"})
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("应用启动 | debug=%s", settings.DEBUG)
    from app.runtime.process_roles import process_role
    role = process_role()
    if role in {"scheduler", "worker", "migrator"}:
        raise RuntimeError(f"{role} role must use its dedicated process entrypoint")
    from app.services.access_control import api_prefix_mismatch_message

    prefix_problem = api_prefix_mismatch_message(settings.API_V1_PREFIX)
    if prefix_problem:
        logger.error(prefix_problem)
    if not settings.is_sqlite:
        try:
            from app.api.v1.setup import is_configured
            if not is_configured():
                if role == "api":
                    raise RuntimeError("managed API requires an initialized platform database")
                logger.info("MySQL/MariaDB 尚未初始化，等待安装引导写入配置")
                yield
                return
        except RuntimeError as e:
            if role == "api":
                raise
            logger.warning("setup status check failed: %s", e)
        except Exception as e:
            logger.warning("setup status check failed: %s", e)
    target = database_target_summary(settings.DATABASE_URL)
    logger.info(
        "平台元数据库目标 | driver=%s | host=%s | port=%s | database=%s",
        target["driver"],
        target["host"],
        target["port"] or "default",
        target["database"] or "default",
    )
    try:
        if role == "api" or os.getenv("TRACEMIND_SCHEMA_ROLE", "all").strip().lower() == "verify":
            from app.core.database import verify_schema_ready
            verify_schema_ready(engine)
            applied_migrations = []
        else:
            applied_migrations = initialize_schema(engine)
        if applied_migrations:
            logger.info("平台元数据库迁移完成 | revisions=%s", ",".join(applied_migrations))
    except InspectionResultIntegrityError as e:
        logger.error("巡检历史数据不一致，启动被安全阻断 | code=%s | error=%s", e.code, e)
        raise RuntimeError(
            "巡检历史数据需要对账，不能继续启动并隐藏旧数据。"
            "请先执行 python scripts/migrate_schema.py inspection-results-check；"
            "详见 docs/operations/inspection-result-reconciliation.md"
        ) from e
    except SchemaUpgradeRequired as e:
        logger.error("平台数据库需要显式迁移或结构修复 | error=%s", e)
        raise RuntimeError(
            "平台数据库结构未就绪，请在备份并停止写入后执行结构迁移；"
            "详见 docs/operations/code-fix-upgrade-checklist.md"
        ) from e
    except Exception as e:
        logger.error(
            "平台元数据库初始化失败 | driver=%s | host=%s | port=%s | database=%s | error=%s",
            target["driver"],
            target["host"],
            target["port"] or "default",
            target["database"] or "default",
            redact_database_error(e),
        )
        raise RuntimeError("平台元数据库初始化失败，请检查数据库服务、网络和连接配置") from e
    if role == "api":
        # Candidate/API processes have no DDL, seed writer, scheduler or queue consumer.
        # The Router checks the durable migration receipt and exact schema again
        # through /health/ready/platform before sending application traffic.
        logger.info("managed API role ready for receipt verification")
        yield
        logger.info("managed API role stopped")
        return
    from app.core.database import SessionLocal
    from app.services.alert_rules import ensure_default_rules
    from app.services.inspection.policy_service import ensure_default_policies

    # Capability definitions are code-owned; the ledger is only a sync target. Syncing
    # here is what makes the three wired readers available on a fresh install, since no
    # authorized endpoint exists for it by design. It is best effort on purpose: a stale
    # or newer ledger must never keep the platform from starting.
    try:
        from app.services.capability_registry import sync_declarations_at_startup

        with SessionLocal() as db:
            sync_declarations_at_startup(db)
    except Exception as e:
        logger.warning("capability_registry_startup_sync failed: %s", e)

    # Native templates are installation data, independent of scheduler activation.
    with SessionLocal() as db:
        ensure_default_policies(db)

    try:
        with SessionLocal() as db:
            ensure_default_rules(db)
    except Exception as e:
        logger.warning("ensure_default_rules failed (safe to ignore in SQLite mode): %s", e)
    try:
        from app.services.perf_baseline import ensure_policies
        from app.services.system_settings import ensure_defaults as ensure_setting_defaults
        from app.services.observability.metric_normalizer import seed_default_mappings
        with SessionLocal() as db:
            ensure_policies(db)
            ensure_setting_defaults(db)
            seed_default_mappings(db)
    except Exception as e:
        logger.warning("ensure perf metric policies failed: %s", e)
    try:
        from app.cmdb.service import ensure_internal_assets

        with SessionLocal() as db:
            result = ensure_internal_assets(db)
            logger.info(
                "资产中心内置发现源就绪 | initialized=%s | assets=%s",
                result.get("initialized"),
                result.get("asset_count"),
            )
    except Exception as e:
        logger.warning("ensure internal CMDB assets failed: %s", e)
    try:
        from app.tasks.master_scheduler import start_master_scheduler
        start_master_scheduler()
    except Exception as e:
        logger.warning("start_master_scheduler failed: %s", e)
    try:
        # The API must not accept durable work when its configured embedded
        # consumer could not even start. Transient storage failures are retried
        # inside run_worker; a thread-start failure aborts lifespan startup.
        from app.tasks.plan_worker import start_embedded_worker

        start_embedded_worker()
        logger.info("数据库表结构同步完成")
        yield
    finally:
        try:
            from app.tasks.plan_worker import stop_embedded_worker

            stop_embedded_worker()
        except Exception as e:
            logger.warning("stop_embedded_capability_worker failed: %s", e)
        try:
            from app.tasks.master_scheduler import stop_master_scheduler
            stop_master_scheduler()
        except Exception as e:
            logger.warning("stop_master_scheduler failed: %s", e)
        logger.info("应用关闭")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

app.add_exception_handler(AuthenticationUnavailable, authentication_unavailable_response)

app.add_middleware(AuthMiddleware)
app.add_middleware(LicenseMiddleware)
app.add_middleware(AccessLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in routers:
    app.include_router(router, prefix=settings.API_V1_PREFIX)
    logger.info("注册路由 | prefix=%s | tags=%s", router.prefix, router.tags)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/health/live")
def health_live():
    return liveness()


@app.get("/health/ready")
def health_ready(db=Depends(get_db)):
    ready, payload = readiness(db)
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/health/ready/platform")
def health_ready_platform():
    from app.runtime.process_roles import platform_readiness

    try:
        return platform_readiness(engine, settings.DATABASE_URL)
    except Exception as exc:
        logger.warning("managed platform readiness refused: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": type(exc).__name__})


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(db=Depends(get_db)):
    return render_metrics(db)


SERVE_STATIC = os.getenv("SERVE_STATIC", "false").lower() == "true"
STATIC_DIR = os.getenv("STATIC_DIR", "")

if SERVE_STATIC and STATIC_DIR and os.path.isdir(STATIC_DIR):
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        from app.runtime.static_paths import resolve_static_file

        try:
            return FileResponse(resolve_static_file(STATIC_DIR, full_path))
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
