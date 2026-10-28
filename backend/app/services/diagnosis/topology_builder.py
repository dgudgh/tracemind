"""服务拓扑：用户 → Nginx → Tomcat → DB → Redis/Kafka，节点状态来自采集信号。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.db_instance import DBInstance
from app.models.diagnosis_center import DiagTopologyEdge, DiagTopologyNode

logger = logging.getLogger("app.services.diagnosis.topology_builder")

# 标准链路顺序（存在哪些节点由数据源决定，DB 节点恒在）
CHAIN = [
    ("user", "用户", "user"),
    ("nginx", "Nginx", "nginx"),
    ("tomcat", "Tomcat", "tomcat"),
    ("db", "数据库", "db"),
    ("redis", "Redis", "redis"),
    ("kafka", "Kafka", "kafka"),
]
# 采集 source_type → 拓扑节点 key
SOURCE_TO_NODE = {"nginx": "nginx", "tomcat": "tomcat", "redis": "redis", "kafka": "kafka",
                  "skywalking": "user", "os": "db", "storage": "db"}


def _worst(a: str, b: str) -> str:
    order = {"ok": 0, "warn": 1, "critical": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def build_topology(
    db: Session,
    incident_id: int,
    instance: DBInstance,
    collector_signals: list | None = None,
) -> tuple[list[DiagTopologyNode], list[DiagTopologyEdge]]:
    """构建拓扑快照。节点状态/错误/告警数由采集信号聚合。"""
    signals = collector_signals or []

    # 聚合每个节点的状态/告警计数/响应时间
    agg: dict[str, dict] = {}
    present = {"user", "db"}  # 用户与数据库恒在
    for s in signals:
        node = SOURCE_TO_NODE.get(s.source, None)
        if not node:
            continue
        present.add(node)
        a = agg.setdefault(node, {"status": "ok", "alert": 0, "rt": None})
        a["status"] = _worst(a["status"], s.status)
        if getattr(s, "abnormal", False):
            a["alert"] += 1
        if s.metric in ("service_resp_time",) and s.value is not None:
            a["rt"] = s.value

    nodes: list[DiagTopologyNode] = []
    order = 0
    node_keys_in_order = []
    for key, name, ntype in CHAIN:
        if key not in present:
            continue
        a = agg.get(key, {})
        label = name
        if key == "db":
            label = f"数据库 {instance.instance_name}"
        row = DiagTopologyNode(
            incident_id=incident_id,
            node_key=key,
            node_name=label,
            node_type=ntype,
            status=a.get("status", "ok"),
            response_time_ms=a.get("rt"),
            error_count=0,
            alert_count=a.get("alert", 0),
            metrics={"source_count": sum(1 for s in signals if SOURCE_TO_NODE.get(s.source) == key)},
            order_index=order,
        )
        db.add(row)
        nodes.append(row)
        node_keys_in_order.append(key)
        order += 1

    # 边：相邻链路节点串联（user→nginx→tomcat→db），db→redis / db→kafka 为依赖边
    edges: list[DiagTopologyEdge] = []
    linear = [k for k in ["user", "nginx", "tomcat", "db"] if k in present]
    for i in range(len(linear) - 1):
        edges.append(DiagTopologyEdge(incident_id=incident_id, from_key=linear[i], to_key=linear[i + 1], status="ok"))
    for dep in ["redis", "kafka"]:
        if dep in present:
            edges.append(DiagTopologyEdge(incident_id=incident_id, from_key="db", to_key=dep, status=agg.get(dep, {}).get("status", "ok")))
    for e in edges:
        db.add(e)

    db.flush()
    return nodes, edges


def node_to_dict(row: DiagTopologyNode) -> dict:
    return {
        "node_key": row.node_key,
        "node_name": row.node_name,
        "node_type": row.node_type,
        "status": row.status,
        "response_time_ms": row.response_time_ms,
        "error_count": row.error_count,
        "alert_count": row.alert_count,
        "metrics": row.metrics or {},
        "order_index": row.order_index,
    }


def edge_to_dict(row: DiagTopologyEdge) -> dict:
    return {"from_key": row.from_key, "to_key": row.to_key, "status": row.status, "latency_ms": row.latency_ms}
