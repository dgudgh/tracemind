"""端到端智能诊断中心 数据模型。

以 DiagIncident（诊断事件）为闭环顶层容器，串起：
故障时间线 → 证据链 → 根因排序 → 服务拓扑 → 处理建议 → 工单 → 复盘。
方法论知识库 / 架构画像 / 诊断数据源 为支撑表。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DiagIncident(Base):
    """诊断事件 —— 端到端闭环容器。"""

    __tablename__ = "diag_incident"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_no: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    db_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    instance_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 现象 / 触发
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(30), default="manual", index=True)  # manual/inspection/alert/awr/ai
    trigger_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 状态机：open→diagnosing→root_caused→ticketed→resolved→reviewed→closed
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    severity: Mapped[str] = mapped_column(String(20), default="info", index=True)  # critical/warning/info
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 画像 / 根因摘要
    arch_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    primary_layer: Mapped[str | None] = mapped_column(String(30), nullable=True)
    root_cause_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AI 结论
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)  # private/external/local
    ai_result: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    # 关联
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 统计
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    root_cause_count: Mapped[int] = mapped_column(Integer, default=0)
    # 时间戳
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    root_caused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagTimelineEvent(Base):
    """故障时间线事件。"""

    __tablename__ = "diag_timeline_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    layer: Mapped[str | None] = mapped_column(String(30), nullable=True)  # 七层
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)  # db/awr/zabbix/prometheus/os/trace...
    event_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagEvidence(Base):
    """证据链节点：现象 → 证据 → 判断 → 影响 → 建议。"""

    __tablename__ = "diag_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    layer: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)  # 七层
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phenomenon: Mapped[str | None] = mapped_column(Text, nullable=True)  # 现象
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)  # 证据
    judgement: Mapped[str | None] = mapped_column(Text, nullable=True)  # 判断
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)  # 影响
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)  # 建议
    metric_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    metric_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    threshold_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # 证据权重
    confidence: Mapped[int] = mapped_column(Integer, default=80)
    severity: Mapped[str] = mapped_column(String(20), default="info", index=True)
    raw: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagRootCause(Base):
    """根因候选与排序。total = w1·evidence_score + w2·prior_prob + w3·methodology_match。"""

    __tablename__ = "diag_root_cause"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    layer: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cause: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    prior_prob: Mapped[float] = mapped_column(Float, default=0.0)
    methodology_match: Mapped[float] = mapped_column(Float, default=0.0)
    total_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=70)
    methodology_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagTopologyNode(Base):
    """服务拓扑节点（per-incident 快照）。"""

    __tablename__ = "diag_topology_node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    node_key: Mapped[str] = mapped_column(String(60), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    node_type: Mapped[str | None] = mapped_column(String(40), nullable=True)  # user/nginx/tomcat/oracle/redis/es/kafka
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok/warn/error
    response_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagTopologyEdge(Base):
    """服务拓扑边。"""

    __tablename__ = "diag_topology_edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    from_key: Mapped[str] = mapped_column(String(60), nullable=False)
    to_key: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagMethodology(Base):
    """方法论知识库。"""

    __tablename__ = "diag_methodology"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    symptom: Mapped[str | None] = mapped_column(Text, nullable=True)  # 故障现象
    layer: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    applicable_arch: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    entry_point: Mapped[str | None] = mapped_column(Text, nullable=True)  # 诊断入口
    troubleshoot_path: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # 排查路径
    check_sql: Mapped[str | None] = mapped_column(Text, nullable=True)  # 检查 SQL
    check_command: Mapped[str | None] = mapped_column(Text, nullable=True)  # 检查命令
    judge_logic: Mapped[str | None] = mapped_column(Text, nullable=True)  # 判断逻辑
    solution: Mapped[str | None] = mapped_column(Text, nullable=True)  # 处理方案
    risk: Mapped[str | None] = mapped_column(Text, nullable=True)  # 风险提示
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # 复盘建议
    keywords: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # 匹配关键词（含等待事件）
    prior_prob: Mapped[float] = mapped_column(Float, default=0.5)  # 先验概率
    tags: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="builtin")  # builtin/sediment/manual
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagArchProfile(Base):
    """架构画像。"""

    __tablename__ = "diag_arch_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    arch_type: Mapped[str | None] = mapped_column(String(60), nullable=True)  # 单机/RAC/DataGuard/...
    arch_tags: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # [虚机, SAN, ASM...]
    details: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    priority_template: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # 层级优先级
    detected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagReview(Base):
    """复盘记录。"""

    __tablename__ = "diag_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    process: Mapped[str | None] = mapped_column(Text, nullable=True)  # 处理过程
    impact_scope: Mapped[str | None] = mapped_column(Text, nullable=True)  # 影响范围
    recovery_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 恢复时间
    optimizations: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)  # 优化项
    sediment_methodology_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DiagAutoPolicy(Base):
    """自动诊断策略：告警/定时触发端到端诊断（无人值守闭环）。"""

    __tablename__ = "diag_auto_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(20), default="alert")  # alert/scheduled/both
    interval_minutes: Mapped[int] = mapped_column(Integer, default=10)   # 评估间隔
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=120)  # 同实例两次诊断最小间隔
    min_severity: Mapped[str] = mapped_column(String(20), default="critical")  # 告警触发阈值 warning/critical
    auto_ticket: Mapped[int] = mapped_column(Integer, default=0)
    auto_notify: Mapped[int] = mapped_column(Integer, default=0)
    next_run_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_incident_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_trigger: Mapped[str | None] = mapped_column(String(40), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class DiagDataSource(Base):
    """诊断数据源（中间件 / APM / OS / 存储 采集器连接配置）。"""

    __tablename__ = "diag_data_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # nginx/tomcat/redis/kafka/skywalking/os/storage
    endpoint: Mapped[str | None] = mapped_column(String(300), nullable=True)
    auth_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)  # 加密后的 JSON 认证信息
    config_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
