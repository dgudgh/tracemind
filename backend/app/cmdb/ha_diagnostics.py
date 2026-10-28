from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.cmdb import normalized_service
from app.cmdb.models import AssetCluster, AssetClusterHealthSnapshot, AssetInstance, AssetRelation
from app.cmdb.replication import manual_relation, normalized_role_derived


PRIMARY_ROLES = {"primary", "adg_primary", "alwayson_primary"}
SECONDARY_ROLES = {
    "replica",
    "standby",
    "read_only",
    "readonly",
    "adg_standby",
    "alwayson_secondary",
}
CANDIDATE_ROLES = {"candidate", "arbiter"}
REPLICATED_ARCHITECTURES = {
    "primary_replica",
    "primary_standby",
    "replication",
    "mgr",
    "adg",
    "data_guard",
    "alwayson",
    "always_on",
    "streaming_replication",
}
DIRECTIONAL_REPLICATION_ARCHITECTURES = {
    "primary_replica",
    "adg",
    "alwayson",
    "streaming_replication",
}
ARCHITECTURE_ALIASES = {
    "single": "single",
    "standalone": "single",
    "primary_standby": "primary_replica",
    "replication": "primary_replica",
    "data_guard": "adg",
    "always_on": "alwayson",
}
LAG_WARNING_SECONDS = 30.0
LAG_CRITICAL_SECONDS = 300.0
STALE_AFTER_HOURS = 24
PLAN_BLOCKING_CODES = {
    "HA_DATA_UNVERIFIED",
    "HA_PARTIAL_VERIFICATION",
    "HA_MEMBER_UNREACHABLE",
    "HA_DATA_STALE",
    "HA_ROLE_UNKNOWN",
    "HA_PRIMARY_MISSING",
    "HA_SECONDARY_MISSING",
    "HA_MGR_PRIMARY_MODE",
    "HA_MGR_MEMBER_COUNT",
    "HA_REPLICATION_LAG_WARNING",
    "HA_REPLICATION_LAG_CRITICAL",
    "HA_LOG_GAP",
    "HA_QUORUM_RISK",
    "HA_MULTIPLE_PRIMARY",
    "HA_TOPOLOGY_MISSING",
    "HA_TOPOLOGY_PARTIAL",
    "HA_TOPOLOGY_AMBIGUOUS",
    "HA_TOPOLOGY_SCOPE_CONFLICT",
    "HA_TOPOLOGY_ENDPOINT_UNVERIFIED",
    "HA_TOPOLOGY_MANUAL_ONLY",
    "HA_TOPOLOGY_RELATION_UNVERIFIED",
    "HA_COMPOSITE_TOPOLOGY_UNVERIFIED",
}


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    try:
        return max(int(float(value or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _finding(
    code: str,
    severity: str,
    title: str,
    description: str,
    *,
    evidence: dict[str, Any] | None = None,
    recommendation: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "description": description,
        "evidence": evidence or {},
        "recommendation": recommendation,
    }


def _member_lag(attributes: dict[str, Any]) -> float | None:
    values = [
        _float(attributes.get("replication_delay_seconds")),
        _float(attributes.get("transport_lag_seconds")),
        _float(attributes.get("apply_lag_seconds")),
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _member_gap(attributes: dict[str, Any]) -> int:
    return max(
        _int(attributes.get("log_gap_count")),
        _int(attributes.get("archive_gap_count")),
        _int(attributes.get("archive_gap")),
        _int(attributes.get("wal_gap_count")),
    )


def _endpoint_facts(cluster: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {
        "vips": set(),
        "listeners": set(),
        "services": set(),
    }
    payloads = [cluster.get("attributes") or {}, *(item.get("attributes") or {} for item in members)]
    for payload in payloads:
        for key, target in (
            ("vip", "vips"),
            ("virtual_ip", "vips"),
            ("listener", "listeners"),
            ("listener_name", "listeners"),
            ("listener_endpoint", "listeners"),
            ("service", "services"),
            ("service_name", "services"),
        ):
            raw = payload.get(key)
            if isinstance(raw, list):
                values[target].update(str(item).strip() for item in raw if str(item).strip())
            elif raw not in (None, ""):
                values[target].add(str(raw).strip())
    return {key: sorted(value) for key, value in values.items()}


def _mgr_is_multi_primary(attributes: dict[str, Any]) -> bool | None:
    value = attributes.get("single_primary_mode")
    if isinstance(value, bool):
        return not value
    if str(value).strip().lower() in {"on", "true", "1", "yes"}:
        return False
    if str(value).strip().lower() in {"off", "false", "0", "no"}:
        return True
    mode = str(attributes.get("primary_mode") or attributes.get("mgr_mode") or "").lower()
    if "multi" in mode:
        return True
    if "single" in mode:
        return False
    return None


def _topology_scope(member: AssetInstance) -> tuple[Any, ...]:
    return (
        member.cluster_id,
        member.tenant_id,
        member.organization_id,
        str(member.db_type or "").casefold(),
    )


def _has_cycle(node_ids: set[int], edges: set[tuple[int, int]]) -> bool:
    adjacency: dict[int, set[int]] = {node_id: set() for node_id in node_ids}
    for source_id, target_id in edges:
        adjacency[source_id].add(target_id)
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node_id: int) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(target_id) for target_id in adjacency[node_id]):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in node_ids)


def _has_upstream_identity(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if any(value.get(key) not in (None, "") for key in (
        "db_unique_name", "server_uuid", "server_id", "replica_id",
    )):
        return True
    if str(value.get("server_name") or "").strip() and str(value.get("availability_group_id") or "").strip():
        return True
    host = str(value.get("host") or "").strip()
    try:
        port = int(value.get("port") or 0)
    except (TypeError, ValueError):
        return False
    return bool(host) and 0 < port < 65536


def _verified_observed_relation(relation: AssetRelation) -> bool:
    """Accept only explicit upstream evidence, not an automatic-source label alone."""
    attributes = relation.attributes
    if not isinstance(attributes, dict) or attributes.get("provenance") != "observed":
        return False
    evidence = attributes.get("evidence")
    if isinstance(evidence, dict):
        return (
            attributes.get("discovery_method") == "cmdb_relation_projection"
            and evidence.get("state") == "verified"
            and _has_upstream_identity(evidence.get("upstream"))
        )
    return (
        _has_upstream_identity(attributes.get("upstream"))
        and attributes.get("state") in (None, "verified")
    )


def _replication_topology_assessment(
    db: Session,
    cluster_row: AssetCluster,
    architecture: str | None,
    members: list[dict[str, Any]],
    member_facts: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Assess persisted, evidence-backed direction; never derive edges from roles."""
    if architecture not in DIRECTIONAL_REPLICATION_ARCHITECTURES:
        return "not_required", []

    findings: list[dict[str, Any]] = []
    member_rows = {
        row.id: row
        for row in db.query(AssetInstance).filter(AssetInstance.cluster_id == cluster_row.id).all()
        if row.status not in {"merged", "inactive"}
    }
    member_ids = set(member_rows)
    unverified_endpoints = [
        item for item in member_facts
        if not item.get("verified") or not item.get("endpoint")
    ]
    if unverified_endpoints:
        findings.append(_finding(
            "HA_TOPOLOGY_ENDPOINT_UNVERIFIED", "warning", "拓扑节点端点身份未完整验证",
            f"{len(unverified_endpoints)}/{len(member_facts)} 个节点缺少已验证的数据库端点身份。",
            evidence={"members": [item.get("name") or item.get("id") for item in unverified_endpoints]},
            recommendation="补齐节点连接端点并完成数据库身份核验后，再生成切换方案。",
        ))

    relations = db.query(AssetRelation).filter(
        AssetRelation.status == "active",
        AssetRelation.relation_type == "replicates_to",
    ).all()
    observed_edges: set[tuple[int, int]] = set()
    manual_edges: set[tuple[int, int]] = set()
    unverified_relations: list[int] = []
    derived_count = 0
    scope_conflicts: list[dict[str, Any]] = []
    cluster_scope = (
        cluster_row.tenant_id,
        cluster_row.organization_id,
        str(cluster_row.db_type or "").casefold(),
    )
    for relation in relations:
        touches_cluster = relation.source_id in member_ids or relation.target_id in member_ids
        if not touches_cluster:
            continue
        source = member_rows.get(relation.source_id)
        target = member_rows.get(relation.target_id)
        if (
            relation.source_type != "instance"
            or relation.target_type != "instance"
            or source is None
            or target is None
            or source.id == target.id
            or _topology_scope(source) != _topology_scope(target)
            or _topology_scope(source)[1:] != cluster_scope
        ):
            scope_conflicts.append({
                "relation_id": relation.id,
                "source_id": relation.source_id,
                "target_id": relation.target_id,
            })
            continue
        if normalized_role_derived(relation):
            derived_count += 1
            continue
        edge = (source.id, target.id)
        if relation.discovery_source == "manual" or manual_relation(relation.attributes):
            manual_edges.add(edge)
            continue
        if not _verified_observed_relation(relation):
            unverified_relations.append(relation.id)
            continue
        observed_edges.add(edge)

    if scope_conflicts:
        findings.append(_finding(
            "HA_TOPOLOGY_SCOPE_CONFLICT", "warning", "复制关系超出当前集群作用域",
            "存在指向其他集群、租户、组织或数据库类型的关系，不能用于当前 HA 就绪判断。",
            evidence={"relations": scope_conflicts},
            recommendation="修正资产归属和关系端点，确保复制关系两端属于同一 HA 集群。",
        ))
    if manual_edges and not observed_edges:
        findings.append(_finding(
            "HA_TOPOLOGY_MANUAL_ONLY", "warning", "手工复制关系不能替代实时拓扑证据",
            "手工关系可用于展示，但在数据库身份和方向证据被自动验证前不能支撑切换就绪。",
            evidence={"manual_edges": sorted(manual_edges)},
            recommendation="执行自动识别并用数据库原生复制元数据验证关系方向。",
        ))
    elif manual_edges:
        findings.append(_finding(
            "HA_TOPOLOGY_MANUAL_IGNORED", "info", "手工复制关系未用于就绪判定",
            "当前就绪结论仅使用自动验证关系；手工关系仅保留为展示和审计信息。",
            evidence={"manual_edges": sorted(manual_edges)},
        ))
    if unverified_relations:
        findings.append(_finding(
            "HA_TOPOLOGY_RELATION_UNVERIFIED", "warning", "复制关系缺少可验证来源证据",
            "automatic 标签本身不能证明复制方向；部分 active 关系没有明确的 observed upstream 证据。",
            evidence={"relation_ids": sorted(unverified_relations)},
            recommendation="用数据库原生上游元数据重新发现关系，并保留 verified evidence。",
        ))

    if len(member_ids) < 2 or not observed_edges:
        findings.append(_finding(
            "HA_TOPOLOGY_MISSING", "warning", "复制拓扑关系尚未验证",
            "复制型架构缺少可用于 HA 判断的已验证主从关系。",
            evidence={
                "member_count": len(member_ids),
                "observed_relation_count": len(observed_edges),
                "manual_relation_count": len(manual_edges),
                "role_derived_relation_count": derived_count,
                "unverified_relation_count": len(unverified_relations),
            },
            recommendation="采集数据库原生上游证据并持久化 active 的同集群复制关系。",
        ))
        return "unknown", findings

    roles = {item.get("id"): item.get("role") for item in members}
    primary_ids = {node_id for node_id, role in roles.items() if role in PRIMARY_ROLES}
    indegree = {node_id: 0 for node_id in member_ids}
    adjacency: dict[int, set[int]] = {node_id: set() for node_id in member_ids}
    for source_id, target_id in observed_edges:
        adjacency[source_id].add(target_id)
        indegree[target_id] += 1

    role_conflicts = [edge for edge in observed_edges if edge[1] in primary_ids]
    ambiguous_parents = sorted(node_id for node_id, count in indegree.items() if count > 1)
    cycle = _has_cycle(member_ids, observed_edges)
    reachable: set[int] = set()
    if len(primary_ids) == 1:
        pending = list(primary_ids)
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            pending.extend(adjacency[node_id] - reachable)

    ambiguous = bool(role_conflicts or ambiguous_parents or cycle)
    if ambiguous:
        findings.append(_finding(
            "HA_TOPOLOGY_AMBIGUOUS", "warning", "复制拓扑方向存在歧义",
            "关系中存在指向主节点、循环或多上游，不能形成唯一可验证的切换路径。",
            evidence={
                "role_conflicts": sorted(role_conflicts),
                "multiple_upstream_nodes": ambiguous_parents,
                "cycle": cycle,
            },
            recommendation="依据数据库原生复制元数据修正方向、循环和重复上游关系。",
        ))

    complete = (
        len(primary_ids) == 1
        and len(observed_edges) == len(member_ids) - 1
        and indegree[next(iter(primary_ids))] == 0
        and all(indegree[node_id] == 1 for node_id in member_ids - primary_ids)
        and reachable == member_ids
        and not ambiguous
        and not scope_conflicts
        and not unverified_endpoints
        and not unverified_relations
    )
    if not complete:
        findings.append(_finding(
            "HA_TOPOLOGY_PARTIAL", "warning", "复制拓扑证据不完整",
            "已发现部分复制关系，但尚未覆盖所有节点形成唯一、连续、已验证的拓扑。",
            evidence={
                "member_count": len(member_ids),
                "observed_relation_count": len(observed_edges),
                "reachable_from_primary": len(reachable),
                "primary_count": len(primary_ids),
            },
            recommendation="补采缺失关系并消除断链、反向关系或作用域冲突。",
        ))
        return "partial", findings
    return "verified", findings


def _mgr_membership_assessment(
    db: Session,
    cluster_row: AssetCluster,
    member_facts: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """MGR is a membership topology; it must not be forced into replica arrows."""
    findings: list[dict[str, Any]] = []
    member_rows = {
        row.id: row
        for row in db.query(AssetInstance).filter(AssetInstance.cluster_id == cluster_row.id).all()
        if row.status not in {"merged", "inactive"}
    }
    member_ids = set(member_rows)
    unverified_endpoints = [
        item for item in member_facts
        if not item.get("verified") or not item.get("endpoint")
    ]
    if unverified_endpoints:
        findings.append(_finding(
            "HA_TOPOLOGY_ENDPOINT_UNVERIFIED", "warning", "拓扑节点端点身份未完整验证",
            f"{len(unverified_endpoints)}/{len(member_facts)} 个 MGR 成员缺少已验证的数据库端点身份。",
            evidence={"members": [item.get("name") or item.get("id") for item in unverified_endpoints]},
            recommendation="补齐成员端点并完成数据库身份核验后，再生成切换方案。",
        ))

    rows = db.query(AssetRelation).filter(
        AssetRelation.status == "active",
        AssetRelation.relation_type == "member_of",
    ).all()
    observed_members: set[int] = set()
    manual_members: set[int] = set()
    scope_conflicts: list[int] = []
    for relation in rows:
        if relation.source_id not in member_ids and relation.target_id != cluster_row.id:
            continue
        source = member_rows.get(relation.source_id)
        if (
            relation.source_type != "instance"
            or relation.target_type != "cluster"
            or source is None
            or relation.target_id != cluster_row.id
            or _topology_scope(source)[1:] != (
                cluster_row.tenant_id,
                cluster_row.organization_id,
                str(cluster_row.db_type or "").casefold(),
            )
        ):
            scope_conflicts.append(relation.id)
            continue
        if relation.discovery_source == "manual" or manual_relation(relation.attributes):
            manual_members.add(source.id)
        else:
            observed_members.add(source.id)

    if scope_conflicts:
        findings.append(_finding(
            "HA_TOPOLOGY_SCOPE_CONFLICT", "warning", "MGR 成员关系超出当前集群作用域",
            "存在指向其他集群、租户、组织或数据库类型的成员关系。",
            evidence={"relation_ids": scope_conflicts},
            recommendation="修正成员关系，使所有端点归属于当前 MGR 集群。",
        ))
    if manual_members - observed_members:
        findings.append(_finding(
            "HA_TOPOLOGY_MANUAL_ONLY", "warning", "手工成员关系不能替代实时成员证据",
            "部分 MGR 成员仅由手工关系登记，不能据此确认法定人数和切换就绪。",
            evidence={"member_ids": sorted(manual_members - observed_members)},
            recommendation="用 Group Replication 原生成员信息完成自动验证。",
        ))

    if not observed_members:
        findings.append(_finding(
            "HA_TOPOLOGY_MISSING", "warning", "MGR 成员拓扑尚未验证",
            "没有可用于 HA 判断的 active 自动成员关系。",
            evidence={"member_count": len(member_ids)},
            recommendation="采集 Group Replication 成员信息并持久化 member_of 关系。",
        ))
        return "unknown", findings
    complete = (
        observed_members == member_ids
        and not scope_conflicts
        and not unverified_endpoints
    )
    if not complete:
        findings.append(_finding(
            "HA_TOPOLOGY_PARTIAL", "warning", "MGR 成员拓扑证据不完整",
            "已验证成员关系未覆盖全部节点，不能确认完整成员集和法定人数边界。",
            evidence={
                "member_count": len(member_ids),
                "verified_member_count": len(observed_members),
            },
            recommendation="补采缺失成员并重新验证所有成员端点。",
        ))
        return "partial", findings
    return "verified", findings


def _action_readiness(
    cluster: dict[str, Any],
    members: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    architecture = ARCHITECTURE_ALIASES.get(cluster.get("architecture"), cluster.get("architecture"))
    db_type = cluster.get("db_type")
    primary = next((item for item in members if item.get("role") in PRIMARY_ROLES), None)
    native_instance_id = primary.get("native_instance_id") if primary else None
    critical_blockers = [item["title"] for item in findings if item["severity"] == "critical"]
    operational_blockers = [
        item["title"]
        for item in findings
        if item["code"] in PLAN_BLOCKING_CODES
    ]
    blockers = list(dict.fromkeys([*critical_blockers, *operational_blockers]))
    plan_endpoint = None
    plan_name = None
    if native_instance_id and db_type == "Oracle" and architecture == "adg":
        plan_endpoint = f"/oracle/{native_instance_id}/dataguard/switchover-check-plan"
        plan_name = "Data Guard 切换前检查"
    elif native_instance_id and db_type == "PostgreSQL" and architecture == "streaming_replication":
        plan_endpoint = f"/operations/postgresql/{native_instance_id}/ha/switchover-check-plan"
        plan_name = "PostgreSQL 主备切换检查"
    elif native_instance_id and db_type == "SQLServer" and architecture == "alwayson":
        plan_endpoint = f"/operations/sqlserver/{native_instance_id}/ha/failover-check-plan"
        plan_name = "Always On 故障转移检查"

    applicable = architecture in REPLICATED_ARCHITECTURES
    if not applicable:
        scope_reason = (
            "单机架构不适用高可用切换"
            if architecture == "single"
            else "当前架构尚未提供自动切换检查方案"
        )
        blockers = list(dict.fromkeys([*blockers, scope_reason]))
    return {
        "mode": "read_only_plan",
        "applicable": applicable,
        "ready_for_plan": applicable and not blockers and bool(native_instance_id),
        "blockers": blockers,
        "primary_instance_id": primary.get("id") if primary else None,
        "native_instance_id": native_instance_id,
        "plan_name": plan_name,
        "plan_endpoint": plan_endpoint,
        "supported_actions": [
            {"key": "switchover_check", "label": "计划切换检查", "execution": "plan_only"},
            {"key": "failover_check", "label": "故障切换检查", "execution": "plan_only"},
            {"key": "node_lifecycle", "label": "节点新增/重建/替换/退役", "execution": "manual_runbook"},
            {"key": "drill", "label": "灾备演练", "execution": "manual_runbook"},
        ] if applicable else [],
        "safety_note": "当前仅生成检查方案和演练依据，不执行主备切换、故障转移或节点变更。",
    }


def assess_cluster(db: Session, cluster_id: int) -> dict[str, Any] | None:
    cluster_row = db.get(AssetCluster, cluster_id)
    cluster = normalized_service.get_cluster(db, cluster_id)
    if not cluster_row or not cluster:
        return None

    members = cluster.get("instances") or []
    architecture = ARCHITECTURE_ALIASES.get(cluster.get("architecture"), cluster.get("architecture"))
    cluster_attributes = cluster.get("attributes") or {}
    now = datetime.now()
    findings: list[dict[str, Any]] = []
    member_facts: list[dict[str, Any]] = []
    verified_count = 0
    reachable_count = 0
    known_role_count = 0
    lag_values: list[float] = []
    log_gap_count = 0

    for member in members:
        attributes = member.get("attributes") or {}
        discovery_status = str(attributes.get("discovery_status") or "").lower()
        verified = discovery_status == "success" or bool(attributes.get("architecture_verified"))
        connection_ok = str(attributes.get("connection_test_status") or "").lower() == "success"
        unreachable = discovery_status == "failed" or member.get("status") in {"critical", "unhealthy", "disabled"}
        reachable = not unreachable and (verified or connection_ok or member.get("status") == "healthy")
        lag = _member_lag(attributes)
        gap = _member_gap(attributes)
        last_seen = _datetime(member.get("last_seen_at"))
        stale = not last_seen or last_seen < now - timedelta(hours=STALE_AFTER_HOURS)
        if verified:
            verified_count += 1
        if reachable:
            reachable_count += 1
        if member.get("role") not in {None, "", "unknown"}:
            known_role_count += 1
        if lag is not None:
            lag_values.append(lag)
        log_gap_count += gap
        member_facts.append({
            "id": member.get("id"),
            "name": member.get("name"),
            "endpoint": member.get("endpoint"),
            "role": member.get("role"),
            "status": member.get("status"),
            "verified": verified,
            "reachable": reachable,
            "stale": stale,
            "lag_seconds": lag,
            "log_gap_count": gap,
            "native_instance_id": member.get("native_instance_id"),
            "last_seen_at": last_seen.isoformat() if last_seen else None,
            "discovery_error": attributes.get("discovery_error"),
        })

    member_count = len(members)
    roles = [item.get("role") for item in members]
    primary_count = sum(1 for role in roles if role in PRIMARY_ROLES)
    secondary_count = sum(1 for role in roles if role in SECONDARY_ROLES)
    candidate_count = sum(1 for role in roles if role in CANDIDATE_ROLES)
    unknown_role_count = member_count - known_role_count
    max_lag_seconds = max(lag_values) if lag_values else None
    endpoint_facts = _endpoint_facts(cluster, members)
    split_brain_risk = False
    quorum_risk = False

    if not members:
        findings.append(_finding(
            "HA_NO_MEMBER", "critical", "集群没有有效实例",
            "当前集群没有可用于诊断的实例，拓扑不可用。",
            recommendation="先执行自动识别，或人工校正实例的集群归属。",
        ))

    db_types = sorted({item.get("db_type") for item in members if item.get("db_type")})
    if len(db_types) > 1 or (db_types and db_types != [cluster.get("db_type")]):
        findings.append(_finding(
            "HA_DB_TYPE_CONFLICT", "critical", "集群内数据库类型不一致",
            "同一集群出现不同数据库类型，属于资产归属冲突。",
            evidence={"cluster_db_type": cluster.get("db_type"), "member_db_types": db_types},
            recommendation="在资产中心拆分错误集群或修改实例归属。",
        ))

    endpoints = [item.get("endpoint") for item in members if item.get("endpoint")]
    duplicate_endpoints = sorted({item for item in endpoints if endpoints.count(item) > 1})
    if duplicate_endpoints:
        findings.append(_finding(
            "HA_DUPLICATE_ENDPOINT", "warning", "存在重复实例端点",
            "同一 IP:Port 被多个资产实例占用，可能是重复发现或错误合并。",
            evidence={"endpoints": duplicate_endpoints},
            recommendation="合并重复实例并保留连接配置正确的资产。",
        ))

    if member_count and verified_count == 0:
        findings.append(_finding(
            "HA_DATA_UNVERIFIED", "warning", "高可用事实尚未实时验证",
            "当前结论仅来自配置或历史元数据，不能作为切换依据。",
            evidence={"verified": verified_count, "members": member_count},
            recommendation="先测试数据库连接，再执行自动识别。",
        ))
    elif verified_count < member_count:
        findings.append(_finding(
            "HA_PARTIAL_VERIFICATION", "warning", "部分节点未完成实时验证",
            f"{member_count - verified_count} 个节点没有本次实时探测证据。",
            evidence={"verified": verified_count, "members": member_count},
            recommendation="修复失败节点连接并重新执行自动识别。",
        ))

    unreachable_members = [item for item in member_facts if not item["reachable"]]
    if unreachable_members:
        severity = "critical" if len(unreachable_members) == member_count else "warning"
        findings.append(_finding(
            "HA_MEMBER_UNREACHABLE", severity, "存在不可达或未确认节点",
            f"{len(unreachable_members)}/{member_count} 个节点当前不可达或状态未知。",
            evidence={"members": [item["endpoint"] for item in unreachable_members]},
            recommendation="检查数据库监听、网络、凭据和实例运行状态。",
        ))

    stale_members = [item for item in member_facts if item["stale"]]
    if stale_members:
        findings.append(_finding(
            "HA_DATA_STALE", "warning", "高可用数据已过期",
            f"{len(stale_members)} 个节点超过 {STALE_AFTER_HOURS} 小时未更新。",
            evidence={"members": [item["endpoint"] for item in stale_members]},
            recommendation="执行自动识别生成新的实时快照。",
        ))

    if architecture in REPLICATED_ARCHITECTURES:
        mgr_multi_primary = _mgr_is_multi_primary(cluster_attributes) if architecture == "mgr" else False
        if primary_count > 1 and architecture != "mgr":
            split_brain_risk = True
            findings.append(_finding(
                "HA_MULTIPLE_PRIMARY", "critical", "检测到多个主节点",
                "单主复制架构同时出现多个主角色，存在脑裂或角色识别错误风险。",
                evidence={"primary_count": primary_count},
                recommendation="禁止切换和写入扩散，立即核对各节点只读状态及仲裁信息。",
            ))
        elif primary_count > 1 and architecture == "mgr" and mgr_multi_primary is not True:
            severity = "critical" if mgr_multi_primary is False else "warning"
            split_brain_risk = severity == "critical"
            findings.append(_finding(
                "HA_MGR_PRIMARY_MODE", severity, "MGR 主节点模式需要确认",
                "检测到多个主角色，但尚未确认集群是否运行在 Multi-Primary 模式。",
                evidence={"primary_count": primary_count, "multi_primary": mgr_multi_primary},
                recommendation="读取 group_replication_single_primary_mode 并校正拓扑。",
            ))
        if primary_count == 0 and known_role_count:
            findings.append(_finding(
                "HA_PRIMARY_MISSING", "critical", "未识别到主节点",
                "复制架构已有角色证据，但没有主节点，服务可用性和切换路径不明确。",
                evidence={"roles": roles},
                recommendation="核对选主状态、只读标志和复制管理器健康。",
            ))
        if secondary_count + candidate_count == 0 and member_count > 1:
            findings.append(_finding(
                "HA_SECONDARY_MISSING", "warning", "未识别到可用副本",
                "集群存在多个节点，但没有识别到副本、备库或候选节点。",
                evidence={"roles": roles},
                recommendation="校正节点角色并检查复制线程或同步状态。",
            ))
        if unknown_role_count:
            findings.append(_finding(
                "HA_ROLE_UNKNOWN", "warning", "节点角色未完整识别",
                f"{unknown_role_count}/{member_count} 个节点角色未知。",
                evidence={"roles": roles},
                recommendation="完成所有节点的实时探测后再生成切换方案。",
            ))

    if architecture == "single" and member_count > 1:
        findings.append(_finding(
            "HA_SINGLE_WITH_MULTIPLE_MEMBERS", "warning", "单机架构包含多个实例",
            "资产模型标记为单机，但集群下存在多个实例。",
            evidence={"member_count": member_count},
            recommendation="重新识别架构，或拆分为独立单机集群。",
        ))

    if architecture == "mgr" and member_count:
        if member_count < 3:
            findings.append(_finding(
                "HA_MGR_MEMBER_COUNT", "warning", "MGR 节点数不足三节点",
                "两节点或单节点 MGR 无法提供稳定多数派容错。",
                evidence={"member_count": member_count},
                recommendation="规划至少三个投票成员，并验证故障后的法定人数。",
            ))
        if verified_count and reachable_count <= member_count // 2:
            quorum_risk = True
            findings.append(_finding(
                "HA_QUORUM_RISK", "critical", "MGR 法定人数不足",
                "当前可达成员未超过半数，集群可能无法完成选主或写入。",
                evidence={"reachable": reachable_count, "members": member_count},
                recommendation="先恢复多数派，禁止强制引导多个分区。",
            ))

    if architecture in DIRECTIONAL_REPLICATION_ARCHITECTURES:
        replication_state, topology_findings = _replication_topology_assessment(
            db, cluster_row, architecture, members, member_facts,
        )
        findings.extend(topology_findings)
    elif architecture == "mgr":
        replication_state, topology_findings = _mgr_membership_assessment(
            db, cluster_row, member_facts,
        )
        findings.extend(topology_findings)
    else:
        replication_state = "not_required"

    composite_rac_dg = architecture == "rac" and any(
        bool(((item.get("attributes") or {}).get("architecture_evidence") or {}).get("has_dataguard"))
        for item in members
    )
    if composite_rac_dg:
        replication_state = "unknown"
        findings.append(_finding(
            "HA_COMPOSITE_TOPOLOGY_UNVERIFIED", "warning", "RAC + Data Guard 复合拓扑尚未验收",
            "当前仅确认 RAC 成员事实，尚未建立并验证跨库 Data Guard 复制图，不能视为完整 HA 拓扑。",
            evidence={"rac_members": member_count, "has_dataguard": True},
            recommendation="分别验证 RAC 成员关系和 Data Guard 数据库级方向后，再评估复合切换路径。",
        ))

    rpo_target = _float(cluster_attributes.get("rpo_target_seconds")) or LAG_WARNING_SECONDS
    lag_critical = max(LAG_CRITICAL_SECONDS, rpo_target * 3)
    if max_lag_seconds is not None and max_lag_seconds > lag_critical:
        findings.append(_finding(
            "HA_REPLICATION_LAG_CRITICAL", "critical", "复制延迟严重超标",
            f"最大复制延迟 {max_lag_seconds:.1f} 秒，超过严重阈值 {lag_critical:.1f} 秒。",
            evidence={"max_lag_seconds": max_lag_seconds, "critical_seconds": lag_critical},
            recommendation="检查网络、日志传输/应用线程、存储 IO 和大事务。",
        ))
    elif max_lag_seconds is not None and max_lag_seconds > rpo_target:
        findings.append(_finding(
            "HA_REPLICATION_LAG_WARNING", "warning", "复制延迟超过 RPO 目标",
            f"最大复制延迟 {max_lag_seconds:.1f} 秒，超过目标 {rpo_target:.1f} 秒。",
            evidence={"max_lag_seconds": max_lag_seconds, "rpo_target_seconds": rpo_target},
            recommendation="在延迟恢复前禁止计划切换。",
        ))

    if log_gap_count:
        findings.append(_finding(
            "HA_LOG_GAP", "critical", "检测到复制日志缺口",
            f"当前存在 {log_gap_count} 个日志缺口记录。",
            evidence={"log_gap_count": log_gap_count},
            recommendation="补齐归档/WAL/日志链并验证连续性后再进行切换。",
        ))

    required_endpoint = {
        "rac": ("vips", "listeners", "services"),
        "adg": ("services",),
        "alwayson": ("listeners",),
        "mgr": ("vips",),
    }.get(architecture, ())
    missing_endpoint_facts = [key for key in required_endpoint if not endpoint_facts[key]]
    if missing_endpoint_facts:
        findings.append(_finding(
            "HA_ENDPOINT_FACTS_MISSING", "info", "访问入口信息不完整",
            "尚未采集完整的 VIP、Listener 或 Service 信息。",
            evidence={"missing": missing_endpoint_facts},
            recommendation="补采服务入口，确保切换后应用连接路径可验证。",
        ))

    critical_count = sum(1 for item in findings if item["severity"] == "critical")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")
    info_count = sum(1 for item in findings if item["severity"] == "info")
    if not member_count:
        health_status = "unknown"
    elif critical_count:
        health_status = "critical"
    elif warning_count:
        health_status = "warning"
    else:
        health_status = "healthy"
    risk_score = min(100, critical_count * 35 + warning_count * 12 + info_count * 2)
    known_architecture = architecture not in {None, "", "unknown"}
    verified_ratio = verified_count / member_count if member_count else 0
    role_ratio = known_role_count / member_count if member_count else 0
    endpoint_ratio = 1.0 if not required_endpoint else (
        (len(required_endpoint) - len(missing_endpoint_facts)) / len(required_endpoint)
    )
    confidence_score = round(
        verified_ratio * 55
        + role_ratio * 25
        + (10 if known_architecture else 0)
        + endpoint_ratio * 10
    ) if member_count else 0

    result = {
        "cluster": {
            "id": cluster.get("id"),
            "name": cluster.get("name"),
            "db_type": cluster.get("db_type"),
            "architecture": architecture,
            "location": " / ".join(filter(None, [cluster.get("region"), cluster.get("site"), cluster.get("zone")])),
            "environment": cluster.get("environment"),
        },
        "summary": {
            "health_status": health_status,
            "risk_score": risk_score,
            "confidence_score": confidence_score,
            "member_count": member_count,
            "verified_count": verified_count,
            "reachable_count": reachable_count,
            "primary_count": primary_count,
            "secondary_count": secondary_count,
            "candidate_count": candidate_count,
            "unknown_role_count": unknown_role_count,
            "max_lag_seconds": max_lag_seconds,
            "log_gap_count": log_gap_count,
            "split_brain_risk": split_brain_risk,
            "quorum_risk": quorum_risk,
            "replication_state": replication_state,
        },
        "thresholds": {
            "rpo_target_seconds": rpo_target,
            "lag_critical_seconds": lag_critical,
            "stale_after_hours": STALE_AFTER_HOURS,
        },
        "findings": findings,
        "members": member_facts,
        "endpoint_facts": endpoint_facts,
        "action_readiness": _action_readiness(cluster, members, findings),
        "generated_at": now,
    }
    return result


def serialize_snapshot(row: AssetClusterHealthSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "cluster_id": row.cluster_id,
        "db_type": row.db_type,
        "architecture": row.architecture,
        "health_status": row.health_status,
        "source": row.source,
        "member_count": row.member_count,
        "verified_count": row.verified_count,
        "reachable_count": row.reachable_count,
        "primary_count": row.primary_count,
        "secondary_count": row.secondary_count,
        "candidate_count": row.candidate_count,
        "max_lag_seconds": row.max_lag_seconds,
        "log_gap_count": row.log_gap_count,
        "split_brain_risk": bool(row.split_brain_risk),
        "quorum_risk": bool(row.quorum_risk),
        "risk_score": row.risk_score,
        "confidence_score": row.confidence_score,
        "findings": row.findings or [],
        "member_facts": row.member_facts or [],
        "endpoint_facts": row.endpoint_facts or {},
        "collected_at": row.collected_at,
    }


def _snapshot_from_assessment(
    cluster_id: int,
    assessment: dict[str, Any],
    source: str,
) -> AssetClusterHealthSnapshot:
    cluster = assessment["cluster"]
    summary = assessment["summary"]
    return AssetClusterHealthSnapshot(
        cluster_id=cluster_id,
        db_type=cluster["db_type"],
        architecture=cluster["architecture"],
        health_status=summary["health_status"],
        source=source,
        member_count=summary["member_count"],
        verified_count=summary["verified_count"],
        reachable_count=summary["reachable_count"],
        primary_count=summary["primary_count"],
        secondary_count=summary["secondary_count"],
        candidate_count=summary["candidate_count"],
        max_lag_seconds=summary["max_lag_seconds"],
        log_gap_count=summary["log_gap_count"],
        split_brain_risk=int(summary["split_brain_risk"]),
        quorum_risk=int(summary["quorum_risk"]),
        risk_score=summary["risk_score"],
        confidence_score=summary["confidence_score"],
        findings=assessment["findings"],
        member_facts=assessment["members"],
        endpoint_facts=assessment["endpoint_facts"],
    )


def capture_snapshot(db: Session, cluster_id: int, source: str = "manual_refresh") -> dict[str, Any] | None:
    assessment = assess_cluster(db, cluster_id)
    if not assessment:
        return None
    row = _snapshot_from_assessment(cluster_id, assessment, source)
    db.add(row)
    db.commit()
    db.refresh(row)
    assessment["snapshot"] = serialize_snapshot(row)
    return assessment


def capture_all_snapshots(db: Session, source: str = "live_discovery") -> list[dict[str, Any]]:
    pending: list[tuple[dict[str, Any], AssetClusterHealthSnapshot]] = []
    for cluster in normalized_service.list_clusters(db):
        assessment = assess_cluster(db, cluster["id"])
        if assessment:
            row = _snapshot_from_assessment(cluster["id"], assessment, source)
            db.add(row)
            pending.append((assessment, row))
    db.commit()
    items: list[dict[str, Any]] = []
    for _assessment, row in pending:
        db.refresh(row)
        items.append(serialize_snapshot(row))
    return items


def list_snapshots(db: Session, cluster_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        db.query(AssetClusterHealthSnapshot)
        .filter(AssetClusterHealthSnapshot.cluster_id == cluster_id)
        .order_by(AssetClusterHealthSnapshot.collected_at.desc(), AssetClusterHealthSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    return [serialize_snapshot(row) for row in rows]
