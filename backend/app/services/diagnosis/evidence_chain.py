"""证据链聚合：多源信号 → 七层 diag_evidence 节点（现象→证据→判断→影响→建议）。

Phase A 源：规则诊断命中（巡检）、最新 AWR 分析等待事件、Zabbix/Prometheus 活跃告警。
Phase B 追加：中间件/APM/OS/存储采集器信号。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagEvidence
from app.models.external_monitoring import (
    PrometheusAlertCache,
    ZabbixHostMapping,
    ZabbixProblemCache,
)
from app.models.observability import ObservabilityAlertEvent, ObservabilityQueryAudit
from app.services.diagnosis import constants as C
from app.services.observability.metric_normalizer import infer_metric_from_text, normalize_metric_name
from app.services.awr_evidence import latest_current_awr, observation_window

logger = logging.getLogger("app.services.diagnosis.evidence_chain")
APM_EVIDENCE_MAX_AGE = timedelta(hours=24)
APM_MIN_MATCH_CONFIDENCE = 60


def _safe_bounded_int(value, *, default: int = 0, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, min(parsed, maximum))


def _from_rule_hits(rule_hits: list) -> list[dict]:
    """rule_engine.RuleHit → 证据节点。复用其已生成的现象/证据/根因/影响/建议。"""
    nodes = []
    for hit in rule_hits:
        if getattr(hit, "rule_code", "") in ("DB_HEALTH_NORMAL",):
            continue
        layer = C.RULE_CATEGORY_TO_LAYER.get(getattr(hit, "category", ""), C.LAYER_DB)
        nodes.append({
            "layer": layer,
            "source": "inspection",
            "phenomenon": f"{hit.rule_name}（{hit.object_type}/{hit.object_name}）",
            "evidence": "\n".join(f"- {e}" for e in (hit.evidence or [])),
            "judgement": hit.root_cause,
            "impact": hit.impact,
            "suggestion": hit.suggestion,
            "weight": 1.0 if hit.severity == "critical" else 0.6,
            "confidence": hit.confidence,
            "severity": hit.severity,
            "raw": {"rule_code": hit.rule_code, "category": hit.category, "actions": hit.actions},
        })
    return nodes


def _awr_severity(pct: float) -> str:
    if pct >= 30:
        return "critical"
    if pct >= 15:
        return "warning"
    return "info"


def _from_awr(db: Session, instance: DBInstance, *, as_of: datetime | None = None) -> list[dict]:
    """最新一份已分析 AWR → 重点等待事件证据。

    读取 awr_analyzer 实际产出结构 analysis_results['wait_analysis']['top_events']，
    每项含 event_name / db_time_pct / wait_class / wait_time。
    """
    report = latest_current_awr(db, instance.id, as_of)
    if not report or not isinstance(report.analysis_results, dict):
        return []
    wait_analysis = report.analysis_results.get("wait_analysis") or {}
    events = wait_analysis.get("top_events") or []
    if not isinstance(events, list):
        return []

    nodes = []
    for ev in events[:8]:
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("event_name") or "").strip()
        if not name:
            continue
        try:
            pct = float(ev.get("db_time_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        wait_class = ev.get("wait_class") or ""
        wait_time = ev.get("wait_time")
        key = name.lower()
        # 'DB CPU' 不是等待事件，但占比高同样重要 → 归 OS/CPU
        if key == "db cpu":
            layer, meaning = C.LAYER_OS, "DB CPU 占比高，CPU 计算资源紧张（多为低效 SQL/并行过高）"
        else:
            layer, meaning = C.WAIT_EVENT_LAYER.get(key, (C.LAYER_DB, f"{wait_class or '数据库'} 等待事件"))
        known = key in C.WAIT_EVENT_LAYER or key == "db cpu"
        sev = _awr_severity(pct)
        nodes.append({
            "layer": layer,
            "source": "awr",
            "phenomenon": f"AWR Top 等待事件：{name}（占 DB Time {pct:.1f}%）",
            "evidence": "，".join(filter(None, [
                f"占 DB Time {pct:.1f}%",
                f"等待类别 {wait_class}" if wait_class else None,
                f"总等待 {wait_time}s" if wait_time not in (None, "", "0") else None,
                f"AWR 报告 #{report.id}",
            ])),
            "judgement": meaning,
            "impact": "影响数据库吞吐与响应，需结合存储/网络/SQL 进一步定位。",
            "suggestion": "参考方法论库对应条目按层排查。",
            "metric_name": name,
            "metric_value": f"{pct:.1f}%",
            "threshold_value": "占 DB Time >15% 关注 / >30% 重点",
            "weight": (1.0 if sev == "critical" else 0.8) if known else 0.5,
            "confidence": 80 if known else 65,
            "severity": sev,
            "raw": {"awr_report_id": report.id, "event": ev, "observation_window": observation_window(report)},
        })
    return nodes


def _from_verified_apm_sql(db: Session, instance: DBInstance) -> list[dict]:
    """Load only bounded SQL fingerprints produced by a successful server-side APM query."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - APM_EVIDENCE_MAX_AGE
    audits = (
        db.query(ObservabilityQueryAudit)
        .filter(
            ObservabilityQueryAudit.query_kind == "apm_sql_evidence",
            ObservabilityQueryAudit.instance_id == instance.id,
            ObservabilityQueryAudit.status == "success",
            ObservabilityQueryAudit.create_time >= cutoff,
        )
        .order_by(ObservabilityQueryAudit.id.desc())
        .limit(20)
        .all()
    )
    nodes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for audit in audits:
        metadata = audit.query_metadata if isinstance(audit.query_metadata, dict) else {}
        if metadata.get("source_verified") is not True:
            continue
        for item in metadata.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            confidence = _safe_bounded_int(item.get("match_confidence"), maximum=100)
            if item.get("source_verified") is not True or item.get("instance_id") != instance.id or confidence < APM_MIN_MATCH_CONFIDENCE:
                continue
            identity = (str(item.get("trace_id") or ""), str(item.get("span_id") or ""))
            if identity in seen:
                continue
            seen.add(identity)
            duration_ms = _safe_bounded_int(item.get("duration_ms"), maximum=86_400_000)
            span_status = str(item.get("span_status") or "").lower()
            severity = "warning" if span_status == "error" or duration_ms >= 1000 else "info"
            service = str(item.get("service") or "未知服务")
            endpoint = str(item.get("endpoint") or "未知端点")
            fingerprint = str(item.get("sql_fingerprint") or "")[:1000]
            nodes.append({
                "layer": C.LAYER_APP,
                "source": "apm",
                "phenomenon": f"{service} / {endpoint} 调用了数据库 SQL",
                "evidence": (
                    f"服务端已验证 APM 数据源；trace={identity[0]}，span={identity[1]}，"
                    f"耗时={duration_ms}ms，实例匹配置信度={confidence}%，SQL 指纹={fingerprint}"
                ),
                "judgement": "应用调用路径与该数据库实例已可靠关联；是否构成根因仍需与数据库、主机和告警证据交叉验证。",
                "impact": "该调用可用于定位受影响服务、接口及对应 SQL 路径。",
                "suggestion": "结合同时段等待事件、执行计划和主机资源确认瓶颈，避免仅凭链路耗时下结论。",
                "metric_name": "apm_sql_duration_ms",
                "metric_value": str(duration_ms),
                "weight": 0.7 if severity == "warning" else 0.45,
                "confidence": confidence,
                "severity": severity,
                "raw": {
                    "source_id": item.get("source_id"),
                    "trace_id": identity[0],
                    "span_id": identity[1],
                    "sql_fingerprint": fingerprint,
                    "match_confidence": confidence,
                    "source_verified": True,
                },
            })
    return nodes


def _external_metric(source_metric: str | None, *parts) -> str:
    inferred = infer_metric_from_text(source_metric, *parts)
    normalized = normalize_metric_name(source_metric)
    if inferred:
        return inferred
    return normalized


def _external_alert_profile(metric_name: str | None, *parts) -> dict:
    metric = str(metric_name or "").lower()
    text = " ".join(str(part or "") for part in parts).lower()
    if metric.startswith("oracle.fra") or " fra" in text or "flash recovery" in text or "fast recovery" in text:
        return {
            "layer": C.LAYER_DB,
            "judgement": "Oracle FRA/归档恢复区容量告警，属于数据库容量风险。",
            "impact": "FRA 满可能导致归档失败、备份/闪回异常，严重时影响主库写入或 Data Guard 传输。",
            "suggestion": "检查 V$RECOVERY_FILE_DEST、归档生成速率、备份保留策略和可清理归档，按流程扩容或清理。",
            "confidence": 85,
        }
    if metric.startswith("db.tablespace") or "tablespace" in text or "表空间" in text:
        return {
            "layer": C.LAYER_DB,
            "judgement": "数据库表空间容量告警，属于数据库层容量风险。",
            "impact": "表空间不足可能导致对象扩展失败、业务写入失败或批处理异常。",
            "suggestion": "检查表空间使用率、数据文件 autoextend、磁盘余量和 Top Segment 增长，按流程扩容或清理。",
            "confidence": 85,
        }
    if metric.startswith(("oracle.dg", "db.replication", "mysql.replication")) or any(
        key in text for key in ("data guard", "dataguard", "archive gap", "replication", " lag")
    ):
        return {
            "layer": C.LAYER_DB,
            "judgement": "数据库复制/同步延迟告警，属于数据库高可用链路风险。",
            "impact": "复制延迟会扩大主备数据差距，影响切换窗口和灾备可用性。",
            "suggestion": "检查 Data Guard/MRP 或复制线程、归档传输、网络延迟和从库应用速率。",
            "confidence": 82,
        }
    if metric.startswith("db.connection"):
        return {
            "layer": C.LAYER_DB,
            "judgement": "数据库连接使用率告警，可能由连接池、并发突增或阻塞堆积引起。",
            "impact": "连接耗尽会导致新请求失败，并放大数据库响应时间。",
            "suggestion": "检查活跃会话、连接池配置、阻塞链和异常应用来源。",
            "confidence": 80,
        }
    if metric.startswith("host.network") or any(k in text for k in ("network", "丢包", "网卡", "packet", "ping")):
        return {
            "layer": C.LAYER_NETWORK,
            "judgement": "网络层告警，纳入证据链综合判断。",
            "impact": "网络异常可能导致数据库连接抖动、复制延迟或业务超时。",
            "suggestion": "联动网络、Zabbix/Prometheus 和数据库等待事件对照时间线。",
            "confidence": 72,
        }
    if metric.startswith(("host.disk", "host.io")):
        return {
            "layer": C.LAYER_STORAGE,
            "judgement": "主机磁盘/IO 告警，可能是数据库等待升高的上游诱因。",
            "impact": "存储延迟会放大数据库读写等待、Redo 写入和批处理耗时。",
            "suggestion": "检查磁盘 await、队列深度、Redo/归档盘和 AWR IO 等待事件。",
            "confidence": 75,
        }
    if metric.startswith("host."):
        return {
            "layer": C.LAYER_OS,
            "judgement": "主机资源层告警，可能是数据库慢的上游诱因。",
            "impact": "CPU/内存资源瓶颈会放大数据库等待和响应时间。",
            "suggestion": "结合主机指标、AWR DB CPU/内存相关指标和业务时间窗定位。",
            "confidence": 72,
        }
    return {
        "layer": C.LAYER_OS,
        "judgement": "外部监控告警，纳入证据链综合判断。",
        "impact": "需结合指标趋势与数据库等待事件确认影响面。",
        "suggestion": "确认实例绑定、告警指标含义和同时间窗数据库证据。",
        "confidence": 60,
    }


def _observability_alert_node(alert: ObservabilityAlertEvent) -> dict:
    raw = alert.raw_data if isinstance(alert.raw_data, dict) else {}
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
    description = alert.content or annotations.get("description") or annotations.get("summary")
    source_metric = (
        alert.metric_name
        or labels.get("metric")
        or labels.get("__name__")
        or labels.get("alertname")
        or alert.alert_key
    )
    metric = _external_metric(source_metric, alert.title, description, labels, annotations, raw)
    profile = _external_alert_profile(metric, alert.title, description, labels, annotations, raw)
    severity = alert.severity or "warning"
    weight = 0.8 if str(severity).lower() in {"critical", "high", "disaster", "严重"} else 0.6
    source = alert.source_type or "observability"
    source_label = {
        "prometheus": "Prometheus",
        "alertmanager": "Alertmanager",
        "zabbix": "Zabbix",
    }.get(source, "观测")
    return {
        "layer": profile["layer"],
        "source": source,
        "phenomenon": f"{source_label} 告警：{alert.title}",
        "evidence": "，".join(
            filter(
                None,
                [
                    f"主机/对象 {alert.host_id}" if alert.host_id else None,
                    f"状态 {alert.status or 'firing'}",
                    f"级别 {severity}",
                    f"时间 {alert.last_time}" if alert.last_time else None,
                ],
            )
        ),
        "judgement": profile["judgement"],
        "impact": profile["impact"],
        "suggestion": profile["suggestion"],
        "metric_name": metric,
        "metric_value": alert.metric_value,
        "weight": weight,
        "confidence": profile["confidence"],
        "severity": severity,
        "raw": {"observability_alert_id": alert.id, "alert_key": alert.alert_key, "metric_name": metric},
    }


def _dedupe_alert_nodes(nodes: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
    for node in nodes:
        key = (
            node.get("source"),
            node.get("phenomenon"),
            node.get("metric_name"),
            node.get("severity"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result


def _from_alerts(db: Session, instance: DBInstance) -> list[dict]:
    """Prometheus / Zabbix 活跃告警 → 七层证据。"""
    nodes = []
    prom = (
        db.query(PrometheusAlertCache)
        .filter(
            PrometheusAlertCache.db_instance_id == instance.id,
            PrometheusAlertCache.alert_status == "firing",
        )
        .limit(50)
        .all()
    )
    for a in prom:
        labels = a.labels_json if isinstance(a.labels_json, dict) else {}
        annotations = a.annotations_json if isinstance(a.annotations_json, dict) else {}
        source_metric = labels.get("metric") or labels.get("__name__") or labels.get("alertname") or a.alert_name
        description = annotations.get("description") or annotations.get("summary")
        metric = _external_metric(source_metric, a.alert_name, description, labels, annotations)
        profile = _external_alert_profile(metric, a.alert_name, description, labels, annotations)
        nodes.append({
            "layer": profile["layer"],
            "source": "prometheus",
            "phenomenon": f"Prometheus 告警：{a.alert_name}",
            "evidence": f"实例 {a.instance_name or '-'} 当前 firing",
            "judgement": profile["judgement"],
            "impact": profile["impact"],
            "suggestion": profile["suggestion"],
            "metric_name": metric,
            "weight": 0.7,
            "confidence": profile["confidence"],
            "severity": a.severity or "warning",
            "raw": {"prom_alert_id": a.id, "metric_name": metric},
        })

    hostids = [
        m.zabbix_hostid for m in
        db.query(ZabbixHostMapping).filter(ZabbixHostMapping.db_instance_id == instance.id).all()
    ]
    if hostids:
        zbx = (
            db.query(ZabbixProblemCache)
            .filter(
                ZabbixProblemCache.hostid.in_(hostids),
                ZabbixProblemCache.status.in_(["firing", "active", "problem"]),
            )
            .limit(50)
            .all()
        )
        for z in zbx:
            sev = "critical" if (z.severity or 0) >= 4 else "warning" if (z.severity or 0) >= 2 else "info"
            raw = z.raw_data if isinstance(z.raw_data, dict) else {}
            source_metric = raw.get("item_key") or raw.get("metric_name") or raw.get("key") or raw.get("key_") or z.objectid
            metric = _external_metric(source_metric, z.problem_name, raw.get("opdata"), raw.get("name"), raw)
            profile = _external_alert_profile(metric, z.problem_name, raw.get("opdata"), raw.get("name"), raw)
            nodes.append({
                "layer": profile["layer"],
                "source": "zabbix",
                "phenomenon": f"Zabbix 问题：{z.problem_name}",
                "evidence": f"主机 {z.host_name or '-'} 当前活跃，级别 {z.severity_name or z.severity}",
                "judgement": profile["judgement"],
                "impact": profile["impact"],
                "suggestion": profile["suggestion"],
                "metric_name": metric,
                "weight": 0.8 if sev == "critical" else 0.6,
                "confidence": profile["confidence"],
                "severity": sev,
                "raw": {"zbx_problem_id": z.id, "metric_name": metric},
            })

    observability_alerts = (
        db.query(ObservabilityAlertEvent)
        .filter(
            ObservabilityAlertEvent.instance_id == instance.id,
            ObservabilityAlertEvent.status.in_(["firing", "active", "problem"]),
        )
        .limit(50)
        .all()
    )
    nodes.extend(_observability_alert_node(alert) for alert in observability_alerts)
    return _dedupe_alert_nodes(nodes)


def _from_signals(signals: list) -> list[dict]:
    """采集器 Signal（异常项）→ 证据节点。"""
    nodes = []
    for s in signals or []:
        if not getattr(s, "abnormal", False):
            continue
        nodes.append({
            "layer": s.layer,
            "source": s.source,
            "phenomenon": s.phenomenon or f"{s.source} {s.metric}",
            "evidence": "，".join(filter(None, [
                f"{s.metric}={s.value}" if s.value is not None else None,
                f"阈值 {s.threshold}" if s.threshold is not None else None,
                f"来源 {(s.detail or {}).get('data_source')}" if (s.detail or {}).get("data_source") else None,
            ])),
            "judgement": s.judgement,
            "impact": s.impact,
            "suggestion": s.suggestion,
            "metric_name": s.metric,
            "metric_value": str(s.value) if s.value is not None else None,
            "threshold_value": str(s.threshold) if s.threshold is not None else None,
            "weight": 1.0 if s.status == "critical" else 0.7,
            "confidence": 75,
            "severity": s.severity,
            "raw": s.detail,
        })
    return nodes


def _from_awr_top_sql(db: Session, instance: DBInstance, *, as_of: datetime | None = None) -> list[dict]:
    """最新一份已分析 AWR → Top SQL 证据（SQL 层）。

    读取 analysis_results['sql_analysis']：top_elapsed_time_sql / top_buffer_gets_sql，
    每项含 sql_id / sql_text / elapsed_time / buffer_gets / executions / *_per_exec。
    """
    report = latest_current_awr(db, instance.id, as_of)
    if not report or not isinstance(report.analysis_results, dict):
        return []
    sql_analysis = report.analysis_results.get("sql_analysis") or {}
    if not isinstance(sql_analysis, dict):
        return []

    # 合并按耗时 / 逻辑读 Top SQL，按 sql_id 去重，保留更强指标
    by_id: dict[str, dict] = {}
    for sql in (sql_analysis.get("top_elapsed_time_sql") or [])[:5]:
        if isinstance(sql, dict) and sql.get("sql_id"):
            by_id.setdefault(sql["sql_id"], {})["elapsed"] = sql
    for sql in (sql_analysis.get("top_buffer_gets_sql") or [])[:5]:
        if isinstance(sql, dict) and sql.get("sql_id"):
            by_id.setdefault(sql["sql_id"], {})["gets"] = sql

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    nodes = []
    for sql_id, parts in list(by_id.items())[:6]:
        base = parts.get("elapsed") or parts.get("gets") or {}
        elapsed = _f(base.get("elapsed_time"))
        gets = _f(base.get("buffer_gets"))
        execs = _f(base.get("executions"))
        per_exec = _f(base.get("elapsed_per_exec"))
        sql_text = (base.get("sql_text") or "").strip().replace("\n", " ")[:160]
        # 严重度：单次耗时高或逻辑读巨大 → 重点
        if per_exec >= 5 or elapsed >= 60:
            sev, weight = "warning", 0.7
        else:
            sev, weight = "info", 0.4
        nodes.append({
            "layer": C.LAYER_SQL,
            "source": "awr",
            "phenomenon": f"AWR Top SQL：{sql_id}（耗时 {elapsed:.1f}s）",
            "evidence": "，".join(filter(None, [
                f"总耗时 {elapsed:.1f}s",
                f"单次 {per_exec:.2f}s" if per_exec else None,
                f"逻辑读 {gets:.0f}" if gets else None,
                f"执行 {execs:.0f} 次" if execs else None,
                f"SQL: {sql_text}" if sql_text else None,
            ])),
            "judgement": "高耗时/高逻辑读 SQL，可能缺索引、计划劣化或访问路径不佳。",
            "impact": "Top SQL 拉高 DB Time，影响相关业务接口与批处理。",
            "suggestion": "查看执行计划与谓词选择性，评估索引/统计信息/SQL 改写。",
            "metric_name": f"sql:{sql_id}",
            "metric_value": f"{elapsed:.1f}s",
            "weight": weight,
            "confidence": 70,
            "severity": sev,
            "raw": {"awr_report_id": report.id, "sql_id": sql_id, "elapsed": elapsed, "buffer_gets": gets,
                    "executions": execs, "observation_window": observation_window(report)},
        })
    return nodes


def build_evidence_chain(
    db: Session,
    incident_id: int,
    instance: DBInstance,
    rule_hits: list,
    collector_signals: list | None = None,
    hardware_profile: dict | None = None,
    *, as_of: datetime | None = None,
) -> list[DiagEvidence]:
    """聚合多源证据并持久化为 diag_evidence。返回已落库节点。"""
    nodes: list[dict] = []
    nodes += _from_rule_hits(rule_hits)
    try:
        nodes += _from_awr(db, instance, as_of=as_of)
    except Exception as e:
        logger.warning("evidence from AWR failed: %s", e)
    try:
        nodes += _from_awr_top_sql(db, instance, as_of=as_of)
    except Exception as e:
        logger.warning("evidence from AWR Top SQL failed: %s", e)
    try:
        nodes += _from_alerts(db, instance)
    except Exception as e:
        logger.warning("evidence from alerts failed: %s", e)
    try:
        nodes += _from_verified_apm_sql(db, instance)
    except Exception as e:
        logger.warning("evidence from verified APM SQL failed: %s", e)
    try:
        from app.services.diagnosis import hardware_profiler

        nodes += hardware_profiler.evidence_from_profile(hardware_profile)
    except Exception as e:
        logger.warning("evidence from hardware profile failed: %s", e)
    nodes += _from_signals(collector_signals)

    saved: list[DiagEvidence] = []
    for n in nodes:
        row = DiagEvidence(
            incident_id=incident_id,
            layer=n.get("layer"),
            source=n.get("source"),
            phenomenon=n.get("phenomenon"),
            evidence=n.get("evidence"),
            judgement=n.get("judgement"),
            impact=n.get("impact"),
            suggestion=n.get("suggestion"),
            metric_name=n.get("metric_name"),
            metric_value=n.get("metric_value"),
            threshold_value=n.get("threshold_value"),
            weight=n.get("weight", 1.0),
            confidence=n.get("confidence", 80),
            severity=n.get("severity", "info"),
            raw=n.get("raw"),
        )
        db.add(row)
        saved.append(row)
    db.flush()
    return saved


def to_dict(row: DiagEvidence) -> dict:
    from app.services.ai_evidence import evidence_state

    result = {
        "id": row.id,
        "layer": row.layer,
        "source": row.source,
        "phenomenon": row.phenomenon,
        "evidence": row.evidence,
        "judgement": row.judgement,
        "impact": row.impact,
        "suggestion": row.suggestion,
        "metric_name": row.metric_name,
        "metric_value": row.metric_value,
        "threshold_value": row.threshold_value,
        "weight": row.weight,
        "confidence": row.confidence,
        "severity": row.severity,
        "raw": row.raw,
    }
    result["execution_status"] = evidence_state(result)[1]
    return result
