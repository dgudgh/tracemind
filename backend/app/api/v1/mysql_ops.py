import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import instances as instance_service
from app.services.ai_degradation import AI_NOT_CONFIGURED, degradation, manual_for
from app.services.mysql_basic import get_basic_info, get_variables, get_status
from app.services.mysql_capacity import get_schema_sizes, get_large_tables
from app.services.mysql_binlog import get_binlog_overview, generate_purge_plan
from app.services.mysql_innodb import get_innodb_status
from app.services.mysql_lock import get_lock_transaction_center, get_transactions, get_lock_waits, generate_kill_plan
from app.services.mysql_deadlock_history import build_deadlock_history_snapshot, build_deadlock_trend_snapshot
from app.services.mysql_slow_sql import get_slow_sql_center, get_slow_sql_top
from app.services.mysql_user import get_users, get_user_grants
from app.services.mysql_table_index import (
    build_add_index_plan,
    build_add_primary_key_plan,
    build_analyze_table_plan,
    build_archive_suggestion,
    build_drop_index_plan,
    build_optimize_table_plan,
    generate_optimize_plan,
    get_duplicate_indexes,
    get_fragmentation,
    get_no_primary_key_tables,
    get_table_index_center,
)
from app.services.mysql_replication import get_replication_status
from app.services.mysql_backup import generate_backup_plan
from app.services.mysql_ops_extra import (
    generate_inspect_ai_plan,
    generate_metadata_backup_plan,
    generate_mycnf_backup_plan,
    get_error_log_info,
    get_latest_deadlock,
    get_mgr_status,
    get_mysql_overview,
    get_relay_log_status,
    get_security_summary,
)
from app.services.mysql_table_statistics import (
    build_analyze_table_plan as build_mysql_stats_analyze_plan,
    build_histogram_plan as build_mysql_stats_histogram_plan,
    get_table_statistics_center,
)
from app.analyzers.mysql.mysql_diagnosis_engine import (
    build_ai_analysis_payload,
    build_ticket_plan_payload,
    get_latest_mysql_diagnosis,
    get_mysql_diagnosis,
    get_mysql_diagnosis_findings,
    get_mysql_diagnosis_modules,
    run_mysql_diagnosis,
)

logger = logging.getLogger("app.api.mysql_ops")
router = APIRouter(prefix="/mysql-ops", tags=["MySQL 日常运维"])
operations_mysql_router = APIRouter(prefix="/operations/mysql", tags=["MySQL operations"])


def _get(db: Session, instance_id: int):
    obj = instance_service.get_by_id(db, instance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Instance not found")
    return obj


@router.get("/modules")
def api_modules():
    return {
        "items": [
            {"key": "basic", "name": "基础信息", "category": "overview"},
            {"key": "variables", "name": "参数管理", "category": "overview"},
            {"key": "capacity", "name": "容量监控", "category": "space"},
            {"key": "binlog", "name": "Binlog 管理", "category": "log"},
            {"key": "relaylog", "name": "Relay Log", "category": "log"},
            {"key": "errorlog", "name": "Error Log", "category": "log"},
            {"key": "innodb", "name": "InnoDB 状态", "category": "engine"},
            {"key": "locks", "name": "锁与事务", "category": "engine"},
            {"key": "deadlocks", "name": "死锁分析", "category": "engine"},
            {"key": "slowsql", "name": "慢 SQL", "category": "performance"},
            {"key": "tables", "name": "表和索引", "category": "governance"},
            {"key": "statistics", "name": "统计信息", "category": "governance"},
            {"key": "replication", "name": "复制/高可用", "category": "ha"},
            {"key": "mgr", "name": "MGR 状态", "category": "ha"},
            {"key": "users", "name": "用户权限", "category": "security"},
            {"key": "backup", "name": "备份恢复", "category": "backup"},
            {"key": "inspectai", "name": "巡检诊断", "category": "diagnosis"},
        ]
    }


@router.get("/{instance_id}/overview")
def api_overview(instance_id: int, db: Session = Depends(get_db)):
    return get_mysql_overview(_get(db, instance_id))


@router.get("/{instance_id}/basic-info")
def api_basic_info(instance_id: int, db: Session = Depends(get_db)):
    return get_basic_info(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/overview")
def api_operations_mysql_overview(instance_id: int, db: Session = Depends(get_db)):
    inst = _get(db, instance_id)
    overview = get_mysql_overview(inst)
    overview["basic_info"] = get_basic_info(inst).get("summary", {})
    return overview


@operations_mysql_router.get("/{instance_id}/basic-info")
def api_operations_mysql_basic_info(instance_id: int, db: Session = Depends(get_db)):
    return get_basic_info(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/basic-info/raw")
def api_operations_mysql_basic_raw(instance_id: int, db: Session = Depends(get_db)):
    data = get_basic_info(_get(db, instance_id))
    return {"raw": data.get("raw", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/basic-info/variables")
def api_operations_mysql_basic_variables(instance_id: int, search: str = "", db: Session = Depends(get_db)):
    return {"items": get_variables(_get(db, instance_id), search)}


@operations_mysql_router.get("/{instance_id}/basic-info/status")
def api_operations_mysql_basic_status(instance_id: int, search: str = "", db: Session = Depends(get_db)):
    return {"items": get_status(_get(db, instance_id), search)}


@operations_mysql_router.get("/{instance_id}/innodb")
def api_operations_mysql_innodb(instance_id: int, db: Session = Depends(get_db)):
    return get_innodb_status(_get(db, instance_id))


@router.get("/{instance_id}/variables")
def api_variables(instance_id: int, search: str = "", db: Session = Depends(get_db)):
    return {"items": get_variables(_get(db, instance_id), search)}


@router.get("/{instance_id}/status")
def api_status(instance_id: int, search: str = "", db: Session = Depends(get_db)):
    return {"items": get_status(_get(db, instance_id), search)}


@router.get("/{instance_id}/capacity/schemas")
def api_capacity_schemas(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_schema_sizes(_get(db, instance_id))}


@router.get("/{instance_id}/capacity/tables")
def api_capacity_tables(instance_id: int, limit: int = 50, db: Session = Depends(get_db)):
    return {"items": get_large_tables(_get(db, instance_id), limit)}


@router.get("/{instance_id}/binlog/overview")
def api_binlog_overview(instance_id: int, db: Session = Depends(get_db)):
    return get_binlog_overview(_get(db, instance_id))


@router.get("/{instance_id}/binlog")
def api_binlog_alias(instance_id: int, db: Session = Depends(get_db)):
    return get_binlog_overview(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/binlog")
def api_operations_mysql_binlog(instance_id: int, db: Session = Depends(get_db)):
    return get_binlog_overview(_get(db, instance_id))


@router.post("/{instance_id}/plans/binlog-purge")
def api_binlog_purge(instance_id: int, before_file: str = "", before_date: str = "", retention_days: int = 7, db: Session = Depends(get_db)):
    return generate_purge_plan(_get(db, instance_id), before_file, before_date, retention_days)


@router.get("/{instance_id}/innodb/status")
def api_innodb_status(instance_id: int, db: Session = Depends(get_db)):
    return get_innodb_status(_get(db, instance_id))


@router.get("/{instance_id}/locks/transactions")
def api_lock_transactions(instance_id: int, db: Session = Depends(get_db)):
    return get_lock_transaction_center(_get(db, instance_id))


@router.get("/{instance_id}/locks/waits")
def api_lock_waits(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_lock_waits(_get(db, instance_id))}


@router.get("/{instance_id}/transactions")
def api_transactions_alias(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_transactions(_get(db, instance_id))}


@router.get("/{instance_id}/locks")
def api_locks_alias(instance_id: int, db: Session = Depends(get_db)):
    return get_lock_transaction_center(_get(db, instance_id))


@router.get("/{instance_id}/deadlocks")
def api_deadlocks(instance_id: int, db: Session = Depends(get_db)):
    return get_latest_deadlock(_get(db, instance_id))


@router.post("/{instance_id}/plans/kill-session")
def api_kill_session(instance_id: int, thread_id: int, action: str = "kill_session", db: Session = Depends(get_db)):
    return generate_kill_plan(_get(db, instance_id), thread_id, action)


@operations_mysql_router.get("/{instance_id}/locks-transactions/overview")
def api_operations_mysql_lock_overview(instance_id: int, db: Session = Depends(get_db)):
    return get_lock_transaction_center(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/locks-transactions/sessions")
def api_operations_mysql_lock_sessions(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("sessions", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/transactions")
def api_operations_mysql_lock_transactions(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("transactions", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/long-transactions")
def api_operations_mysql_long_transactions(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("long_transactions", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/lock-waits")
def api_operations_mysql_lock_waits(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("lock_waits", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/blocking-chain")
def api_operations_mysql_blocking_chain(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("blocking_chains", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/deadlocks")
def api_operations_mysql_deadlock_detail(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    items = data.get("sections", {}).get("deadlocks", [])
    return {
        "items": items,
        "deadlock": items[0] if items else {},
        "raw": data.get("raw", {}).get("innodb_status", ""),
        "warnings": data.get("warnings", []),
    }


@operations_mysql_router.get("/{instance_id}/locks-transactions/deadlocks/history")
def api_operations_mysql_deadlock_history(instance_id: int, db: Session = Depends(get_db)):
    return build_deadlock_history_snapshot(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/locks-transactions/deadlocks/trend")
def api_operations_mysql_deadlock_trend(instance_id: int, db: Session = Depends(get_db)):
    return build_deadlock_trend_snapshot(_get(db, instance_id))


@operations_mysql_router.post("/{instance_id}/locks-transactions/deadlocks/analyze")
def api_operations_mysql_deadlock_analyze(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    items = data.get("sections", {}).get("deadlocks", [])
    deadlock = items[0] if items else {}
    return {
        "status": "ok",
        "message": "已生成 MySQL 死锁规则分析。未配置大模型时仅返回结构化规则结论。",
        "deadlock": deadlock,
        "analysis": deadlock.get("analysis", {}),
        "warnings": data.get("warnings", []),
    }


@operations_mysql_router.get("/{instance_id}/locks-transactions/metadata-locks")
def api_operations_mysql_metadata_locks(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("metadata_locks", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/table-locks")
def api_operations_mysql_table_locks(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": [data.get("sections", {}).get("table_lock_stats", {})], "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/wait-events")
def api_operations_mysql_wait_events(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("wait_events", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/locks-transactions/hot-objects")
def api_operations_mysql_hot_objects(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {"items": data.get("sections", {}).get("hot_objects", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.post("/{instance_id}/locks-transactions/kill-session-plan")
def api_operations_mysql_kill_session_plan(instance_id: int, thread_id: int, db: Session = Depends(get_db)):
    return generate_kill_plan(_get(db, instance_id), thread_id, "kill_session")


@operations_mysql_router.post("/{instance_id}/locks-transactions/kill-query-plan")
def api_operations_mysql_kill_query_plan(instance_id: int, thread_id: int, db: Session = Depends(get_db)):
    return generate_kill_plan(_get(db, instance_id), thread_id, "kill_query")


@operations_mysql_router.post("/{instance_id}/locks-transactions/ai-analyze")
def api_operations_mysql_lock_ai_analyze(instance_id: int, db: Session = Depends(get_db)):
    data = get_lock_transaction_center(_get(db, instance_id))
    return {
        "status": "plan_only",
        "message": "已生成 MySQL 锁与事务规则诊断摘要。未配置大模型时仅返回规则结论。",
        "summary": data.get("summary", {}),
        "risk_items": data.get("risk_items", []),
        "warnings": data.get("warnings", []),
    }


@router.get("/{instance_id}/slow-sql/top")
def api_slow_sql_top(instance_id: int, limit: int = 50, time_range: str = "24h", db: Session = Depends(get_db)):
    return get_slow_sql_center(_get(db, instance_id), limit, time_range)


@router.get("/{instance_id}/slow-sql")
def api_slow_sql_alias(instance_id: int, limit: int = 50, time_range: str = "24h", db: Session = Depends(get_db)):
    return get_slow_sql_center(_get(db, instance_id), limit, time_range)


@router.get("/{instance_id}/top-sql")
def api_top_sql(instance_id: int, limit: int = 50, time_range: str = "24h", db: Session = Depends(get_db)):
    return get_slow_sql_center(_get(db, instance_id), limit, time_range)


@operations_mysql_router.get("/{instance_id}/slow-sql/overview")
def api_operations_mysql_slow_sql_overview(instance_id: int, limit: int = 200, time_range: str = "24h", db: Session = Depends(get_db)):
    return get_slow_sql_center(_get(db, instance_id), limit, time_range)


@operations_mysql_router.get("/{instance_id}/slow-sql/top")
def api_operations_mysql_slow_sql_top(instance_id: int, limit: int = 200, time_range: str = "24h", db: Session = Depends(get_db)):
    return get_slow_sql_center(_get(db, instance_id), limit, time_range)


@operations_mysql_router.get("/{instance_id}/slow-sql/digests")
def api_operations_mysql_slow_sql_digests(instance_id: int, limit: int = 200, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), limit, time_range)
    return {"items": data.get("digest_details", []), "summary": data.get("summary", {}), "time_range": data.get("time_range"), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/slow-sql/table-profile")
def api_operations_mysql_slow_sql_table_profile(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    return {"items": data.get("table_profile", []), "time_range": data.get("time_range"), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/slow-sql/index-profile")
def api_operations_mysql_slow_sql_index_profile(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    return {"items": data.get("recommendations", []), "time_range": data.get("time_range"), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/slow-sql/trend")
def api_operations_mysql_slow_sql_trend(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 50, time_range)
    return data.get("trend", {})


@operations_mysql_router.get("/{instance_id}/slow-sql/config")
def api_operations_mysql_slow_sql_config(instance_id: int, db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 20)
    return {"config": data.get("config", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/slow-sql/raw")
def api_operations_mysql_slow_sql_raw(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    return {"raw": data.get("raw", {}), "digest_details": data.get("digest_details", []), "time_range": data.get("time_range"), "warnings": data.get("warnings", [])}


@operations_mysql_router.post("/{instance_id}/slow-sql/collect")
def api_operations_mysql_slow_sql_collect(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    return {"status": "ok", "message": "已刷新 Performance Schema 慢 SQL 摘要。历史快照入库将在后续任务启用。", "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.post("/{instance_id}/slow-sql/analyze")
def api_operations_mysql_slow_sql_analyze(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    return {"status": "ok", "message": "已生成 MySQL 慢 SQL 规则诊断。", "summary": data.get("summary", {}), "risk_items": data.get("risk_items", []), "recommendations": data.get("recommendations", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.post("/{instance_id}/slow-sql/optimize-plan")
def api_operations_mysql_slow_sql_optimize_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    schema = str(payload.get("schema_name") or payload.get("schema") or "schema")
    table = str(payload.get("table_name") or payload.get("table") or "table")
    columns = payload.get("columns") or ["column_name"]
    cols = ", ".join([f"`{str(col).strip('`')}`" for col in columns])
    idx_name = payload.get("index_name") or f"idx_{'_'.join([str(c).strip('`') for c in columns])}"
    return {
        "status": "plan_only",
        "message": "已生成慢 SQL 索引优化方案，不直接执行。",
        "sql": f"ALTER TABLE `{schema.strip('`')}`.`{table.strip('`')}` ADD INDEX `{str(idx_name).strip('`')}` ({cols});",
        "warnings": [
            "新增索引会增加写入成本。",
            "生产执行前请确认字段选择性、磁盘空间、维护窗口和回滚方案。",
            "大表 DDL 建议使用在线 DDL 或 pt-online-schema-change。",
        ],
    }


@operations_mysql_router.post("/{instance_id}/slow-sql/ai-analyze")
def api_operations_mysql_slow_sql_ai_analyze(instance_id: int, time_range: str = "24h", db: Session = Depends(get_db)):
    """只返回规则结果；本入口不读 AI 配置、不调用模型，也不承诺模型结论。"""
    data = get_slow_sql_center(_get(db, instance_id), 200, time_range)
    state = degradation(AI_NOT_CONFIGURED, local_result_available=True, reason_code="not_applicable")
    return {
        "status": AI_NOT_CONFIGURED,
        "result_source": "local_rules",
        "message": "本次仅返回 MySQL 慢 SQL 规则诊断结果，未调用大模型。",
        "degradation": state,
        "manual": manual_for(state, "slow_sql.raw_list"),
        "summary": data.get("summary", {}),
        "risk_items": data.get("risk_items", []),
        "recommendations": data.get("recommendations", []),
    }


@router.get("/{instance_id}/users")
def api_users(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_users(_get(db, instance_id))}


@router.get("/{instance_id}/users/{user}/grants")
def api_user_grants(instance_id: int, user: str, host: str = "%", db: Session = Depends(get_db)):
    return {"grants": get_user_grants(_get(db, instance_id), user, host)}


@router.get("/{instance_id}/tables/no-primary-key")
def api_no_pk(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_no_primary_key_tables(_get(db, instance_id))}


@router.get("/{instance_id}/no-pk-tables")
def api_no_pk_alias(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_no_primary_key_tables(_get(db, instance_id))}


@router.get("/{instance_id}/tables-indexes/overview")
def api_table_index_center(instance_id: int, db: Session = Depends(get_db)):
    return get_table_index_center(_get(db, instance_id))


@operations_mysql_router.get("/{instance_id}/tables-indexes/overview")
def api_operations_table_index_center(instance_id: int, db: Session = Depends(get_db)):
    return get_table_index_center(_get(db, instance_id))


@router.get("/{instance_id}/tables/fragmentation")
def api_fragmentation(instance_id: int, threshold_mb: int = 100, db: Session = Depends(get_db)):
    return {"items": get_fragmentation(_get(db, instance_id), threshold_mb)}


@router.get("/{instance_id}/large-tables")
def api_large_tables(instance_id: int, limit: int = 50, db: Session = Depends(get_db)):
    return {"items": get_large_tables(_get(db, instance_id), limit)}


@router.get("/{instance_id}/fragmentation")
def api_fragmentation_alias(instance_id: int, threshold_mb: int = 100, db: Session = Depends(get_db)):
    return {"items": get_fragmentation(_get(db, instance_id), threshold_mb)}


@router.get("/{instance_id}/statistics")
def api_statistics(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    return get_table_statistics_center(_get(db, instance_id), limit)


@operations_mysql_router.get("/{instance_id}/table-statistics/overview")
def api_operations_mysql_table_statistics_overview(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    return get_table_statistics_center(_get(db, instance_id), limit)


@operations_mysql_router.get("/{instance_id}/table-statistics/tables")
def api_operations_mysql_table_statistics_tables(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("table_stats", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/stale")
def api_operations_mysql_table_statistics_stale(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("stale_stats", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/cardinality")
def api_operations_mysql_table_statistics_cardinality(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("cardinality", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/row-estimation")
def api_operations_mysql_table_statistics_row_estimation(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("row_estimation", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/histograms")
def api_operations_mysql_table_statistics_histograms(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("histograms", []), "candidates": data.get("sections", {}).get("histogram_candidates", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/partitions")
def api_operations_mysql_table_statistics_partitions(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("partition_stats", []), "summary": data.get("summary", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/config")
def api_operations_mysql_table_statistics_config(instance_id: int, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), 100)
    return {"config": data.get("sections", {}).get("config", {}), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/plan-risk")
def api_operations_mysql_table_statistics_plan_risk(instance_id: int, limit: int = 500, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), limit)
    return {"items": data.get("sections", {}).get("plan_risks", []), "warnings": data.get("warnings", [])}


@operations_mysql_router.get("/{instance_id}/table-statistics/trend")
def api_operations_mysql_table_statistics_trend(instance_id: int, db: Session = Depends(get_db)):
    _get(db, instance_id)
    return {"items": [], "message": "统计信息趋势需要定时快照入库后展示。"}


@operations_mysql_router.post("/{instance_id}/table-statistics/analyze-plan")
def api_operations_mysql_table_statistics_analyze_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_mysql_stats_analyze_plan(payload)


@operations_mysql_router.post("/{instance_id}/table-statistics/histogram-plan")
def api_operations_mysql_table_statistics_histogram_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_mysql_stats_histogram_plan(payload, drop=False)


@operations_mysql_router.post("/{instance_id}/table-statistics/drop-histogram-plan")
def api_operations_mysql_table_statistics_drop_histogram_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_mysql_stats_histogram_plan(payload, drop=True)


@operations_mysql_router.post("/{instance_id}/table-statistics/ai-analyze")
def api_operations_mysql_table_statistics_ai_analyze(instance_id: int, db: Session = Depends(get_db)):
    data = get_table_statistics_center(_get(db, instance_id), 500)
    return {
        "status": "plan_only",
        "message": "已生成 MySQL 表统计信息规则诊断。未配置大模型时返回规则结论。",
        "summary": data.get("summary", {}),
        "risk_items": data.get("risk_items", []),
        "recommendations": data.get("recommendations", []),
        "warnings": data.get("warnings", []),
    }


@router.get("/{instance_id}/indexes/duplicate")
def api_duplicate_indexes(instance_id: int, db: Session = Depends(get_db)):
    return {"items": get_duplicate_indexes(_get(db, instance_id))}


@router.get("/{instance_id}/indexes")
def api_indexes_alias(instance_id: int, db: Session = Depends(get_db)):
    inst = _get(db, instance_id)
    return {
        "no_primary_key": get_no_primary_key_tables(inst),
        "duplicate_indexes": get_duplicate_indexes(inst),
        "fragmentation": get_fragmentation(inst),
    }


@router.post("/{instance_id}/plans/optimize-table")
def api_optimize(instance_id: int, schema: str, table: str, db: Session = Depends(get_db)):
    return generate_optimize_plan(_get(db, instance_id), schema, table)


@router.get("/{instance_id}/replication/status")
def api_replication(instance_id: int, db: Session = Depends(get_db)):
    return get_replication_status(_get(db, instance_id))


@router.get("/{instance_id}/replication")
def api_replication_alias(instance_id: int, db: Session = Depends(get_db)):
    return get_replication_status(_get(db, instance_id))


@router.get("/{instance_id}/mgr")
def api_mgr(instance_id: int, db: Session = Depends(get_db)):
    return get_mgr_status(_get(db, instance_id))


@router.get("/{instance_id}/relay-log")
def api_relay_log(instance_id: int, db: Session = Depends(get_db)):
    return get_relay_log_status(_get(db, instance_id))


@router.get("/{instance_id}/error-log")
def api_error_log(instance_id: int, db: Session = Depends(get_db)):
    return get_error_log_info(_get(db, instance_id))


@router.get("/{instance_id}/security")
def api_security(instance_id: int, db: Session = Depends(get_db)):
    return get_security_summary(_get(db, instance_id))


@router.post("/{instance_id}/plans/backup")
def api_backup(instance_id: int, backup_type: str = "full", database: str = "", table: str = "", db: Session = Depends(get_db)):
    return generate_backup_plan(_get(db, instance_id), backup_type, database, table)


@router.post("/{instance_id}/backup/plan")
def api_backup_plan_alias(instance_id: int, backup_type: str = "full", database: str = "", table: str = "", db: Session = Depends(get_db)):
    return generate_backup_plan(_get(db, instance_id), backup_type, database, table)


@router.post("/{instance_id}/mycnf/backup-plan")
def api_mycnf_backup_plan(instance_id: int, db: Session = Depends(get_db)):
    return generate_mycnf_backup_plan(_get(db, instance_id))


@router.post("/{instance_id}/metadata/backup-plan")
def api_metadata_backup_plan(instance_id: int, db: Session = Depends(get_db)):
    return generate_metadata_backup_plan(_get(db, instance_id))


@router.post("/{instance_id}/binlog/purge-plan")
def api_binlog_purge_alias(instance_id: int, before_file: str = "", before_date: str = "", retention_days: int = 7, db: Session = Depends(get_db)):
    return generate_purge_plan(_get(db, instance_id), before_file, before_date, retention_days)


@router.post("/{instance_id}/optimize-table-plan")
def api_optimize_alias(instance_id: int, schema: str, table: str, db: Session = Depends(get_db)):
    return generate_optimize_plan(_get(db, instance_id), schema, table)


@router.post("/{instance_id}/tables-indexes/analyze-table-plan")
def api_table_index_analyze_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_analyze_table_plan(payload)


@router.post("/{instance_id}/tables-indexes/optimize-table-plan")
def api_table_index_optimize_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_optimize_table_plan(payload)


@router.post("/{instance_id}/tables-indexes/add-index-plan")
def api_table_index_add_index_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_add_index_plan(payload)


@router.post("/{instance_id}/tables-indexes/drop-index-plan")
def api_table_index_drop_index_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_drop_index_plan(payload)


@router.post("/{instance_id}/tables-indexes/add-primary-key-plan")
def api_table_index_add_pk_plan(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_add_primary_key_plan(payload)


@router.post("/{instance_id}/tables-indexes/archive-suggestion")
def api_table_index_archive_suggestion(instance_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    _get(db, instance_id)
    return build_archive_suggestion(payload)


@router.post("/{instance_id}/inspect-ai")
def api_inspect_ai(instance_id: int, db: Session = Depends(get_db)):
    return generate_inspect_ai_plan(_get(db, instance_id))


@operations_mysql_router.post("/{instance_id}/inspect-diagnosis")
def api_operations_mysql_inspect_diagnosis(instance_id: int, db: Session = Depends(get_db)):
    inst = _get(db, instance_id)
    if inst.db_type != "mysql":
        raise HTTPException(status_code=400, detail="Instance is not MySQL")
    return run_mysql_diagnosis(db, inst)


@operations_mysql_router.get("/{instance_id}/inspection/latest")
def api_operations_mysql_inspection_latest(instance_id: int, db: Session = Depends(get_db)):
    diagnosis = get_latest_mysql_diagnosis(db, instance_id)
    if not diagnosis:
        return {
            "empty": True,
            "task": None,
            "modules": [],
            "findings": [],
            "report": None,
            "message": "No MySQL inspection diagnosis found",
        }
    return diagnosis


@operations_mysql_router.get("/{instance_id}/inspection/{task_id}")
def api_operations_mysql_inspection_detail(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    diagnosis = get_mysql_diagnosis(db, instance_id, task_id)
    if not diagnosis:
        raise HTTPException(status_code=404, detail="MySQL inspection diagnosis not found")
    return diagnosis


@operations_mysql_router.get("/{instance_id}/inspection/{task_id}/modules")
def api_operations_mysql_inspection_modules(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    return {"items": get_mysql_diagnosis_modules(db, instance_id, task_id)}


@operations_mysql_router.get("/{instance_id}/inspection/{task_id}/findings")
def api_operations_mysql_inspection_findings(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    return {"items": get_mysql_diagnosis_findings(db, instance_id, task_id)}


@operations_mysql_router.get("/{instance_id}/inspection/{task_id}/report")
def api_operations_mysql_inspection_report(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    diagnosis = get_mysql_diagnosis(db, instance_id, task_id)
    if not diagnosis:
        raise HTTPException(status_code=404, detail="MySQL inspection diagnosis not found")
    return diagnosis["report"]


@operations_mysql_router.post("/{instance_id}/inspection/{task_id}/ai-analyze")
def api_operations_mysql_inspection_ai_analyze(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    payload = build_ai_analysis_payload(db, instance_id, task_id)
    if not payload:
        raise HTTPException(status_code=404, detail="MySQL inspection diagnosis not found")
    return payload


@operations_mysql_router.post("/{instance_id}/inspection/{task_id}/ticket-plan")
def api_operations_mysql_inspection_ticket_plan(instance_id: int, task_id: str, db: Session = Depends(get_db)):
    payload = build_ticket_plan_payload(db, instance_id, task_id)
    if not payload:
        raise HTTPException(status_code=404, detail="MySQL inspection diagnosis not found")
    return payload
