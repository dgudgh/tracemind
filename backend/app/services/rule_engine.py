from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.models.diagnosis_evidence import DiagnosisEvidence
from app.models.diagnosis_result import DiagnosisResult
from app.models.diagnosis_rule import DiagnosisRule
from app.services.inspection import dispatch as inspection_dispatch


DEFAULT_RULES = [
    {
        "rule_code": "ORACLE_TABLESPACE_001",
        "rule_name": "表空间使用率过高",
        "category": "space",
        "severity": "critical",
        "condition_json": {"keywords": ["表空间", "tablespace"], "status": ["warning", "critical"]},
        "action_json": ["generate_tablespace_extend_sql", "generate_archive_plan", "create_ticket", "push_zabbix"],
        "description": "识别表空间使用率、剩余空间、自动扩展和文件系统联动风险。",
    },
    {
        "rule_code": "ORACLE_FRA_ARCHIVE_001",
        "rule_name": "FRA / 归档空间风险",
        "category": "space",
        "severity": "critical",
        "condition_json": {"keywords": ["FRA", "归档", "archive"], "status": ["warning", "critical"]},
        "action_json": ["generate_rman_cleanup_plan", "generate_archive_backup_plan", "create_ticket", "push_zabbix"],
        "description": "识别 FRA、归档目录、归档增长和 ORA-00257 风险。",
    },
    {
        "rule_code": "SQL_PERFORMANCE_001",
        "rule_name": "慢 SQL / 高频 SQL 风险",
        "category": "performance",
        "severity": "warning",
        "condition_json": {"keywords": ["SQL", "慢", "执行计划", "逻辑读"], "status": ["warning", "critical"]},
        "action_json": ["open_slow_sql_analysis", "generate_index_advice", "gather_stats_plan"],
        "description": "识别慢 SQL、高逻辑读、全表扫描、执行计划变化和索引治理线索。",
    },
    {
        "rule_code": "INDEX_STATS_001",
        "rule_name": "索引 / 统计信息治理风险",
        "category": "sql_index",
        "severity": "warning",
        "condition_json": {"keywords": ["索引", "统计信息", "index", "stats", "UNUSABLE"], "status": ["warning", "critical"]},
        "action_json": ["generate_rebuild_index_sql", "gather_stats_plan", "create_ticket"],
        "description": "识别失效索引、统计信息过期和索引使用异常。",
    },
    {
        "rule_code": "TRANSACTION_LOCK_001",
        "rule_name": "事务 / 锁等待风险",
        "category": "transaction",
        "severity": "critical",
        "condition_json": {"keywords": ["锁", "阻塞", "事务", "lock", "blocking"], "status": ["warning", "critical"]},
        "action_json": ["show_blocking_session", "create_ticket"],
        "description": "识别阻塞会话、长事务、行锁等待和业务卡顿风险。",
    },
    {
        "rule_code": "UNDO_001",
        "rule_name": "UNDO 与闪回风险",
        "category": "transaction",
        "severity": "warning",
        "condition_json": {"keywords": ["UNDO", "ORA-01555", "闪回"], "status": ["warning", "critical"]},
        "action_json": ["open_undo_management", "generate_undo_retention_plan"],
        "description": "识别 ACTIVE/UNEXPIRED/EXPIRED、ORA-01555 和闪回窗口风险。",
    },
    {
        "rule_code": "HA_DATAGUARD_001",
        "rule_name": "Data Guard 高可用风险",
        "category": "ha",
        "severity": "critical",
        "condition_json": {"keywords": ["Data Guard", "DG", "MRP", "lag", "备库"], "status": ["warning", "critical"]},
        "action_json": ["show_dataguard_status", "create_ticket", "push_zabbix"],
        "description": "识别传输延迟、应用延迟、gap 和 MRP 异常。",
    },
    {
        "rule_code": "BACKUP_ARCHIVE_001",
        "rule_name": "备份 / 历史归档风险",
        "category": "archive",
        "severity": "warning",
        "condition_json": {"keywords": ["备份", "归档任务", "历史库", "EXPDP", "CSV"], "status": ["warning", "critical"]},
        "action_json": ["generate_archive_plan", "generate_verify_sql", "create_ticket"],
        "description": "识别备份缺失、归档失败、校验缺失和生产清理风险。",
    },
    {
        "rule_code": "EXTERNAL_MONITOR_001",
        "rule_name": "外部监控联动风险",
        "category": "external_monitor",
        "severity": "warning",
        "condition_json": {"keywords": ["Zabbix", "Prometheus", "Alertmanager", "主机", "文件系统"], "status": ["warning", "critical"]},
        "action_json": ["push_zabbix", "merge_alert", "create_ticket"],
        "description": "关联主机、文件系统、Prometheus 和 Zabbix 告警上下文。",
    },
]


@dataclass
class RuleHit:
    rule_code: str
    rule_name: str
    category: str
    severity: str
    confidence: int
    object_type: str
    object_name: str
    evidence: list[str]
    root_cause: str
    impact: str
    suggestion: str
    actions: list[str]


def ensure_default_diagnosis_rules(db: Session) -> None:
    existing = {row.rule_code: row for row in db.query(DiagnosisRule).all()}
    for item in DEFAULT_RULES:
        row = existing.get(item["rule_code"])
        if row:
            row.rule_name = item["rule_name"]
            row.category = item["category"]
            row.severity = item["severity"]
            row.condition_json = item["condition_json"]
            row.action_json = item["action_json"]
            row.description = item["description"]
        else:
            db.add(DiagnosisRule(**item, enabled=1))
    db.commit()


def run_rule_diagnosis(db: Session, instance_id: int, persist: bool = True) -> dict:
    ensure_default_diagnosis_rules(db)
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        raise ValueError("数据库实例不存在")

    task_id, rows = inspection_dispatch.run_collecting_rows(
        db, instance, trigger_type="rule_engine",
    )
    hits = build_rule_hits(instance, rows)
    run_id = f"diag-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"

    saved_results = []
    if persist:
        for hit in hits:
            result = DiagnosisResult(
                run_id=run_id,
                instance_id=instance.id,
                db_type=instance.db_type,
                instance_name=instance.instance_name,
                rule_code=hit.rule_code,
                rule_name=hit.rule_name,
                category=hit.category,
                severity=hit.severity,
                confidence=hit.confidence,
                object_type=hit.object_type,
                object_name=hit.object_name,
                evidence_json=hit.evidence,
                root_cause=hit.root_cause,
                impact=hit.impact,
                suggestion=hit.suggestion,
                action_json=hit.actions,
                status="open",
            )
            db.add(result)
            db.flush()
            for evidence in hit.evidence:
                db.add(DiagnosisEvidence(
                    diagnosis_id=result.id,
                    evidence_type="inspection",
                    evidence_name=hit.rule_name,
                    evidence_value=evidence,
                    source_table="db_check_result",
                    source_query=f"task_id={task_id}",
                ))
            saved_results.append(result)
        db.commit()
        for result in saved_results:
            db.refresh(result)

    return {
        "run_id": run_id,
        "task_id": task_id,
        "instance": {
            "id": instance.id,
            "instance_name": instance.instance_name,
            "db_type": instance.db_type,
            "host": instance.host,
            "port": instance.port,
        },
        "summary": {
            "total_checks": len(rows),
            "warning": len([r for r in rows if r.check_status == "warning"]),
            "critical": len([r for r in rows if r.check_status == "critical"]),
            "diagnosis_count": len(hits),
            "critical_diagnosis": len([h for h in hits if h.severity == "critical"]),
            "warning_diagnosis": len([h for h in hits if h.severity == "warning"]),
        },
        "results": [serialize_hit(hit, saved_results[index] if index < len(saved_results) else None, run_id, instance) for index, hit in enumerate(hits)],
    }


def build_rule_hits(instance: DBInstance, rows: list[DBCheckResult]) -> list[RuleHit]:
    abnormal = [row for row in rows if row.check_status in ("warning", "critical")]
    if not abnormal:
        return [
            RuleHit(
                rule_code="DB_HEALTH_NORMAL",
                rule_name="未发现明确异常",
                category="overview",
                severity="normal",
                confidence=90,
                object_type="instance",
                object_name=instance.instance_name,
                evidence=[f"本次巡检共 {len(rows)} 项，未发现 warning 或 critical 项。"],
                root_cause="当前结构化巡检数据未显示明确异常。",
                impact="暂无明确业务影响。",
                suggestion="保持当前巡检频率；建议在业务高峰期和批处理窗口后再次巡检。",
                actions=["export_report"],
            )
        ]

    grouped: dict[str, list[DBCheckResult]] = defaultdict(list)
    for row in abnormal:
        grouped[_match_rule(row)].append(row)

    hits = []
    for rule_code, items in grouped.items():
        rule = next((item for item in DEFAULT_RULES if item["rule_code"] == rule_code), None)
        if not rule:
            rule = _generic_rule()
        severity = "critical" if any(row.check_status == "critical" for row in items) else "warning"
        object_name = _guess_object_name(items)
        evidence = [_format_evidence(row) for row in items[:12]]
        confidence = 95 if severity == "critical" else 88
        hits.append(RuleHit(
            rule_code=rule["rule_code"],
            rule_name=rule["rule_name"],
            category=rule["category"],
            severity=severity,
            confidence=confidence,
            object_type=_guess_object_type(rule["category"]),
            object_name=object_name,
            evidence=evidence,
            root_cause=_build_root_cause(rule["category"], items),
            impact=_build_impact(rule["category"], severity),
            suggestion=_build_suggestion(rule["category"], items),
            actions=list(rule["action_json"]),
        ))
    return sorted(hits, key=lambda item: (0 if item.severity == "critical" else 1 if item.severity == "warning" else 2, item.category, item.rule_name))


def serialize_result(row: DiagnosisResult) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "instance_id": row.instance_id,
        "db_type": row.db_type,
        "instance_name": row.instance_name,
        "rule_code": row.rule_code,
        "rule_name": row.rule_name,
        "category": row.category,
        "severity": row.severity,
        "confidence": row.confidence,
        "object_type": row.object_type,
        "object_name": row.object_name,
        "evidence": row.evidence_json or [],
        "root_cause": row.root_cause,
        "impact": row.impact,
        "suggestion": row.suggestion,
        "actions": row.action_json or [],
        "status": row.status,
        "create_time": row.create_time.isoformat(sep=" ") if row.create_time else None,
    }


def serialize_hit(hit: RuleHit, row: DiagnosisResult | None, run_id: str, instance: DBInstance) -> dict:
    payload = {
        "id": row.id if row else None,
        "run_id": run_id,
        "instance_id": instance.id,
        "db_type": instance.db_type,
        "instance_name": instance.instance_name,
        "rule_code": hit.rule_code,
        "rule_name": hit.rule_name,
        "category": hit.category,
        "severity": hit.severity,
        "confidence": hit.confidence,
        "object_type": hit.object_type,
        "object_name": hit.object_name,
        "evidence": hit.evidence,
        "root_cause": hit.root_cause,
        "impact": hit.impact,
        "suggestion": hit.suggestion,
        "actions": hit.actions,
        "status": row.status if row else "open",
        "create_time": row.create_time.isoformat(sep=" ") if row and row.create_time else None,
    }
    return payload


def build_action_plan(row: DiagnosisResult) -> dict:
    actions = row.action_json or []
    sections = [
        {
            "title": "诊断摘要",
            "script": "\n".join([
                f"规则：{row.rule_name}",
                f"级别：{row.severity}",
                f"实例：{row.instance_name}",
                f"对象：{row.object_type or '-'} / {row.object_name or '-'}",
                f"根因：{row.root_cause or '-'}",
                f"建议：{row.suggestion or '-'}",
            ]),
        },
        {
            "title": "证据链",
            "script": "\n".join(f"- {item}" for item in (row.evidence_json or [])) or "- 暂无证据",
        },
    ]

    for action in actions:
        section = _build_action_section(row, action)
        if section:
            sections.append(section)

    return {
        "status": "plan_only",
        "message": "已生成规则诊断处置方案。所有脚本只供人工确认，不会自动执行。",
        "sections": sections,
    }


def _build_action_section(row: DiagnosisResult, action: str) -> dict | None:
    object_name = row.object_name or "<OBJECT_NAME>"
    owner = "<OWNER>"
    table_name = "<TABLE_NAME>"
    if "." in object_name:
        owner, table_name = object_name.split(".", 1)

    if action == "generate_tablespace_extend_sql":
        return {
            "title": "表空间扩容 SQL",
            "script": f"""-- 请先确认 ASM / 文件系统剩余空间，再在维护窗口执行
ALTER TABLESPACE {object_name}
  ADD DATAFILE '<DATAFILE_PATH>/{object_name.lower()}_nn.dbf'
  SIZE 30720M
  AUTOEXTEND ON NEXT 1024M MAXSIZE 30720M;""",
        }
    if action == "generate_rman_cleanup_plan":
        return {
            "title": "RMAN 归档清理方案",
            "script": """-- 请确认归档已备份，再执行清理
rman target /
CROSSCHECK ARCHIVELOG ALL;
DELETE NOPROMPT EXPIRED ARCHIVELOG ALL;
DELETE NOPROMPT ARCHIVELOG ALL COMPLETED BEFORE 'SYSDATE-1';""",
        }
    if action == "generate_archive_backup_plan":
        return {
            "title": "归档备份建议",
            "script": """-- 建议先备份归档，再清理 FRA / 归档目录
rman target /
BACKUP ARCHIVELOG ALL FORMAT '<BACKUP_PATH>/arch_%d_%T_%s_%p.bkp';
LIST BACKUP OF ARCHIVELOG ALL;""",
        }
    if action == "gather_stats_plan":
        return {
            "title": "统计信息收集 SQL",
            "script": f"""BEGIN
  DBMS_STATS.GATHER_TABLE_STATS(
    ownname          => '{owner}',
    tabname          => '{table_name}',
    estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE,
    method_opt       => 'FOR ALL COLUMNS SIZE AUTO',
    cascade          => TRUE,
    degree           => 4,
    no_invalidate    => DBMS_STATS.AUTO_INVALIDATE
  );
END;
/""",
        }
    if action == "generate_index_advice":
        return {
            "title": "索引建议 SQL 模板",
            "script": f"""-- 请结合 SQL 谓词字段、选择性、DML 压力和测试库验证后再执行
CREATE INDEX {owner}.IDX_{table_name}_CANDIDATE
ON {owner}.{table_name} (<COLUMN_1>, <COLUMN_2>)
ONLINE;
""",
        }
    if action == "generate_rebuild_index_sql":
        return {
            "title": "索引重建 SQL",
            "script": f"""ALTER INDEX {object_name} REBUILD ONLINE;
-- 如果使用 PARALLEL 重建，完成后请恢复：
ALTER INDEX {object_name} NOPARALLEL;""",
        }
    if action == "generate_archive_plan":
        return {
            "title": "历史归档处理建议",
            "script": f"""-- 建议按月分批归档，并先做行数校验
SELECT COUNT(*) AS archive_rows
FROM {owner}.{table_name}
WHERE <ARCHIVE_COLUMN> < ADD_MONTHS(TRUNC(SYSDATE), -6);

-- 可在历史归档中心生成：同库历史表、跨库历史库、CSV、EXPDP/IMPDP 完整方案。""",
        }
    if action == "generate_verify_sql":
        return {
            "title": "归档校验 SQL",
            "script": f"""SELECT COUNT(*) AS src_count FROM {owner}.{table_name} WHERE <ARCHIVE_CONDITION>;
SELECT COUNT(*) AS dst_count FROM <HISTORY_OWNER>.{table_name}_HIS WHERE <ARCHIVE_CONDITION>;
SELECT MIN(<ARCHIVE_COLUMN>), MAX(<ARCHIVE_COLUMN>) FROM {owner}.{table_name} WHERE <ARCHIVE_CONDITION>;""",
        }
    if action == "show_blocking_session":
        return {
            "title": "阻塞会话定位 SQL",
            "script": """SELECT s.sid,
       s.serial#,
       s.username,
       s.machine,
       s.program,
       s.event,
       s.blocking_session,
       s.sql_id
FROM v$session s
WHERE s.blocking_session IS NOT NULL
   OR s.sid IN (SELECT blocking_session FROM v$session WHERE blocking_session IS NOT NULL);""",
        }
    if action == "generate_undo_retention_plan":
        return {
            "title": "UNDO 保留时间调整 SQL",
            "script": """-- 只生成方案，请结合 UNDO 空间和业务长查询确认
ALTER SYSTEM SET undo_retention = 3600 SCOPE=BOTH SID='*';""",
        }
    if action == "open_undo_management":
        return {
            "title": "UNDO 风险核查 SQL",
            "script": """SELECT begin_time, end_time, maxquerylen, tuned_undoretention,
       ssolderrcnt, nospaceerrcnt, unxpstealcnt
FROM v$undostat
ORDER BY begin_time DESC
FETCH FIRST 48 ROWS ONLY;""",
        }
    if action == "show_dataguard_status":
        return {
            "title": "Data Guard 状态核查 SQL",
            "script": """SELECT name, value, unit, time_computed
FROM v$dataguard_stats;

SELECT * FROM v$archive_gap;""",
        }
    if action == "push_zabbix":
        return {
            "title": "Zabbix 联动建议",
            "script": "可点击 Zabbix 按钮生成推送载荷，由告警联动模块人工确认后推送。",
        }
    if action == "create_ticket":
        return {
            "title": "工单建议",
            "script": "可点击工单按钮生成标准工单描述，人工确认后提交到工单系统。",
        }
    return None


def _match_rule(row: DBCheckResult) -> str:
    text = " ".join(str(value or "") for value in [
        row.check_category,
        row.check_item,
        row.metric_name,
        row.finding,
        row.suggestion,
    ]).lower()
    for rule in DEFAULT_RULES:
        keywords = rule.get("condition_json", {}).get("keywords", [])
        if any(str(keyword).lower() in text for keyword in keywords):
            return rule["rule_code"]
    return "DB_HEALTH_ABNORMAL"


def _generic_rule() -> dict:
    return {
        "rule_code": "DB_HEALTH_ABNORMAL",
        "rule_name": "数据库健康检查异常",
        "category": "overview",
        "severity": "warning",
        "action_json": ["export_report", "create_ticket"],
    }


def _guess_object_type(category: str) -> str:
    mapping = {
        "space": "storage",
        "performance": "sql",
        "sql_index": "index",
        "transaction": "session",
        "ha": "standby",
        "archive": "archive_job",
        "external_monitor": "host",
    }
    return mapping.get(category, "instance")


def _guess_object_name(items: list[DBCheckResult]) -> str:
    for row in items:
        raw = row.raw_data if isinstance(row.raw_data, dict) else {}
        for key in ("tablespace_name", "object_name", "name", "sql_id", "dest_name"):
            if raw.get(key):
                return str(raw[key])
        if row.metric_name:
            return row.metric_name
        if row.check_item:
            return row.check_item
    return "-"


def _format_evidence(row: DBCheckResult) -> str:
    parts = [f"{row.check_category or '检查'} / {row.check_item or '-'}"]
    if row.metric_name:
        parts.append(f"指标 {row.metric_name}")
    if row.metric_value is not None:
        parts.append(f"当前值 {row.metric_value}")
    if row.threshold_value:
        parts.append(f"阈值 {row.threshold_value}")
    if row.finding:
        parts.append(f"发现：{row.finding}")
    return "，".join(parts)


def _build_root_cause(category: str, items: list[DBCheckResult]) -> str:
    names = "、".join(dict.fromkeys([row.check_item or row.metric_name or "未知检查项" for row in items[:5]]))
    templates = {
        "space": f"{names} 显示空间或归档相关指标异常，可能由对象增长、保留策略或底层文件系统空间不足引起。",
        "performance": f"{names} 显示 SQL 性能相关指标异常，可能与执行频率、逻辑读、访问路径或执行计划变化有关。",
        "sql_index": f"{names} 显示索引或统计信息治理风险，可能影响优化器选择和 SQL 执行计划稳定性。",
        "transaction": f"{names} 显示事务、锁或 UNDO 相关风险，可能与长事务、阻塞会话或保留窗口不足有关。",
        "ha": f"{names} 显示高可用链路异常，可能与日志传输、日志应用、归档 gap 或备库资源有关。",
        "archive": f"{names} 显示备份或历史归档流程风险，可能影响恢复能力、归档完整性或生产库清理节奏。",
        "external_monitor": f"{names} 显示外部监控上下文异常，需要结合主机、文件系统和告警平台一起判断。",
    }
    return templates.get(category, f"{names} 存在异常，需要结合证据链继续确认根因。")


def _build_impact(category: str, severity: str) -> str:
    level = "严重" if severity == "critical" else "潜在"
    templates = {
        "space": f"{level}空间风险可能导致写入失败、归档暂停、数据库挂起或业务接口报错。",
        "performance": f"{level}性能风险可能导致 MES 页面响应慢、接口超时、连接堆积和批处理延迟。",
        "sql_index": f"{level}索引/统计信息风险可能导致执行计划不稳定、全表扫描增加和 DML 成本上升。",
        "transaction": f"{level}事务锁风险可能导致过站、报工、追溯查询或生产接口卡顿。",
        "ha": f"{level}高可用风险可能降低容灾切换能力，增加故障恢复时间。",
        "archive": f"{level}备份归档风险可能影响恢复能力、审计留存和生产库空间治理。",
        "external_monitor": f"{level}外部监控风险可能说明数据库问题受到主机、磁盘或网络资源影响。",
    }
    return templates.get(category, f"{level}风险可能影响数据库稳定性和业务连续性。")


def _build_suggestion(category: str, items: list[DBCheckResult]) -> str:
    suggestions = [row.suggestion for row in items if row.suggestion]
    if suggestions:
        return "；".join(dict.fromkeys(suggestions[:5]))
    templates = {
        "space": "先确认增长来源和文件系统空间，再生成扩容、清理或历史归档方案。",
        "performance": "先定位 SQL_ID、执行计划和表访问路径，再评估统计信息、索引或分区治理。",
        "sql_index": "先确认索引是否失效、统计信息是否过期，再生成重建索引或统计信息收集脚本。",
        "transaction": "先定位阻塞源和长事务，再与业务确认是否可以结束会话或调整事务提交频率。",
        "ha": "先确认日志传输、日志应用、归档 gap 和备库空间，再生成修复步骤。",
        "archive": "先完成备份/归档校验，再生成重试、校验和生产清理脚本。",
        "external_monitor": "先关联 Zabbix/Prometheus 主机告警，再合并为综合诊断或转工单。",
    }
    return templates.get(category, "保留证据链，生成工单并在维护窗口执行变更。")
