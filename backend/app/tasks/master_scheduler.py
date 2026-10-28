"""统一总调度器 — 每分钟驱动所有按时间执行的后台作业。
替代原先 3 个独立调度器（容量/AWR/性能基线），复用各自的到期处理逻辑。
"""
import logging
import threading
import time
from collections import Counter
from datetime import datetime

from app.core.database import SessionLocal
from app.core.database_options import redact_database_error
from app.models.perf_metric_policy import DBPerfMetricPolicy

logger = logging.getLogger("app.tasks.master_scheduler")

_scheduler = None
_SCHEDULER_LEASE_KEY = "master-due-tasks"
_SCHEDULER_OWNER_ID = None

BUSINESS_STATUSES = {"success", "failed", "partial", "skipped", "no_work", "unknown", "aborted"}


def aggregate_business_status(statuses) -> str:
    states = {status if isinstance(status, str) else "unknown" for status in statuses}
    if not states:
        return "no_work"
    if states - BUSINESS_STATUSES:
        states = (states & BUSINESS_STATUSES) | {"unknown"}
    if "partial" in states:
        return "partial"
    if states & {"failed", "aborted"}:
        return "partial" if "success" in states else "failed"
    if "unknown" in states:
        return "partial" if "success" in states else "unknown"
    if "success" in states:
        return "partial" if "skipped" in states else "success"
    return "skipped" if "skipped" in states else "no_work"


def _nonnegative_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _business_result(status, summary):
    summary = dict(summary)
    if "status" in summary and summary["status"] != status:
        summary["reported_status"] = summary["status"]
    summary["status"] = status
    return {"status": status, "summary": summary}


def normalize_business_result(result) -> dict:
    """Interpret known job contracts; returning from Python is not business success."""
    if isinstance(result, (list, tuple)):
        items = [normalize_business_result(item) for item in result]
        statuses = [item["status"] for item in items]
        return _business_result(aggregate_business_status(statuses),
                                {"count": len(items), "counts": dict(Counter(statuses)), "results": items})
    if not isinstance(result, dict):
        return _business_result("unknown", {"reason": "missing_or_unrecognized_business_result"})

    summary = {key: value for key, value in result.items()
               if key in {"status", "count", "processed", "success", "failed", "partial", "skipped",
                          "unknown", "no_work", "executed", "examined", "expired", "run_id"}
               and isinstance(value, (str, int, float, bool, type(None)))}
    # The inspection scheduler returns only identities, statuses and redacted messages.
    if isinstance(result.get("policy_results"), list):
        summary["policy_results"] = result["policy_results"]
    if isinstance(result.get("counts"), dict):
        summary["counts"] = {key: value for key, value in result["counts"].items()
                             if key in BUSINESS_STATUSES and _nonnegative_count(value)}
    if isinstance(result.get("retention"), dict):
        retention = result["retention"]
        retention_status = retention.get("status")
        summary["retention"] = {"status": retention_status if isinstance(retention_status, str)
                                and retention_status in BUSINESS_STATUSES else "unknown"}
        if _nonnegative_count(retention.get("deleted")):
            summary["retention"]["deleted"] = retention["deleted"]

    status = result.get("status")
    aliases = {"error": "failed", "no-work": "no_work", "plan_only": "skipped"}
    status = aliases.get(status, status) if isinstance(status, str) else None
    if status in BUSINESS_STATUSES:
        # Explicit failure/partial evidence must not be hidden by a success label.
        contrary = []
        for name in ("failed", "partial", "unknown", "skipped"):
            count = result.get(name)
            if _nonnegative_count(count) and count > 0:
                contrary.append(name)
            if summary.get("counts", {}).get(name, 0) > 0:
                contrary.append(name)
        for item in result.get("policy_results", []) if isinstance(result.get("policy_results"), list) else []:
            child_status = item.get("status") if isinstance(item, dict) else "unknown"
            if child_status != "success":
                contrary.append(child_status if isinstance(child_status, str) else "unknown")
        retention_status = summary.get("retention", {}).get("status")
        if retention_status and retention_status not in {"success", "no_work"}:
            contrary.append(retention_status)
        if contrary:
            status = aggregate_business_status([status, *contrary])
        return _business_result(status, summary)

    if {"executed", "failed"} <= result.keys() and all(
        _nonnegative_count(result[key]) for key in ("executed", "failed")
    ):
        executed, failed = result["executed"], result["failed"]
        # These legacy jobs count returned attempts in executed, but exceptions
        # only in failed. Do not claim an exact success count from the difference.
        status = ("partial" if executed > failed else "failed") if failed else "success" if executed else "no_work"
        return _business_result(status, summary)

    if {"examined", "expired", "failed"} <= result.keys() and all(
        _nonnegative_count(result[key]) for key in ("examined", "expired", "failed")
    ):
        examined, expired, failed = (result[key] for key in ("examined", "expired", "failed"))
        status = aggregate_business_status(
            (["success"] if expired else []) + (["failed"] if failed else [])
            + (["unknown"] if expired + failed != examined else [])
        )
        return _business_result(status, summary)

    if result.get("success") is False or (_nonnegative_count(result.get("failed")) and result["failed"] > 0):
        return _business_result("failed", summary)
    # processed/count are attempts, not delivery/collection outcomes, including 0.
    return _business_result("unknown", {**summary, "reason": "business_outcome_not_reported"})


def _owner_id() -> str:
    global _SCHEDULER_OWNER_ID
    if _SCHEDULER_OWNER_ID is None:
        from app.services.report_center import worker_identity

        _SCHEDULER_OWNER_ID = worker_identity()
    return _SCHEDULER_OWNER_ID


def run_due_perf_jobs() -> dict:
    """性能基线：处理到期的 per-instance 策略，并按保留设置清理过期指标。"""
    from app.services.perf_baseline import run_collect_by_policy
    from app.services import system_settings as settings_svc
    from app.models.perf_metric import DBPerfMetric
    from datetime import timedelta
    db = SessionLocal()
    policy_results = []
    try:
        now = datetime.now()
        policies = (
            db.query(DBPerfMetricPolicy)
            .filter(DBPerfMetricPolicy.enabled == 1)
            .filter((DBPerfMetricPolicy.next_run_time == None) | (DBPerfMetricPolicy.next_run_time <= now))  # noqa: E711
            .all()
        )
        for policy in policies:
            outcome = {"policy_id": policy.id, "instance_id": policy.instance_id}
            try:
                result = run_collect_by_policy(db, policy)
                policy_results.append({**outcome, "status": normalize_business_result(result)["status"],
                                       "policy_status_persisted": True})
            except Exception as exc:
                db.rollback()
                logger.error("性能基线采集失败 | instance_id=%s | error=%s", outcome["instance_id"], redact_database_error(exc))
                policy_results.append({**outcome, "status": "failed", "policy_status_persisted": False})
        # 清理过期性能指标（按系统参数保留天数）
        try:
            keep_days = settings_svc.get_int(db, "retention_perf_metric_days", 90)
            cutoff = now - timedelta(days=max(keep_days, 1))
            deleted = db.query(DBPerfMetric).filter(DBPerfMetric.capture_time < cutoff).delete(synchronize_session=False)
            db.commit()
            retention = {"status": "success" if deleted else "no_work", "deleted": deleted}
        except Exception as exc:
            logger.warning("清理过期性能指标失败: %s", redact_database_error(exc))
            db.rollback()
            retention = {"status": "failed"}
        statuses = [item["status"] for item in policy_results]
        # Cleanup success must not mask failed collection; cleanup failure still matters.
        status = aggregate_business_status(statuses + (["failed"] if retention["status"] == "failed" else []))
        if not statuses:
            status = retention["status"]
        return {"status": status, "processed": len(policy_results), "counts": dict(Counter(statuses)),
                "policy_results": policy_results, "retention": retention}
    finally:
        db.close()


def run_due_report_jobs():
    from app.services.report_center import run_due_report_schedules

    db = SessionLocal()
    try:
        runs = run_due_report_schedules(db, worker_id=_owner_id())
        # ORM records may be expired by retention commits. Materialize before close.
        return [{"run_id": row.id, "status": row.status} for row in runs]
    finally:
        db.close()




class _LeaseHeartbeat:
    def __init__(
        self,
        lease_key: str,
        owner_id: str,
        fencing_epoch: int,
        interval_seconds: int = 15,
        ttl_seconds: int = 60,
    ):
        self.lease_key = lease_key
        self.owner_id = owner_id
        self.fencing_epoch = fencing_epoch
        self.interval_seconds = interval_seconds
        self.ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="scheduler-lease-heartbeat", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=max(1, self.interval_seconds + 1))

    def _run(self):
        from app.services.report_center import heartbeat_scheduler_lease

        while not self._stop.wait(self.interval_seconds):
            db = SessionLocal()
            try:
                if not heartbeat_scheduler_lease(
                    db,
                    self.lease_key,
                    self.owner_id,
                    expected_epoch=self.fencing_epoch,
                    ttl_seconds=self.ttl_seconds,
                ):
                    logger.error("调度租约心跳失败，当前执行批次可能失去领导权 | owner=%s", self.owner_id)
                    self.lost.set()
                    return
            except Exception as exc:
                logger.error("调度租约心跳异常 | owner=%s | error=%s", self.owner_id, exc)
                db.rollback()
                self.lost.set()
                return
            finally:
                db.close()


def due_job_functions() -> tuple:
    """Registry of per-tick jobs, resolved lazily.

    Kept as its own callable instead of a literal inside :func:`run_due_tasks` so
    that ownership of "this consumer is actually scheduled" is assertable: a
    commented-out entry or a stale reference in a docstring cannot satisfy the
    check, whereas grepping the source for a name can be fooled by both.
    """
    from app.tasks.capacity_scheduler import run_due_capacity_jobs
    from app.tasks.awr_collect_scheduler import run_due_awr_jobs
    from app.tasks.inspection_scheduler import (
        run_due_inspection_job_expiry,
        run_due_inspection_job_overdue,
        run_due_inspection_job_recovery,
        run_due_inspection_jobs,
    )
    from app.tasks.observability_scheduler import run_due_observability_jobs
    from app.tasks.data_quality_scheduler import run_due_data_quality_jobs
    from app.tasks.notification_scheduler import run_due_notification_jobs
    from app.tasks.alert_action_scheduler import run_due_alert_action_jobs

    return (
        run_due_capacity_jobs,
        run_due_awr_jobs,
        run_due_perf_jobs,
        run_due_inspection_jobs,
        # Three consumers, not one: a job can die (recovery), never start (expiry) or run past
        # its deadline while ignoring a cancel (overdue). Folding them together would hide any
        # one of the three the day it stopped being scheduled.
        run_due_inspection_job_recovery,
        run_due_inspection_job_expiry,
        run_due_inspection_job_overdue,
        run_due_report_jobs,
        run_due_notification_jobs,
        run_due_observability_jobs,
        run_due_data_quality_jobs,
        run_due_alert_action_jobs,
    )


def run_due_tasks():
    """总入口：依次处理各类到期任务（各自独立容错）。"""
    from app.services.report_center import (
        claim_scheduler_lease_token,
        finish_dispatch_run,
        start_dispatch_run,
        validate_scheduler_lease,
    )

    owner_id = _owner_id()
    control_db = SessionLocal()
    heartbeat = None
    dispatch = None
    try:
        fencing_epoch = claim_scheduler_lease_token(
            control_db,
            _SCHEDULER_LEASE_KEY,
            owner_id,
            ttl_seconds=60,
        )
        if fencing_epoch is None:
            logger.debug("当前节点不是调度主节点，跳过本轮 | owner=%s", owner_id)
            return {"status": "standby", "owner_id": owner_id}
        dispatch = start_dispatch_run(
            control_db,
            _SCHEDULER_LEASE_KEY,
            owner_id,
            fencing_epoch=fencing_epoch,
        )
        heartbeat = _LeaseHeartbeat(_SCHEDULER_LEASE_KEY, owner_id, fencing_epoch)
        heartbeat.start()
        results = []
        jobs = due_job_functions()
        for fn in jobs:
            job_name = getattr(fn, "__name__", fn.__class__.__name__)
            lease_current = False
            if not heartbeat.lost.is_set():
                fence_db = SessionLocal()
                try:
                    lease_current = validate_scheduler_lease(
                        fence_db,
                        _SCHEDULER_LEASE_KEY,
                        owner_id,
                        fencing_epoch,
                    )
                finally:
                    fence_db.close()
            if not lease_current:
                heartbeat.lost.set()
                results.append({
                    "job": job_name,
                    "status": "aborted",
                    "dispatch_status": "aborted",
                    "duration_ms": 0,
                    "error": "scheduler fencing lease lost before job start",
                })
                continue
            started = time.monotonic()
            try:
                result = fn()
                outcome = normalize_business_result(result)
                results.append({
                    "job": job_name,
                    "status": outcome["status"],
                    "dispatch_status": "success",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "summary": outcome["summary"],
                })
            except Exception as exc:
                logger.error("定时任务执行异常 | job=%s | error=%s", job_name, exc, exc_info=True)
                results.append({
                    "job": job_name,
                    "status": "failed",
                    "dispatch_status": "failed",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "error": str(exc)[:1000],
                })
        if not heartbeat.lost.is_set():
            fence_db = SessionLocal()
            try:
                if not validate_scheduler_lease(
                    fence_db,
                    _SCHEDULER_LEASE_KEY,
                    owner_id,
                    fencing_epoch,
                ):
                    heartbeat.lost.set()
            finally:
                fence_db.close()
        if heartbeat.lost.is_set() and not any(item.get("status") == "aborted" for item in results):
            results.append({
                "job": "scheduler_fencing_guard",
                "status": "aborted",
                "dispatch_status": "aborted",
                "duration_ms": 0,
                "error": "scheduler fencing lease lost before dispatch completion",
            })
        finish_dispatch_run(control_db, dispatch, results)
        return {
            "status": dispatch.status,
            "dispatch_status": "aborted" if heartbeat.lost.is_set() else "success",
            "owner_id": owner_id,
            "fencing_epoch": fencing_epoch,
            "dispatch_uid": dispatch.dispatch_uid,
            "jobs": results,
        }
    except Exception as exc:
        logger.error("总调度批次启动失败 | owner=%s | error=%s", owner_id, exc, exc_info=True)
        control_db.rollback()
        if dispatch is not None:
            try:
                from app.models.report_center import SchedulerDispatchRun

                failed_dispatch = control_db.query(SchedulerDispatchRun).filter(SchedulerDispatchRun.id == dispatch.id).first()
                if failed_dispatch is not None:
                    failed_dispatch.status = "failed"
                    failed_dispatch.finished_at = datetime.now()
                    failed_dispatch.message = str(exc)[:2000]
                    control_db.commit()
            except Exception as persist_exc:
                control_db.rollback()
                logger.error("记录总调度失败状态异常 | owner=%s | error=%s", owner_id, persist_exc)
        return {"status": "failed", "dispatch_status": "failed", "owner_id": owner_id, "error": str(exc)}
    finally:
        if heartbeat:
            heartbeat.stop()
        control_db.close()


def start_master_scheduler():
    global _scheduler
    if _scheduler:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("APScheduler 未安装，定时任务不会自动运行")
        return None
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    _scheduler.add_job(
        run_due_tasks,
        "interval",
        minutes=1,
        id="unified_due_tasks",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )
    _scheduler.start()
    logger.info("统一定时任务调度器已启动")
    return _scheduler


def stop_master_scheduler():
    global _scheduler
    if _scheduler:
        # Keep the lease heartbeat alive until in-flight work and its writes drain.
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("统一定时任务调度器已停止")
    try:
        from app.services.report_center import release_scheduler_lease

        db = SessionLocal()
        try:
            release_scheduler_lease(db, _SCHEDULER_LEASE_KEY, _owner_id())
        finally:
            db.close()
    except Exception as exc:
        logger.warning("释放调度租约失败: %s", exc)


def get_scheduler_status() -> dict:
    from app.models.report_center import SchedulerDispatchRun, SchedulerLease
    from app.services.report_center import serialize_dispatch, serialize_lease

    db = SessionLocal()
    try:
        lease = db.query(SchedulerLease).filter(SchedulerLease.lease_key == _SCHEDULER_LEASE_KEY).first()
        dispatches = db.query(SchedulerDispatchRun).order_by(SchedulerDispatchRun.started_at.desc()).limit(20).all()
        lease_payload = serialize_lease(lease)
        dispatch_payload = [serialize_dispatch(row) for row in dispatches]
    except Exception as exc:
        logger.warning("读取调度状态失败: %s", exc)
        lease_payload = None
        dispatch_payload = []
    finally:
        db.close()
    return {
        "running": _scheduler is not None,
        "node_id": _owner_id(),
        "role": "leader" if lease_payload and lease_payload.get("owner_id") == _owner_id() and lease_payload.get("active") else "standby",
        "lease": lease_payload,
        "dispatch_history": dispatch_payload,
        "jobs": [
            {"id": job.id, "next_run_time": str(job.next_run_time) if job.next_run_time else None}
            for job in _scheduler.get_jobs()
        ] if _scheduler else [],
    }
