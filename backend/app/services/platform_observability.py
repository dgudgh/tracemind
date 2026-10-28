from __future__ import annotations

import os
import platform
import resource
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


HTTP_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
PROCESS_STARTED_AT = time.time()


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight = 0
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._duration_count: Counter[tuple[str, str]] = Counter()
        self._duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._duration_buckets: Counter[tuple[str, str, float]] = Counter()

    def request_started(self) -> None:
        with self._lock:
            self._inflight += 1

    def request_finished(self, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        normalized_method = (method or "UNKNOWN").upper()[:12]
        normalized_route = route if route.startswith("/") else "/unknown"
        status_class = f"{max(0, int(status_code)) // 100}xx"
        key = (normalized_method, normalized_route)
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._requests[(normalized_method, normalized_route, status_class)] += 1
            self._duration_count[key] += 1
            self._duration_sum[key] += max(0.0, duration_seconds)
            for bucket in HTTP_BUCKETS:
                if duration_seconds <= bucket:
                    self._duration_buckets[(normalized_method, normalized_route, bucket)] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "inflight": self._inflight,
                "requests": dict(self._requests),
                "duration_count": dict(self._duration_count),
                "duration_sum": dict(self._duration_sum),
                "duration_buckets": dict(self._duration_buckets),
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._inflight = 0
            self._requests.clear()
            self._duration_count.clear()
            self._duration_sum.clear()
            self._duration_buckets.clear()


runtime_metrics = RuntimeMetrics()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def route_template(request) -> str:
    route = request.scope.get("route") if hasattr(request, "scope") else None
    path = getattr(route, "path", None)
    if path:
        return str(path)
    raw_path = str(getattr(getattr(request, "url", None), "path", "/unknown"))
    if raw_path in {"/health", "/health/live", "/health/ready", "/metrics"}:
        return raw_path
    return "/unmatched"


def liveness() -> dict:
    return {
        "status": "ok",
        "service": "tracemind-platform",
        "pid": os.getpid(),
        "uptime_seconds": round(max(0.0, time.time() - PROCESS_STARTED_AT), 3),
        "runtime": platform.python_version(),
    }


def readiness(db: Session) -> tuple[bool, dict]:
    checks: dict[str, dict] = {}
    try:
        started = time.monotonic()
        db.execute(text("SELECT 1")).scalar_one()
        checks["database"] = {
            "status": "ready",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception as exc:
        db.rollback()
        checks["database"] = {"status": "failed", "error": type(exc).__name__}

    if checks["database"]["status"] != "ready":
        checks["schema"] = {"status": "not_checked", "reason": "database_unavailable"}
    else:
        try:
            # Reuse the successful probe connection; a failed database must not be retried here.
            inspector = inspect(db.connection())
            required_tables = {
                "db_instance",
                "scheduler_lease",
                "scheduler_dispatch_run",
                "security_audit_event",
            }
            existing = {name for name in required_tables if inspector.has_table(name)}
            missing = sorted(required_tables - existing)
            checks["schema"] = {
                "status": "ready" if not missing else "failed",
                "missing_tables": missing,
            }
        except Exception as exc:
            checks["schema"] = {"status": "failed", "error": type(exc).__name__}

    ready = all(item.get("status") == "ready" for item in checks.values())
    return ready, {
        "status": "ready" if ready else "not_ready",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value)}"' for name, value in labels.items()) + "}"


def _process_rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage if platform.system() == "Darwin" else usage * 1024)


def _safe_count(db: Session, model, *filters) -> int:
    try:
        query = db.query(model)
        if filters:
            query = query.filter(*filters)
        return int(query.count())
    except Exception:
        db.rollback()
        return -1


def render_runtime_metrics(db: Session) -> str:
    from app.models.report_center import SchedulerDispatchRun, SchedulerLease

    snapshot = runtime_metrics.snapshot()
    lines = [
        "# HELP tracemind_process_uptime_seconds Platform process uptime.",
        "# TYPE tracemind_process_uptime_seconds gauge",
        f"tracemind_process_uptime_seconds {max(0.0, time.time() - PROCESS_STARTED_AT):.3f}",
        "# HELP tracemind_process_resident_memory_bytes Platform resident memory.",
        "# TYPE tracemind_process_resident_memory_bytes gauge",
        f"tracemind_process_resident_memory_bytes {_process_rss_bytes()}",
        "# HELP tracemind_http_requests_inflight Current HTTP requests.",
        "# TYPE tracemind_http_requests_inflight gauge",
        f"tracemind_http_requests_inflight {snapshot['inflight']}",
        "# HELP tracemind_http_requests_total HTTP requests by route and status class.",
        "# TYPE tracemind_http_requests_total counter",
    ]
    for (method, route, status_class), count in sorted(snapshot["requests"].items()):
        lines.append(
            f"tracemind_http_requests_total{_labels(method=method, route=route, status_class=status_class)} {count}"
        )
    lines.extend([
        "# HELP tracemind_http_request_duration_seconds HTTP request duration histogram.",
        "# TYPE tracemind_http_request_duration_seconds histogram",
    ])
    for (method, route), count in sorted(snapshot["duration_count"].items()):
        for bucket in HTTP_BUCKETS:
            value = snapshot["duration_buckets"].get((method, route, bucket), 0)
            lines.append(
                f"tracemind_http_request_duration_seconds_bucket{_labels(method=method, route=route, le=bucket)} {value}"
            )
        lines.append(
            f"tracemind_http_request_duration_seconds_bucket{_labels(method=method, route=route, le='+Inf')} {count}"
        )
        lines.append(
            f"tracemind_http_request_duration_seconds_sum{_labels(method=method, route=route)} {snapshot['duration_sum'][(method, route)]:.6f}"
        )
        lines.append(
            f"tracemind_http_request_duration_seconds_count{_labels(method=method, route=route)} {count}"
        )

    active_lease = 0
    try:
        now = _utcnow()
        active_lease = db.query(SchedulerLease).filter(
            SchedulerLease.owner_id.isnot(None), SchedulerLease.lease_until > now
        ).count()
    except Exception:
        db.rollback()
        active_lease = -1
    lines.extend([
        "# HELP tracemind_scheduler_active_leases Active scheduler leader leases.",
        "# TYPE tracemind_scheduler_active_leases gauge",
        f"tracemind_scheduler_active_leases {active_lease}",
        "# HELP tracemind_scheduler_dispatch_total Scheduler dispatch runs by status.",
        "# TYPE tracemind_scheduler_dispatch_total gauge",
    ])
    for status in ("running", "success", "partial", "failed"):
        lines.append(
            f"tracemind_scheduler_dispatch_total{_labels(status=status)} "
            f"{_safe_count(db, SchedulerDispatchRun, SchedulerDispatchRun.status == status)}"
        )

    try:
        pool = db.get_bind().pool
        checked_out = pool.checkedout() if hasattr(pool, "checkedout") else -1
        size = pool.size() if hasattr(pool, "size") else -1
        overflow = pool.overflow() if hasattr(pool, "overflow") else -1
    except Exception:
        checked_out = size = overflow = -1
    lines.extend([
        "# HELP tracemind_database_pool_connections Database pool connections by state.",
        "# TYPE tracemind_database_pool_connections gauge",
        f"tracemind_database_pool_connections{_labels(state='checked_out')} {checked_out}",
        f"tracemind_database_pool_connections{_labels(state='size')} {size}",
        f"tracemind_database_pool_connections{_labels(state='overflow')} {overflow}",
    ])
    return "\n".join(lines) + "\n"
