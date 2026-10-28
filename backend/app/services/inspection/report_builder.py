from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from . import result_writer


SEVERITY_WEIGHT = {"critical": 25, "warning": 10, "info": 2, "normal": 0}
RISK_SCORE = {"critical": 95, "warning": 70, "info": 35, "normal": 0}


def build_report_payload(
    db: Session,
    task_id: str,
    instance_id: int | None = None,
) -> dict | None:
    rows = result_writer.get_task_rows(db, task_id, instance_id)
    if not rows:
        return None

    task = _task_dict(task_id, rows)
    modules = _module_dicts(rows)
    findings = [_finding_dict(row) for row in rows]
    abnormal = [item for item in findings if item["severity"] in {"critical", "warning"}]
    report = {
        "instance": result_writer.report_target(rows),
        "inspection_time": task.get("finished_at") or task.get("create_time"),
        "overall_conclusion": _conclusion(task, len(rows)),
        "key_risks": abnormal[:5],
        "recommended_priorities": abnormal[:5],
        "root_cause_analysis": [
            _rule_cause(item) for item in abnormal[:8] if item.get("finding")
        ],
        "business_impact": [
            _rule_impact(item)
            for item in abnormal[:5]
        ],
        "analysis_source": "rule_engine",
        "raw_appendix": {"rows": [result_writer.row_to_dict(row) for row in rows]},
    }
    return {"task": task, "modules": modules, "findings": findings, "report": report}


def _task_dict(task_id: str, rows: list) -> dict:
    counts = _counts(rows)
    execution = result_writer.execution_summary(rows)
    score = _score(counts) if execution["task_status"] == "success" else None
    risk_level = _risk_level(counts, score)
    create_time = max((row.create_time for row in rows if row.create_time), default=None)
    return {
        "task_id": task_id,
        "instance_id": rows[0].instance_id,
        "db_type": rows[0].db_type,
        "overall_score": score,
        "risk_level": risk_level,
        "summary": _conclusion({"risk_level": risk_level, **execution}, len(rows)),
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "info_count": counts["info"],
        "normal_count": counts["normal"],
        "total_count": len(rows),
        "started_at": _dt(create_time),
        "finished_at": _dt(create_time),
        "create_time": _dt(create_time),
        **execution,
        **result_writer.account_capability_summary(rows),
        **result_writer.inspection_template_summary(rows),
    }


def _module_dicts(rows: list) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.check_category or "Other"].append(row)

    modules = []
    for category, items in grouped.items():
        counts = _counts(items)
        execution = result_writer.execution_summary(items)
        runtime_only = all(result_writer._is_runtime_summary(row) for row in items)
        if runtime_only and all(result_writer.finding_execution_status(row) == "success" for row in items):
            execution = {"task_status": "success", "failure_reason": None}
        score = _score(counts) if execution["task_status"] == "success" else None
        if runtime_only:
            score = None
        abnormal = [
            row for row in items
            if _severity(row.check_status) != "normal"
            or result_writer.finding_execution_status(row) != "success"
        ]
        if execution["task_status"] != "success":
            summary = f"{category}: {execution['failure_reason']}"
        elif runtime_only:
            summary = f"{category}: 运行摘要，不参与健康评分"
        elif abnormal:
            summary = f"{category}: 发现 {len(abnormal)} 个异常项"
        else:
            summary = f"{category}: 未发现明显风险"
        modules.append(
            {
                "module_key": category,
                "module_name": category,
                "score": score,
                "risk_level": _risk_level(counts, score),
                **execution,
                "summary": summary,
                "evidence": {"counts": counts, "total": len(items)},
                "top_risks": [_finding_dict(row) for row in abnormal[:5]],
                "suggestions": [row.suggestion for row in abnormal if row.suggestion][:5],
                "raw_data": {"rows": [result_writer.row_to_dict(row) for row in items]},
            }
        )
    return modules


def _finding_dict(row) -> dict:
    severity = _severity(row.check_status)
    evidence = _finding_evidence(row)
    execution_status = evidence["execution_status"]
    if execution_status in {"failed", "partial", "skipped"} and severity == "normal":
        severity = "warning"
    operation_plan = _operation_plan(row, severity)
    return {
        "id": row.id,
        "task_id": row.task_id,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "module_key": row.check_category,
        "check_item": row.check_item,
        "severity": severity,
        "execution_status": execution_status,
        "risk_score": RISK_SCORE.get(severity, 0),
        "finding": row.finding,
        "evidence": evidence,
        "root_causes": [],
        "business_impact": [],
        "suggestions": [row.suggestion] if row.suggestion else [],
        "operation_plan": operation_plan,
        "rollback_plan": operation_plan["rollback_plan"],
        "related_objects": [],
        "related_sql": [],
        "raw_data": result_writer.row_to_dict(row),
    }


def _rule_cause(item: dict) -> str:
    name = str(item.get("check_item") or "巡检项")
    finding = str(item.get("finding") or "未记录异常明细")
    lowered = f"{name} {finding}".lower()
    if "内存配置" in name or "tmp_table_size" in lowered:
        return f"内存参数口径不一致：{finding}"
    if "buffer pool" in lowered or "缓冲池" in lowered:
        return f"InnoDB 工作集未完全命中缓冲池：{finding}"
    if "安全信息" in name or "任意主机" in finding:
        return f"账号来源范围过宽：{finding}"
    if "表与索引" in name or "无主键" in finding:
        return f"对象设计缺少稳定主键：{finding}"
    if "ssl" in lowered or "tls" in lowered or "secure_transport" in lowered:
        return f"服务端未强制加密传输：{finding}"
    if "备份" in name:
        return f"备份证据未闭环：{finding}"
    return f"{name}：{finding}"


def _rule_impact(item: dict) -> str:
    name = str(item.get("check_item") or "巡检项")
    finding = str(item.get("finding") or "")
    lowered = f"{name} {finding}".lower()
    if "内存配置" in name or "tmp_table_size" in lowered:
        return "并发排序或临时表负载升高时，较小的有效上限可能增加磁盘临时表；会话级缓冲区过大还会放大内存峰值。"
    if "buffer pool" in lowered or "缓冲池" in lowered:
        return "Buffer Pool 命中率偏低会增加物理读和存储延迟，但需结合持续采样判断是否为启动预热或短时波动。"
    if "安全信息" in name or "任意主机" in finding:
        return "未限制来源地址的高权限或复制账号会扩大凭据泄露后的可攻击面。"
    if "表与索引" in name or "无主键" in finding:
        return "无主键表可能增加行定位、在线变更和行格式复制成本；小表影响有限，仍应在扩容前治理。"
    if "ssl" in lowered or "tls" in lowered or "secure_transport" in lowered:
        return "客户端未主动启用 TLS 时，账号凭据与业务数据可能通过未加密链路传输。"
    if "备份" in name:
        return "无法验证最近成功备份与恢复点，会使 RPO、RTO 和恢复可用性处于未知状态。"
    return f"{name} 已触发规则风险，需结合趋势、业务窗口和关联监控确认实际影响。"


def _finding_evidence(row) -> dict:
    raw_data = dict(row.raw_data) if isinstance(row.raw_data, dict) else {}
    if raw_data.get("sql") or raw_data.get("rows") is not None:
        return {**raw_data, "execution_status": result_writer.finding_execution_status(row)}
    evidence = raw_data or {
        "metric": row.metric_name,
        "value": row.metric_value,
        "threshold": row.threshold_value,
    }
    execution_status = result_writer.finding_execution_status(row)
    if str(row.db_type or "").lower() == "oracle" and execution_status == "success":
        # Saved metrics cannot prove which SQL, cursor columns or rows were executed.
        return {**evidence, "execution_status": "unknown", "evidence_source": "legacy_saved_metric"}
    return {**evidence, "execution_status": execution_status}


def _operation_plan(row, severity: str) -> dict:
    db_type = str(row.db_type or "").lower()
    category = str(row.check_category or "")
    key = _module_key(db_type, category)
    template = _PLAN_TEMPLATES.get(db_type, {}).get(key) or _GENERIC_PLAN_TEMPLATE
    steps = [
        "确认当前巡检结果、采集时间、实例和业务影响范围。",
        *template["steps"],
        "将本方案提交变更审批，审批通过后在维护窗口人工执行。",
        "执行后重新巡检并保留执行输出、前后指标截图和验证结果。",
    ]
    sql = template["sql"]
    rollback_plan = [
        *template["rollback_plan"],
        "本巡检报告默认 status=plan_only，系统不会自动执行任何变更。",
    ]
    if severity == "normal":
        steps = [
            "当前检查项未发现明显异常，保留结果作为健康基线。",
            *template.get("normal_steps", []),
            "按既定巡检周期持续观察趋势变化。",
        ]
        sql = template.get("readonly_sql", sql[:1])
        rollback_plan = ["只读检查，无生产变更，无需回滚。"]

    return {
        "status": "plan_only",
        "read_only": True,
        "db_type": db_type,
        "module": category or key,
        "severity": severity,
        "approval_required": severity in {"critical", "warning"},
        "steps": steps,
        "sql": sql,
        "rollback_plan": rollback_plan,
        "warnings": [
            "仅生成方案，不直接执行。",
            "涉及 KILL、扩容、重建索引、更新统计信息、故障切换、归档清理等动作必须人工确认。",
        ],
    }


def _module_key(db_type: str, category: str) -> str:
    mapping = {
        "sqlserver": {
            "01. 版本信息": "overview",
            "02. 连接信息": "overview",
            "03. 活动会话": "session",
            "04. 数据库大小": "space",
            "05. 数据库状态": "overview",
            "06. 关键配置": "overview",
            "07. 等待统计 TOP 10": "session",
            "08. CPU 信息": "overview",
            "09. 阻塞进程": "session",
            "10. 锁信息": "session",
            "11. 死锁信息": "session",
            "12. 备份信息": "logs",
            "13. 日志空间": "logs",
            "14. SQL Server 内存": "overview",
            "15. 链接服务器": "governance",
            "16. 文件组": "space",
            "17. 主机资源": "space",
            "运行概览": "overview",
            "空间管理": "space",
            "日志管理": "logs",
            "会话事务": "session",
            "对象统计": "objects",
            "高可用": "ha",
            "数据治理": "governance",
        },
        "postgresql": {
            "运行概览": "overview",
            "空间管理": "space",
            "WAL 日志": "logs",
            "会话事务": "session",
            "对象统计": "objects",
            "复制高可用": "ha",
            "数据治理": "governance",
        },
        "mysql": {
            "实例状态": "overview",
            "MySQL 巡检": "overview",
            "连接": "session",
            "复制": "ha",
            "慢 SQL": "objects",
        },
        "oracle": {
            "实例状态": "overview",
            "Oracle 巡检": "overview",
            "表空间": "space",
            "归档": "logs",
            "会话": "session",
            "对象": "objects",
            "Data Guard": "ha",
        },
    }
    return mapping.get(db_type, {}).get(category, "generic")


_GENERIC_PLAN_TEMPLATE = {
    "steps": [
        "复核采集明细和原始数据，确认是否为权限不足、网络波动或真实风险。",
        "补充实例、主机、监控告警和业务系统上下文，形成证据链。",
        "根据建议生成只读验证 SQL 或 plan_only 变更脚本。",
    ],
    "normal_steps": ["抽样检查关键指标是否和监控平台一致。"],
    "readonly_sql": ["-- plan_only: review raw inspection evidence and keep it as baseline."],
    "sql": ["-- plan_only: no executable change generated for this generic finding."],
    "rollback_plan": ["如已在外部执行变更，按变更单中保留的原始配置和备份回退。"],
}


_PLAN_TEMPLATES = {
    "sqlserver": {
        "overview": {
            "steps": [
                "核对 SQL Server 版本、补丁级别、实例启动时间和服务账号。",
                "确认连接数、活动会话、阻塞和关键数据库状态。",
                "如存在版本或补丁风险，先在测试环境验证补丁兼容性。",
            ],
            "normal_steps": ["保留版本、启动时间、会话数量作为 SQL Server 运行基线。"],
            "readonly_sql": [
                "SELECT @@SERVERNAME AS server_name, @@VERSION AS version;",
                "SELECT sqlserver_start_time FROM sys.dm_os_sys_info;",
                "SELECT name, state_desc, recovery_model_desc FROM sys.databases ORDER BY name;",
            ],
            "sql": [
                "SELECT @@SERVERNAME AS server_name, @@VERSION AS version;",
                "SELECT sqlserver_start_time FROM sys.dm_os_sys_info;",
                "SELECT name, state_desc, recovery_model_desc FROM sys.databases ORDER BY name;",
            ],
            "rollback_plan": ["运行概览只读检查无需回滚；补丁或配置变更需按厂商回退手册执行。"],
        },
        "space": {
            "steps": [
                "确认数据库文件、日志文件、TempDB 和磁盘挂载点剩余容量。",
                "识别异常增长文件，检查 FILEGROWTH 是否为固定 MB 且符合业务峰值。",
                "生成扩容或增长策略 plan_only 脚本，先评估备份、磁盘和维护窗口。",
            ],
            "normal_steps": ["持续跟踪数据库文件、日志文件和 TempDB 增长趋势。"],
            "readonly_sql": [
                "SELECT DB_NAME(database_id) AS database_name, name, type_desc, size * 8 / 1024 AS size_mb, growth, is_percent_growth FROM sys.master_files ORDER BY size DESC;",
                "SELECT * FROM sys.dm_db_file_space_usage;",
            ],
            "sql": [
                "SELECT DB_NAME(database_id) AS database_name, name, type_desc, size * 8 / 1024 AS size_mb, growth, is_percent_growth FROM sys.master_files ORDER BY size DESC;",
                "-- plan_only: ALTER DATABASE [<database>] MODIFY FILE (NAME = N'<logical_name>', FILEGROWTH = 1024MB);",
                "-- plan_only: ALTER DATABASE [<database>] MODIFY FILE (NAME = N'<logical_name>', SIZE = <target_size>MB);",
            ],
            "rollback_plan": [
                "扩容前记录 sys.master_files 中原始 size、growth、is_percent_growth。",
                "如扩容策略异常，回退 FILEGROWTH 到原值；文件缩容必须单独评估，不自动执行。",
            ],
        },
        "logs": {
            "steps": [
                "检查事务日志使用率、VLF 数量、恢复模式和最近日志备份时间。",
                "FULL 恢复模式下确认日志备份链是否连续，避免日志文件持续膨胀。",
                "对高 VLF 或日志爆涨场景生成日志备份、收缩和重新增长的 plan_only 方案。",
            ],
            "normal_steps": ["保留日志使用率、VLF 数量和备份链状态作为日志基线。"],
            "readonly_sql": [
                "DBCC SQLPERF(LOGSPACE);",
                "SELECT d.name, d.recovery_model_desc, MAX(b.backup_finish_date) AS last_log_backup FROM sys.databases d LEFT JOIN msdb.dbo.backupset b ON d.name = b.database_name AND b.type = 'L' GROUP BY d.name, d.recovery_model_desc;",
            ],
            "sql": [
                "DBCC SQLPERF(LOGSPACE);",
                "SELECT DB_NAME(database_id) AS database_name, COUNT(*) AS vlf_count FROM sys.dm_db_log_info(NULL) GROUP BY database_id;",
                "-- plan_only: BACKUP LOG [<database>] TO DISK = N'D:\\backup\\<database>_log.trn' WITH INIT, COMPRESSION, CHECKSUM;",
            ],
            "rollback_plan": [
                "日志备份不可回滚，但可通过备份链验证恢复点。",
                "如调整日志文件大小，保留原文件大小和增长策略，必要时按审批单恢复。",
            ],
        },
        "session": {
            "steps": [
                "确认活动请求、阻塞链、锁等待对象和长事务开始时间。",
                "联系业务确认会话来源、SQL 文本和是否允许终止。",
                "仅生成 KILL 会话 plan_only 脚本，禁止系统自动终止会话。",
            ],
            "normal_steps": ["保留活动会话和阻塞链为空的巡检证据。"],
            "readonly_sql": [
                "SELECT session_id, status, blocking_session_id, wait_type, wait_time, command FROM sys.dm_exec_requests ORDER BY wait_time DESC;",
                "SELECT request_session_id, resource_type, request_mode, request_status FROM sys.dm_tran_locks;",
            ],
            "sql": [
                "SELECT session_id, status, blocking_session_id, wait_type, wait_time, command FROM sys.dm_exec_requests ORDER BY wait_time DESC;",
                "SELECT request_session_id, resource_type, request_mode, request_status FROM sys.dm_tran_locks;",
                "-- plan_only: KILL <session_id>; -- 必须人工审批后执行",
            ],
            "rollback_plan": [
                "KILL 会话不可回滚，执行前必须确认事务影响和业务补偿方案。",
                "如误杀导致业务失败，按应用重试、补偿任务或数据库恢复流程处理。",
            ],
        },
        "objects": {
            "steps": [
                "确认大表、索引碎片、缺失索引、未使用索引和统计信息更新时间。",
                "对候选对象先查看执行计划和业务访问频率，避免盲目重建索引。",
                "生成 UPDATE STATISTICS / REBUILD INDEX 的 plan_only 脚本，并安排低峰执行。",
            ],
            "normal_steps": ["保留对象容量排行和统计信息更新时间，作为后续增长分析基线。"],
            "readonly_sql": [
                "SELECT TOP 100 SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count FROM sys.tables t JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1) GROUP BY t.schema_id, t.name ORDER BY row_count DESC;",
                "SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name, OBJECT_NAME(object_id) AS table_name, name AS stats_name, STATS_DATE(object_id, stats_id) AS stats_update_time FROM sys.stats ORDER BY STATS_DATE(object_id, stats_id);",
            ],
            "sql": [
                "SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name, OBJECT_NAME(object_id) AS table_name, name AS stats_name, STATS_DATE(object_id, stats_id) AS stats_update_time FROM sys.stats ORDER BY STATS_DATE(object_id, stats_id);",
                "-- plan_only: UPDATE STATISTICS [<schema>].[<table>] WITH SAMPLE 30 PERCENT;",
                "-- plan_only: ALTER INDEX [<index_name>] ON [<schema>].[<table>] REBUILD WITH (ONLINE = ON);",
            ],
            "rollback_plan": [
                "更新统计信息前可导出关键统计信息或记录 STATS_DATE，必要时用备份环境对比执行计划。",
                "索引重建前记录索引定义、碎片率和执行计划；异常时按原索引定义重建或回退发布。",
            ],
        },
        "ha": {
            "steps": [
                "检查 Always On、数据库状态、同步状态、同步健康和恢复模式。",
                "确认主副本延迟、日志发送队列、重做队列和最近备份链。",
                "故障切换只生成检查清单，不自动执行 failover。",
            ],
            "normal_steps": ["保留数据库同步状态、恢复模式和高可用健康状态。"],
            "readonly_sql": [
                "SELECT name, state_desc, recovery_model_desc FROM sys.databases ORDER BY name;",
                "SELECT DB_NAME(database_id) AS database_name, synchronization_state_desc, synchronization_health_desc FROM sys.dm_hadr_database_replica_states;",
            ],
            "sql": [
                "SELECT DB_NAME(database_id) AS database_name, synchronization_state_desc, synchronization_health_desc FROM sys.dm_hadr_database_replica_states;",
                "-- plan_only: ALTER AVAILABILITY GROUP [<ag_name>] FAILOVER; -- 仅人工审批后执行",
            ],
            "rollback_plan": [
                "故障切换前确认回切条件、连接串策略和应用重连机制。",
                "如切换后异常，按 Always On 回切流程和备份恢复预案处理。",
            ],
        },
        "governance": {
            "steps": [
                "筛选超过阈值的大表、历史表和高增长对象。",
                "确认保留周期、归档条件、唯一键和回查需求。",
                "生成分区、归档、BCP 导出的 plan_only 脚本，先在历史库或测试库验证。",
            ],
            "normal_steps": ["当前无治理候选表时，保留阈值和对象行数作为治理基线。"],
            "readonly_sql": [
                "SELECT TOP 100 SCHEMA_NAME(t.schema_id) AS schema_name, t.name AS table_name, SUM(p.rows) AS row_count FROM sys.tables t JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1) GROUP BY t.schema_id, t.name HAVING SUM(p.rows) > 5000000 ORDER BY row_count DESC;",
            ],
            "sql": [
                "-- plan_only: bcp \"SELECT * FROM <database>.<schema>.<table> WHERE <archive_condition>\" queryout D:\\backup\\archive.csv -c -t, -T",
                "-- plan_only: INSERT INTO <history_table> SELECT * FROM <source_table> WHERE <archive_condition>;",
                "-- plan_only: CREATE PARTITION FUNCTION pf_<table>(datetime2) AS RANGE RIGHT FOR VALUES (...);",
            ],
            "rollback_plan": [
                "归档前保留源表行数、校验 SQL、备份和抽样数据。",
                "删除生产历史数据前必须完成历史库校验；异常时从历史库或备份回灌。",
            ],
        },
    },
    "postgresql": {
        "space": {
            "steps": ["检查数据库、表、索引大小和膨胀线索。", "生成 VACUUM/REINDEX/扩容 plan_only 方案。"],
            "normal_steps": ["保留 pg_database_size 和 pg_stat_user_tables 作为容量基线。"],
            "readonly_sql": ["SELECT datname, pg_database_size(datname) AS size_bytes FROM pg_database ORDER BY size_bytes DESC;"],
            "sql": ["-- plan_only: VACUUM (VERBOSE, ANALYZE) <schema>.<table>;", "-- plan_only: REINDEX INDEX CONCURRENTLY <index_name>;"],
            "rollback_plan": ["VACUUM/ANALYZE 无直接回滚；REINDEX 前保留索引定义和执行窗口。"],
        },
        "objects": {
            "steps": ["检查 dead tuples、全表扫描比例和统计信息新鲜度。", "生成 ANALYZE/VACUUM plan_only 方案。"],
            "normal_steps": ["保留对象统计信息作为优化基线。"],
            "readonly_sql": ["SELECT schemaname, relname, n_live_tup, n_dead_tup, last_analyze, last_autoanalyze FROM pg_stat_user_tables ORDER BY n_dead_tup DESC;"],
            "sql": ["-- plan_only: ANALYZE VERBOSE <schema>.<table>;", "-- plan_only: VACUUM (VERBOSE, ANALYZE) <schema>.<table>;"],
            "rollback_plan": ["统计信息更新不可直接回滚，执行前保留执行计划和关键 SQL 性能基线。"],
        },
    },
    "mysql": {
        "overview": {
            "steps": ["检查实例状态、连接数、复制状态和关键参数。", "对异常项生成只读验证 SQL。"],
            "normal_steps": ["保留 MySQL 巡检结果作为实例健康基线。"],
            "readonly_sql": ["SHOW GLOBAL STATUS;", "SHOW GLOBAL VARIABLES;"],
            "sql": ["SHOW GLOBAL STATUS;", "SHOW GLOBAL VARIABLES;"],
            "rollback_plan": ["只读检查无需回滚；参数变更需保留原值并使用 SET GLOBAL 或配置文件回退。"],
        },
        "session": {
            "steps": ["检查连接数、活跃事务、锁等待和阻塞链。", "确认业务来源后生成 KILL plan_only。"],
            "normal_steps": ["保留连接和锁等待基线。"],
            "readonly_sql": ["SHOW PROCESSLIST;", "SELECT * FROM information_schema.innodb_trx;"],
            "sql": ["SHOW PROCESSLIST;", "-- plan_only: KILL <thread_id>;"],
            "rollback_plan": ["KILL 不可回滚，必须先确认事务影响和业务补偿。"],
        },
        "objects": {
            "steps": ["检查慢 SQL、表统计、索引使用和碎片。", "生成 ANALYZE/OPTIMIZE/索引调整 plan_only。"],
            "normal_steps": ["保留慢 SQL 和对象统计基线。"],
            "readonly_sql": ["SELECT * FROM information_schema.tables WHERE table_schema NOT IN ('mysql','sys','performance_schema','information_schema');"],
            "sql": ["-- plan_only: ANALYZE TABLE `<schema>`.`<table>`;", "-- plan_only: OPTIMIZE TABLE `<schema>`.`<table>`;"],
            "rollback_plan": ["索引变更前保留 SHOW CREATE TABLE；异常时按原 DDL 回退。"],
        },
    },
    "oracle": {
        "overview": {
            "steps": ["检查实例状态、归档模式、会话、等待和关键参数。", "对异常项生成只读验证 SQL。"],
            "normal_steps": ["保留 Oracle 巡检结果作为健康基线。"],
            "readonly_sql": ["SELECT instance_name, status, startup_time FROM v$instance;", "SELECT name, open_mode, log_mode FROM v$database;"],
            "sql": ["SELECT instance_name, status, startup_time FROM v$instance;", "SELECT name, open_mode, log_mode FROM v$database;"],
            "rollback_plan": ["只读检查无需回滚；参数变更需保留 spfile/pfile 原值。"],
        },
        "space": {
            "steps": ["检查表空间、TEMP、UNDO、数据文件和增长趋势。", "生成扩容或清理 plan_only 脚本。"],
            "normal_steps": ["保留表空间、TEMP、UNDO 使用率基线。"],
            "readonly_sql": ["SELECT tablespace_name, file_name, bytes/1024/1024 AS mb, autoextensible FROM dba_data_files;"],
            "sql": ["-- plan_only: ALTER DATABASE DATAFILE '<file_name>' RESIZE <target_size>M;", "-- plan_only: ALTER TABLESPACE <tablespace> ADD DATAFILE '<file>' SIZE <size>M AUTOEXTEND ON;"],
            "rollback_plan": ["扩容前记录原数据文件大小；缩容需单独评估高水位，不自动执行。"],
        },
        "objects": {
            "steps": ["检查无效对象、统计信息、索引状态和热点对象。", "生成 gather stats/rebuild index plan_only。"],
            "normal_steps": ["保留对象状态和统计信息基线。"],
            "readonly_sql": ["SELECT owner, table_name, last_analyzed, stale_stats FROM dba_tab_statistics WHERE owner NOT IN ('SYS','SYSTEM');"],
            "sql": ["-- plan_only: EXEC DBMS_STATS.GATHER_TABLE_STATS('<owner>', '<table_name>');", "-- plan_only: ALTER INDEX <owner>.<index_name> REBUILD ONLINE;"],
            "rollback_plan": ["统计信息更新前导出 stats；异常时使用 DBMS_STATS.RESTORE_TABLE_STATS 回退。"],
        },
    },
}


def _counts(rows: list) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0, "normal": 0}
    for row in rows:
        sev = _severity(row.check_status)
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _score(counts: dict[str, int]) -> int:
    return max(0, 100 - sum(counts.get(k, 0) * v for k, v in SEVERITY_WEIGHT.items()))


def _risk_level(counts: dict[str, int], score: int | None) -> str:
    if score is None:
        return "warning"
    if counts.get("critical"):
        return "critical"
    if counts.get("warning"):
        return "warning"
    if counts.get("info"):
        return "info"
    return "normal" if score >= 90 else "info"


def _severity(status: str | None) -> str:
    normalized = str(status or "").lower()
    if normalized in {"error", "failed", "failure"}:
        return "warning"
    if normalized in {"critical", "warning", "info"}:
        return normalized
    return "normal"


def _conclusion(task: dict, total: int) -> str:
    if task.get("task_status") == "failed":
        return "巡检采集失败，未获得完整健康评估。"
    if task.get("task_status") == "partial":
        return f"巡检部分采集失败，仅获得 {total} 项结果，健康评估不完整。"
    risk = task.get("risk_level")
    if risk == "critical":
        return f"巡检完成，发现严重风险。共检查 {total} 项。"
    if risk == "warning":
        return f"巡检完成，发现警告风险。共检查 {total} 项。"
    return f"巡检完成，未发现明显风险。共检查 {total} 项。"


def _dt(value) -> str | None:
    return value.isoformat(sep=" ") if value else None
