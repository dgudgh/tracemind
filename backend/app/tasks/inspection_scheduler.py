"""Scheduled inspection jobs driven by DBInspectionPolicy."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from app.core.database import SessionLocal
from app.core.database_options import redact_database_error
from app.models.db_instance import DBInstance
from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import policy_service, runner
from app.tasks.master_scheduler import aggregate_business_status

logger = logging.getLogger("app.tasks.inspection_scheduler")


def run_due_inspection_jobs() -> dict:
    db = SessionLocal()
    policy_results = []
    try:
        policy_service.ensure_default_policies(db)
        now = datetime.now()
        policies = db.query(DBInspectionPolicy).filter(DBInspectionPolicy.enabled == 1).all()
        for policy in policies:
            if not policy_service.is_policy_due(policy, now):
                continue
            outcome = {"policy_id": getattr(policy, "id", None), "policy_key": policy.policy_key, "instances": []}
            try:
                instances = _assigned_instances(db, policy)
                if not instances:
                    policy_service.mark_policy_run(policy, "skipped", "未分配启用实例，已跳过。", now)
                    db.commit()
                    policy_results.append({**outcome, "status": "skipped", "policy_status_persisted": True,
                                           "reason": "no_assigned_enabled_instances"})
                    continue
                failures = []
                for instance in instances:
                    try:
                        task_status, reason = _run_queued_inspection(db, instance, policy)
                        outcome["instances"].append({"instance_id": instance.id, "status": task_status})
                        if task_status in {"failed", "partial", "timed_out", "interrupted", "cancelled"}:
                            failures.append(f"实例 {instance.id}: {redact_database_error(str(reason))}")
                    except Exception as exc:
                        reason = redact_database_error(exc)
                        logger.error("定时巡检失败 | policy=%s | instance_id=%s | error=%s", policy.policy_key, instance.id, reason)
                        failures.append(f"实例 {instance.id}: {reason}")
                        outcome["instances"].append({"instance_id": instance.id, "status": "failed"})
                        db.rollback()
                counts = Counter(item["status"] for item in outcome["instances"])
                ok_count, partial_count = counts["success"], counts["partial"]
                status = aggregate_business_status(counts)
                message = f"已执行 {ok_count}/{len(instances)} 个实例。"
                if failures:
                    message = f"成功 {ok_count}，部分失败 {partial_count}，失败 {counts['failed']}，跳过 {counts['skipped']}，无任务 {counts['no_work']}。"
                    message += "；".join(failures[:3])[:1200]
                policy_service.mark_policy_run(policy, status, message, now)
                db.commit()
                policy_results.append({**outcome, "status": status, "counts": dict(counts), "policy_status_persisted": True})
            except Exception as exc:
                logger.error("巡检策略调度异常 | policy=%s | error=%s", policy.policy_key, redact_database_error(exc))
                db.rollback()
                status_persisted = False
                try:
                    policy_service.mark_policy_run(policy, "failed", redact_database_error(exc), now)
                    db.commit()
                    status_persisted = True
                except Exception:
                    db.rollback()
                policy_results.append({**outcome, "status": "failed", "reason": redact_database_error(exc),
                                       "policy_status_persisted": status_persisted})
        statuses = [item["status"] for item in policy_results]
        return {"status": aggregate_business_status(statuses), "processed": len(policy_results),
                "counts": dict(Counter(statuses)), "policy_results": policy_results}
    finally:
        db.close()


def _run_queued_inspection(db, instance: DBInstance, policy: DBInspectionPolicy) -> tuple[str, str]:
    """Execute one scheduled inspection through the durable queue.

    Scheduled runs used to bypass `jobs` entirely, so cancellations, retries,
    leases and progress never applied to them. They now enter the same control
    plane as any other run; the job row's terminal status is the outcome.
    """
    from uuid import uuid4

    from app.services.inspection import jobs

    db_type = jobs.db_type_for(policy, instance)
    row = jobs.enqueue(db, instance, db_type, policy, trigger_type="scheduled")
    token = uuid4().hex
    if jobs.claim(db, row.id, token) is None:
        return "skipped", "巡检任务已被取消或已被其他执行节点领取"
    finished = jobs.execute(db, row, token)
    status = finished.status
    if status not in {"success", "partial", "failed", "skipped", "no_work",
                      "cancelled", "timed_out", "interrupted"}:
        # An unrecognised status must never be summarised as healthy.
        status = "failed"
    return status, finished.failure_reason or "巡检未返回有效的成功结果"


def run_due_inspection_job_recovery() -> dict:
    """Close runs whose executor vanished.

    ``jobs.reap_interrupted`` existed and was called by nothing but tests: a run whose node
    died stayed ``running`` for ever, and the list showed work in progress that would never
    finish. This consumer is what turns the function into behaviour; ``master_scheduler``
    registers it and a test asserts the registration is real.
    """
    from app.services.inspection import jobs

    db = SessionLocal()
    try:
        interrupted = jobs.reap_interrupted(db)
        return {"status": "success" if interrupted else "no_work", "count": interrupted,
                "interrupted": interrupted}
    except Exception as exc:
        db.rollback()
        logger.error("巡检失主任务回收失败 | error=%s", redact_database_error(exc))
        return {"status": "failed", "reason": redact_database_error(exc), "count": 0}
    finally:
        db.close()


def run_due_inspection_job_expiry() -> dict:
    """Expire queued runs that were never claimed within their own timeout.

    A queued job that is never picked up is indistinguishable from one that is about to run, so
    it has to reach a terminal state of its own instead of waiting for a claim that never comes.
    """
    from app.services.inspection import jobs

    db = SessionLocal()
    try:
        expired = jobs.reap_expired(db)
        return {"status": "success" if expired else "no_work", "count": expired,
                "expired": expired}
    except Exception as exc:
        db.rollback()
        logger.error("巡检排队超时回收失败 | error=%s", redact_database_error(exc))
        return {"status": "failed", "reason": redact_database_error(exc), "count": 0}
    finally:
        db.close()


def run_due_inspection_job_overdue() -> dict:
    """Terminate running jobs that overran their deadline or ignored a cancel.

    The third state a run can get stuck in, after "its node died" (recovery) and "it was never
    claimed" (expiry): the executor is alive and heartbeating, so the lease never expires, while
    the deadline and the operator's cancel request are both ignored. Nothing else in the platform
    could end that run.
    """
    from app.services.inspection import jobs

    db = SessionLocal()
    try:
        reaped = jobs.reap_overdue(db)
        return {"status": "success" if reaped else "no_work", "count": reaped,
                "reaped": reaped}
    except Exception as exc:
        db.rollback()
        logger.error("巡检超期运行强制终结失败 | error=%s", redact_database_error(exc))
        return {"status": "failed", "reason": redact_database_error(exc), "count": 0}
    finally:
        db.close()


def _assigned_instances(db, policy: DBInspectionPolicy) -> list[DBInstance]:
    report_config = policy.report_config_json or {}
    ids = [int(value) for value in (report_config.get("instance_ids") or []) if str(value).strip()]
    if not ids:
        return []
    db_types = [str(value).lower() for value in (policy.db_types_json or []) if value != "host"]
    query = db.query(DBInstance).filter(DBInstance.enabled == 1, DBInstance.id.in_(ids))
    if db_types:
        query = query.filter(DBInstance.db_type.in_(db_types))
    return query.order_by(DBInstance.id.asc()).all()
