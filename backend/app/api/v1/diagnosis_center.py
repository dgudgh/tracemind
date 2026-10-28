"""端到端智能诊断中心 API。"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_password, encrypt_password
from app.core.database import get_db
from app.models.db_instance import DBInstance
from app.models.diagnosis_center import (
    DiagArchProfile,
    DiagDataSource,
    DiagIncident,
    DiagMethodology,
)
from app.schemas.common import PageResponse
from app.schemas.diagnosis import (
    AutoPolicyUpdate,
    DataSourceCreate,
    DataSourceUpdate,
    DiagnoseRequest,
    MethodologyCreate,
    MethodologyUpdate,
    ReviewCreate,
    TicketFromIncidentRequest,
)
from app.services.ai_degradation import (
    AI_MODEL_FAILED,
    AI_MODEL_OK,
    AI_NOT_CONFIGURED,
    AI_OUTPUT_REJECTED,
    degradation,
    manual_for,
    no_manual,
)
from app.services.diagnosis import arch_profiler, auto_trigger, methodology_kb, orchestrator, retrospective

logger = logging.getLogger("app.api.diagnosis_center")
router = APIRouter(prefix="/diagnosis-center", tags=["智能诊断中心"])


class RouteIntentRequest(BaseModel):
    text: str
    current_key: str | None = None
    selected_instance_id: int | None = None
    selected_db_type: str | None = None


ROUTE_TARGETS = [
    {"key": "home.dashboard", "name": "工作台", "use": "平台风险总览、首页、快速入口"},
    {"key": "asset.center", "name": "资产管理", "use": "数据库实例、SSH 主机、内部 CMDB、外部 CMDB、架构拓扑和资产关系"},
    {"key": "inspect.run", "name": "一键巡检", "use": "执行数据库健康检查、生成巡检结果"},
    {"key": "inspect.reports", "name": "巡检报告", "use": "查看巡检报告、健康报告"},
    {"key": "inspect.tasks", "name": "巡检任务", "use": "定时巡检、任务调度"},
    {"key": "inspect.compare", "name": "历史对比", "use": "巡检前后对比、历史差异"},
    {"key": "inspect.policies", "name": "巡检策略", "use": "配置巡检规则、检查策略"},
    {"key": "perf.overview", "name": "性能总览", "use": "性能概览、整体性能态势"},
    {"key": "perf.sql", "name": "SQL 画像", "use": "SQL 分析、执行计划画像、SQL Profile"},
    {"key": "perf.slowsql", "name": "慢 SQL 分析", "use": "慢查询、耗时 SQL、SQL 超时"},
    {"key": "perf.lock", "name": "锁等待分析", "use": "锁等待、阻塞链、死锁、长事务定位"},
    {"key": "perf.index", "name": "索引分析", "use": "缺失索引、索引建议、索引优化"},
    {"key": "perf.table", "name": "表画像", "use": "表容量、对象统计、表分析"},
    {"key": "perf.baseline", "name": "性能基线", "use": "基线、阈值、性能对比"},
    {"key": "perf.trend", "name": "问题趋势", "use": "故障趋势、容量趋势、问题趋势"},
    {"key": "perf.capacity", "name": "容量分析", "use": "容量预测、空间趋势"},
    {"key": "perf.optimization", "name": "优化建议", "use": "调优中心、优化建议、数据库画像"},
    {"key": "perf.awr.report", "name": "AWR 综合", "use": "Oracle AWR 导出、上传、报告分析"},
    {"key": "perf.awr.history", "name": "AWR 历史报告", "use": "查看 AWR 历史报告"},
    {"key": "perf.awr.compare", "name": "AWR 对比", "use": "AWR 前后对比、报告对比"},
    {"key": "perf.awr.collect", "name": "AWR 自动采集", "use": "定时采集 AWR"},
    {"key": "diagnose.smart", "name": "智能诊断", "use": "AI 提问、规则分析、客户报障、现场问题、根因分析、证据链与故障传播链"},
    {"key": "ops.overview", "name": "运行概览", "use": "数据库启动关闭、运行状态、参数概览"},
    {"key": "ops.space", "name": "空间管理", "use": "表空间、磁盘空间、TEMP、UNDO、Redo、扩容方案"},
    {"key": "ops.log", "name": "日志管理", "use": "归档日志、FRA、Binlog、WAL、日志清理"},
    {"key": "ops.session", "name": "会话事务", "use": "会话、事务、锁、阻塞、连接数"},
    {"key": "ops.object", "name": "对象统计", "use": "表、索引、统计信息、碎片、对象分析"},
    {"key": "ops.security", "name": "用户安全", "use": "用户、账号、权限、授权安全"},
    {"key": "ops.ha", "name": "高可用", "use": "Data Guard、复制、主备、AlwaysOn、高可用状态"},
    {"key": "ops.governance", "name": "数据治理", "use": "历史数据、归档表、清理表、分区治理"},
    {"key": "ops.governance.objectStorage", "name": "归档对象存储", "use": "对象存储、归档文件、S3、MinIO、生命周期"},
    {"key": "backup.overview", "name": "备份总览", "use": "备份覆盖率、备份成功率、恢复点总览"},
    {"key": "backup.plan", "name": "备份方案", "use": "数据库备份、Oracle RMAN、EXPDP、MySQL 备份、备份策略、备份脚本"},
    {"key": "backup.restore", "name": "恢复方案", "use": "数据库恢复、还原、PITR、恢复演练"},
    {"key": "backup.report", "name": "报告与告警", "use": "备份报告、备份告警、备份失败"},
    {"key": "backup.platform", "name": "备份平台接入", "use": "外部备份平台、备份任务同步、恢复演练"},
    {"key": "rescue.overview", "name": "抢救总览", "use": "数据抢救、恢复总览"},
    {"key": "rescue.tasks", "name": "抢救任务", "use": "数据救援任务、恢复任务"},
    {"key": "monitor.alert.unified", "name": "综合告警", "use": "统一告警、监控事件、告警列表"},
    {"key": "monitor.alert.db", "name": "容量告警", "use": "数据库容量告警、空间预警"},
    {"key": "monitor.observability", "name": "可观测监控", "use": "指标采集、可观测、监控源"},
    {"key": "monitor.observability.logs", "name": "日志关联", "use": "日志平台、日志查询、故障时间线、日志关联分析"},
    {"key": "monitor.observability.grafana", "name": "Grafana 模板", "use": "Grafana、Dashboard、仪表盘模板"},
    {"key": "monitor.observability.apm", "name": "APM 链路", "use": "APM、Trace ID、调用链、SQL 链路追踪"},
    {"key": "integration.zabbix", "name": "Zabbix 联动", "use": "Zabbix 配置、Zabbix 告警"},
    {"key": "integration.prometheus", "name": "Prometheus 告警", "use": "Prometheus、Alertmanager"},
    {"key": "integration.notify", "name": "通知渠道", "use": "通知、售后群、消息渠道"},
    {"key": "integration.itsm", "name": "工单管理", "use": "工单、ITSM、售后单"},
    {"key": "protection.automation", "name": "自动化审批", "use": "自动化执行、操作审批、回滚方案"},
    {"key": "report.unified", "name": "统一报告", "use": "运维报告、售后报告、统一报告"},
    {"key": "report.alert", "name": "告警报告", "use": "告警统计、报警报告"},
    {"key": "system.ai", "name": "AI 配置", "use": "大模型、Ollama、Provider、模型配置"},
    {"key": "system.params", "name": "系统参数", "use": "系统设置、参数配置"},
    {"key": "system.schedule", "name": "调度任务", "use": "定时任务、计划任务、调度"},
    {"key": "system.user", "name": "用户管理", "use": "用户、账号、权限"},
    {"key": "system.identity", "name": "企业认证", "use": "企业身份源、OIDC、LDAP、单点登录、用户组映射"},
]

ROUTE_TARGET_MAP = {item["key"]: item for item in ROUTE_TARGETS}
LEGACY_ROUTE_KEYS = {
    "backupPolicies": "backup.plan",
    "backupTasks": "backup.plan",
    "backupOverview": "backup.overview",
    "backupRecoveryCenter": "backup.restore",
    "backupReports": "backup.report",
    "inspectionCenter": "inspect.run",
    "awrReport": "perf.awr.report",
    "slowSqlAnalysis": "perf.slowsql",
    "lockAnalysis": "perf.lock",
    "oracleSession": "ops.session",
    "oracleSpace": "ops.space",
    "oracleLogArchive": "ops.log",
    "diagnosisCenter": "diagnose.smart",
    "ruleDiagnosis": "diagnose.smart",
    "diagnose.ai": "diagnose.smart",
    "diagnose.rule": "diagnose.smart",
    "aiConfig": "system.ai",
}


def _normalize_route_key(key: str | None) -> str | None:
    if not key:
        return None
    normalized = LEGACY_ROUTE_KEYS.get(key, key)
    if normalized in ROUTE_TARGET_MAP:
        return normalized
    return None


def _extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _local_route_intent(text: str, selected_db_type: str | None = None) -> dict:
    lower = (text or "").lower()
    compact = "".join(lower.split())
    target_db_type = None
    if any(word in lower for word in ["oracle", "orcl", "rman", "expdp", "awr"]):
        target_db_type = "oracle"
    elif any(word in lower for word in ["mysql", "innodb", "xtrabackup", "mysqldump", "binlog"]):
        target_db_type = "mysql"
    elif any(word in lower for word in ["postgresql", "postgres", "pg_dump", "wal"]):
        target_db_type = "postgresql"
    elif any(word in lower for word in ["sqlserver", "sql server", "mssql"]):
        target_db_type = "sqlserver"
    checks = [
        ("backup.plan", ["备份", "rman", "expdp", "dump", "全备", "增备", "xtrabackup", "mysqldump"]),
        ("backup.restore", ["恢复", "还原", "restore", "recover", "pitr", "演练"]),
        ("perf.awr.report", ["awr", "ash"]),
        ("perf.slowsql", ["慢sql", "慢查询", "耗时sql", "sql慢", "执行计划"]),
        ("perf.lock", ["锁", "阻塞", "死锁", "rowlock", "lockwait", "enq:tx"]),
        ("inspect.run", ["巡检", "健康检查", "检查数据库"]),
        ("ops.space", ["表空间", "空间", "扩容", "容量不足", "temp", "undo"]),
        ("ops.log", ["归档", "fra", "日志清理", "binlog", "wal"]),
        ("monitor.alert.unified", ["告警", "报警", "异常事件"]),
        ("system.ai", ["ai配置", "模型配置", "ollama", "大模型"]),
        ("report.unified", ["报告", "售后报告", "运维报告"]),
    ]
    for key, keywords in checks:
        if any(word in compact or word in lower for word in keywords):
            target = ROUTE_TARGET_MAP[key]
            return {
                "action": "navigate",
                "target_key": key,
                "target_name": target["name"],
                "confidence": 0.62,
                "reason": f"本地兜底判断命中「{target['name']}」。",
                "target_db_type": target_db_type or selected_db_type,
                "source": "local",
            }
    return {
        "action": "diagnose",
        "target_key": "diagnose.smart",
        "target_name": "端到端智能诊断",
        "confidence": 0.5,
        "reason": "未识别到明确工具动作，按现场问题进入智能诊断。",
        "target_db_type": target_db_type or selected_db_type,
        "source": "local",
    }


def _build_route_prompt(payload: RouteIntentRequest, instance: DBInstance | None) -> str:
    target_lines = "\n".join(
        f"- {item['key']}｜{item['name']}｜适用：{item['use']}"
        for item in ROUTE_TARGETS
    )
    instance_line = "未选择实例"
    if instance:
        instance_line = (
            f"{instance.instance_name}，类型 {instance.db_type}，"
            f"地址 {instance.host}:{instance.port}"
        )
    elif payload.selected_db_type:
        instance_line = f"已选择数据库类型：{payload.selected_db_type}"

    return f"""你是 TraceMind AI 的首页智能分诊器。你的任务是根据用户一句话，判断应该打开哪个功能页面，或进入端到端根因诊断工作台。

用户输入：
{payload.text}

当前页面：{payload.current_key or '未知'}
当前实例：{instance_line}

可打开的页面只能从下面列表选择：
{target_lines}

判断规则：
1. 如果用户是在报故障、描述客户现场现象、报错、超时、锁等待、Nginx 499、业务无响应、需要根因分析，action 选 diagnose，target_key 必须是 diagnose.smart。
2. 如果用户明确要使用某个工具或运维功能，例如备份、恢复、巡检、AWR、慢 SQL、锁分析、表空间扩容、告警、报告、AI 配置，action 选 navigate，并选择最匹配的 target_key。
3. “我要对数据库进行备份”“需要 Oracle 备份”“生成 RMAN 备份方案”都应选择 backup.plan。
4. 不要返回不存在的页面 key。不要让用户自己选择功能。

只输出 JSON，不要输出解释文字。JSON 格式：
{{
  "action": "navigate 或 diagnose",
  "target_key": "页面 key",
  "target_db_type": "oracle/mysql/postgresql/sqlserver/null",
  "confidence": 0.0 到 1.0,
  "reason": "一句话说明为什么",
  "needs_instance": true 或 false
}}"""


@router.post("/route-intent")
def route_intent(payload: RouteIntentRequest, db: Session = Depends(get_db)):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请输入要分析的问题或功能")

    instance = None
    if payload.selected_instance_id:
        instance = db.query(DBInstance).filter(DBInstance.id == payload.selected_instance_id).first()

    try:
        from app.services.ai_config import call_ai_provider, get_default_ai_config

        ai_config = get_default_ai_config(db)
        if not ai_config:
            fallback = _local_route_intent(text, payload.selected_db_type)
            fallback["degradation"] = degradation(AI_NOT_CONFIGURED, local_result_available=True)
            fallback["manual"] = manual_for(fallback["degradation"], "diagnosis.evidence_review")
            return fallback
        ai_text = call_ai_provider(ai_config, _build_route_prompt(payload, instance))
        parsed = _extract_json_object(ai_text)
        action = "diagnose" if str(parsed.get("action", "")).lower() == "diagnose" else "navigate"
        target_key = _normalize_route_key(parsed.get("target_key"))
        if action == "diagnose":
            target_key = "diagnose.smart"
        if not target_key:
            logger.warning("AI route target invalid: %s | raw=%s", parsed.get("target_key"), ai_text[:300])
            fallback = _local_route_intent(text, payload.selected_db_type)
            fallback["degradation"] = degradation(AI_OUTPUT_REJECTED, local_result_available=True,
                                                  reason_code="invalid_output")
            fallback["manual"] = manual_for(fallback["degradation"], "diagnosis.evidence_review")
            return fallback
        target = ROUTE_TARGET_MAP[target_key]
        confidence = parsed.get("confidence", 0.7)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except Exception:
            confidence = 0.7
        return {
            "action": action,
            "target_key": target_key,
            "target_name": target["name"],
            "target_db_type": parsed.get("target_db_type") if parsed.get("target_db_type") in {"oracle", "mysql", "postgresql", "sqlserver"} else payload.selected_db_type,
            "confidence": confidence,
            "reason": parsed.get("reason") or f"AI 判断应打开「{target['name']}」。",
            "needs_instance": bool(parsed.get("needs_instance", target_key.startswith(("ops.", "perf.", "inspect.", "backup.", "diagnose.", "monitor.alert.db")))),
            "source": "ai",
            "ai_provider": ai_config.provider_name,
            "model_name": ai_config.model_name,
            "degradation": degradation(AI_MODEL_OK, local_result_available=True),
            "manual": no_manual(),
        }
    except Exception as e:
        logger.warning("AI route intent failed: %s", e)
        fallback = _local_route_intent(text, payload.selected_db_type)
        fallback["ai_error"] = "AI 分诊不可用，已按本地规则识别意图。"
        fallback["degradation"] = degradation(AI_MODEL_FAILED, local_result_available=True,
                                              reason_code="provider_unavailable")
        fallback["manual"] = manual_for(fallback["degradation"], "diagnosis.evidence_review")
        return fallback


def _get_incident(db: Session, incident_id: int) -> DiagIncident:
    row = db.query(DiagIncident).filter(DiagIncident.id == incident_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="诊断事件不存在")
    return row


# --------------------------------------------------------------------------- 诊断
@router.post("/diagnose")
def diagnose(payload: DiagnoseRequest, db: Session = Depends(get_db)):
    try:
        detail = orchestrator.run_diagnosis(
            db,
            payload.instance_id,
            title=payload.title,
            symptom=payload.symptom,
            intent_summary=payload.intent_summary,
            intent_signals=payload.intent_signals,
            recommended_cards=payload.recommended_cards,
            trigger_source=payload.trigger_source,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 可选自动转工单 / 通知
    if payload.create_ticket:
        try:
            _create_ticket_for_incident(db, detail["id"], TicketFromIncidentRequest())
            detail = orchestrator.get_incident_detail(db, detail["id"])
        except HTTPException as e:
            detail["ticket_error"] = e.detail
    if payload.notify:
        try:
            _notify_incident(db, detail["id"])
        except HTTPException as e:
            detail["notify_error"] = e.detail
    return detail


@router.get("/incidents", response_model=PageResponse)
def list_incidents(
    instance_id: int | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total = orchestrator.list_incidents(db, instance_id, status, page, page_size)
    return PageResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    _get_incident(db, incident_id)
    return orchestrator.get_incident_detail(db, incident_id)


# --------------------------------------------------------------------------- 工单
def _create_ticket_for_incident(db: Session, incident_id: int, payload: TicketFromIncidentRequest):
    from app.itsm.models import ItsmConfig
    from app.itsm.schemas import TicketCreate
    from app.itsm.service import create_ticket

    incident = _get_incident(db, incident_id)
    config_id = payload.config_id
    if not config_id:
        cfg = db.query(ItsmConfig).filter(ItsmConfig.enabled == 1).order_by(ItsmConfig.id).first()
        if not cfg:
            raise HTTPException(status_code=400, detail="未配置可用的工单系统")
        config_id = cfg.id

    detail = orchestrator.get_incident_detail(db, incident_id)
    desc_lines = [
        f"诊断事件：{incident.incident_no}",
        f"实例：{incident.instance_name}（{incident.db_type}）",
        f"架构：{incident.arch_type}",
        f"综合风险：{incident.severity}，评分：{incident.score}",
        f"主因层：{incident.primary_layer}",
        f"根因：{incident.root_cause_summary}",
        "",
        "AI 结论：",
        incident.ai_summary or "（无）",
    ]
    priority = payload.priority or ("高" if incident.severity == "critical" else "中" if incident.severity == "warning" else "低")
    ticket = create_ticket(db, TicketCreate(
        config_id=config_id,
        source_type="AI_DIAGNOSIS",
        source_id=incident.incident_no,
        source_ref_id=incident.id,
        instance_id=incident.instance_id,
        instance_name=incident.instance_name,
        db_type=incident.db_type,
        title=f"[诊断] {incident.title}",
        description="\n".join(desc_lines),
        priority=priority,
        severity=incident.severity,
        category="数据库诊断",
        assignee=payload.assignee,
    ))
    tid = ticket.get("ticket_id") if isinstance(ticket, dict) else getattr(ticket, "id", None)
    if tid:
        incident.ticket_id = tid
    incident.status = "ticketed"
    db.commit()
    return ticket


@router.post("/incidents/{incident_id}/ticket")
def create_ticket_for_incident(incident_id: int, payload: TicketFromIncidentRequest, db: Session = Depends(get_db)):
    ticket = _create_ticket_for_incident(db, incident_id, payload)
    return {"status": "ok", "ticket": ticket, "incident": orchestrator.get_incident_detail(db, incident_id)}


# --------------------------------------------------------------------------- 通知
def _notify_incident(db: Session, incident_id: int):
    from app.notify.schemas import EventCreate
    from app.notify.service import create_event, dispatch_event

    incident = _get_incident(db, incident_id)
    level = "critical" if incident.severity == "critical" else "warning" if incident.severity == "warning" else "normal"
    event = create_event(db, EventCreate(
        event_type="DB_DIAGNOSIS",
        level=level,
        instance_id=incident.instance_id,
        title=f"[诊断] {incident.title}",
        body=f"根因：{incident.root_cause_summary}\n{incident.ai_summary or ''}",
    ))
    event_id = event.id if hasattr(event, "id") else (event.get("id") if isinstance(event, dict) else event)
    return dispatch_event(db, event_id)


@router.post("/incidents/{incident_id}/notify")
def notify_incident(incident_id: int, db: Session = Depends(get_db)):
    result = _notify_incident(db, incident_id)
    return {"status": "ok", "result": result}


# --------------------------------------------------------------------------- 复盘
@router.post("/incidents/{incident_id}/review")
def create_review(incident_id: int, payload: ReviewCreate, db: Session = Depends(get_db)):
    incident = _get_incident(db, incident_id)
    review = retrospective.create_review(db, incident, payload.model_dump())
    return {"status": "ok", "review": retrospective.to_dict(review),
            "incident": orchestrator.get_incident_detail(db, incident_id)}


# --------------------------------------------------------------------------- 架构画像
@router.get("/instances/{instance_id}/arch-profile")
def get_arch_profile(instance_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagArchProfile).filter(DiagArchProfile.instance_id == instance_id).first()
    if not row:
        instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
        if not instance:
            raise HTTPException(status_code=404, detail="实例不存在")
        row = arch_profiler.profile_architecture(db, instance, refresh=True)
    return arch_profiler.to_dict(row)


@router.post("/instances/{instance_id}/arch-profile/refresh")
def refresh_arch_profile(instance_id: int, db: Session = Depends(get_db)):
    instance = db.query(DBInstance).filter(DBInstance.id == instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    row = arch_profiler.profile_architecture(db, instance, refresh=True)
    return arch_profiler.to_dict(row)


# --------------------------------------------------------------------------- 自动诊断策略
@router.get("/instances/{instance_id}/auto-policy")
def get_auto_policy(instance_id: int, db: Session = Depends(get_db)):
    if not db.query(DBInstance).filter(DBInstance.id == instance_id).first():
        raise HTTPException(status_code=404, detail="实例不存在")
    return auto_trigger.policy_to_dict(auto_trigger.get_or_create_policy(db, instance_id))


@router.put("/instances/{instance_id}/auto-policy")
def update_auto_policy(instance_id: int, payload: AutoPolicyUpdate, db: Session = Depends(get_db)):
    if not db.query(DBInstance).filter(DBInstance.id == instance_id).first():
        raise HTTPException(status_code=404, detail="实例不存在")
    return auto_trigger.policy_to_dict(auto_trigger.update_policy(db, instance_id, payload.model_dump()))


@router.post("/auto/run-now")
def run_auto_now(db: Session = Depends(get_db)):
    """立即评估一次所有自动诊断策略（手动驱动，便于验证）。"""
    n = auto_trigger.run_due_auto_diagnosis(db)
    return {"status": "ok", "triggered": n}


# --------------------------------------------------------------------------- 方法论库
@router.get("/methodology")
def list_methodology(layer: str | None = Query(None), keyword: str | None = Query(None), db: Session = Depends(get_db)):
    return {"items": methodology_kb.list_methodologies(db, layer, keyword)}


@router.post("/methodology")
def create_methodology(payload: MethodologyCreate, db: Session = Depends(get_db)):
    row = methodology_kb.create_methodology(db, payload.model_dump(exclude_unset=True))
    return methodology_kb.to_dict(row)


@router.put("/methodology/{methodology_id}")
def update_methodology(methodology_id: int, payload: MethodologyUpdate, db: Session = Depends(get_db)):
    row = db.query(DiagMethodology).filter(DiagMethodology.id == methodology_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="方法论不存在")
    return methodology_kb.to_dict(methodology_kb.update_methodology(db, row, payload.model_dump(exclude_unset=True)))


@router.delete("/methodology/{methodology_id}")
def delete_methodology(methodology_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagMethodology).filter(DiagMethodology.id == methodology_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="方法论不存在")
    methodology_kb.delete_methodology(db, row)
    return {"status": "ok"}


# --------------------------------------------------------------------------- 诊断数据源
def _data_source_to_dict(row: DiagDataSource) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "source_type": row.source_type,
        "endpoint": row.endpoint,
        "has_auth": bool(row.auth_cipher),
        "config_json": row.config_json,
        "instance_id": row.instance_id,
        "enabled": row.enabled,
        "remark": row.remark,
        "create_time": row.create_time.isoformat(sep=" ") if row.create_time else None,
    }


@router.get("/data-sources")
def list_data_sources(source_type: str | None = Query(None), db: Session = Depends(get_db)):
    q = db.query(DiagDataSource)
    if source_type:
        q = q.filter(DiagDataSource.source_type == source_type)
    return {"items": [_data_source_to_dict(r) for r in q.order_by(DiagDataSource.id.desc()).all()]}


@router.post("/data-sources")
def create_data_source(payload: DataSourceCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    auth = data.pop("auth", None)
    row = DiagDataSource(
        **{k: v for k, v in data.items()},
        auth_cipher=encrypt_password(json.dumps(auth)) if auth else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _data_source_to_dict(row)


@router.put("/data-sources/{ds_id}")
def update_data_source(ds_id: int, payload: DataSourceUpdate, db: Session = Depends(get_db)):
    row = db.query(DiagDataSource).filter(DiagDataSource.id == ds_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    data = payload.model_dump(exclude_unset=True)
    auth = data.pop("auth", None)
    if auth is not None:
        row.auth_cipher = encrypt_password(json.dumps(auth)) if auth else None
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return _data_source_to_dict(row)


@router.post("/data-sources/{ds_id}/test")
def test_data_source(ds_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagDataSource).filter(DiagDataSource.id == ds_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    from app.services.diagnosis.collectors_runner import test_data_source as _test
    return _test(db, row)


@router.delete("/data-sources/{ds_id}")
def delete_data_source(ds_id: int, db: Session = Depends(get_db)):
    row = db.query(DiagDataSource).filter(DiagDataSource.id == ds_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    db.delete(row)
    db.commit()
    return {"status": "ok"}
