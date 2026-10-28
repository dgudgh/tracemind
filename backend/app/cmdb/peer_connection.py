"""Derive peer addressing and verify identities without accepting peer credentials."""
from typing import Any

from app.cmdb.discovery import normalize_host_address


def oracle_peer_options(source: Any, member: dict) -> dict:
    if str(source.db_type).lower() != "oracle":
        return {}
    connect_type = member.get("oracle_connect_type")
    if connect_type == "service_name" and member.get("service_name"):
        return {"oracle_connect_type": "service_name", "service_name": member["service_name"], "sid": None}
    if connect_type == "sid" and member.get("sid"):
        return {"oracle_connect_type": "sid", "sid": member["sid"], "service_name": None}
    if source.oracle_connect_type == "sid" and member.get("instance_name"):
        return {"oracle_connect_type": "sid", "sid": member["instance_name"], "service_name": None}
    return {}


def _identity(value: Any) -> str:
    return str(value or "").strip().casefold()


def verify_peer_identity(engine: str, member: dict, actual: dict, source_profile: dict) -> None:
    """Reject missing or contradictory identities when the seed supplied one."""
    checks = []
    if engine == "mysql":
        # ``SHOW REPLICAS`` / ``SHOW SLAVE HOSTS`` may expose both fields, but
        # server_uuid is the stronger identity and must not silently fall back
        # to server_id when the peer omits or contradicts it.  When the seed
        # supplied neither identity, endpoint authentication remains useful to
        # an explicitly configured connection; the discovery orchestrator keeps
        # an unconfigured peer unverified instead of inventing an identity here.
        expected_uuid = member.get("server_uuid")
        if expected_uuid not in (None, ""):
            checks = [("server_uuid", expected_uuid, actual.get("server_uuid"))]
        elif member.get("server_id") not in (None, ""):
            checks = [("server_id", member.get("server_id"), actual.get("server_id"))]
    elif engine == "oracle":
        checks = [("db_unique_name", member.get("db_unique_name"), actual.get("db_unique_name")),
                  ("instance_name", member.get("instance_name"), actual.get("instance_name"))]
    elif engine == "postgresql":
        checks = [("system_identifier", source_profile.get("system_identifier"), actual.get("system_identifier"))]
        if not source_profile.get("system_identifier"):
            if source_profile.get("in_recovery") is True and member.get("role_type") == "primary":
                expected_hosts = {normalize_host_address(member.get("host")), normalize_host_address(member.get("reported_host"))} - {""}
                observed_host = normalize_host_address(source_profile.get("upstream_host"))
                expected_ports = {str(member.get("port"))}
                observed_port = str(source_profile.get("upstream_port") or "")
                if actual.get("in_recovery") is not False:
                    raise ValueError("未能核实 PostgreSQL 上游主节点身份")
            else:
                expected_hosts = {normalize_host_address(source_profile.get(key))
                                  for key in ("connection_host", "host_ip")} - {""}
                expected_ports = {str(source_profile[key]) for key in ("connection_port", "port")
                                  if source_profile.get(key)}
                observed_host = normalize_host_address(actual.get("upstream_host"))
                observed_port = str(actual.get("upstream_port") or "")
                if actual.get("in_recovery") is not True:
                    raise ValueError("未能核实 PostgreSQL 物理备库身份")
            if not observed_host or observed_host not in expected_hosts or observed_port not in expected_ports:
                raise ValueError("PostgreSQL 集群标识不可用，且 WAL 上游端点缺失或不符，未确认该成员")
    elif engine == "sqlserver":
        expected_replica = member.get("replica_id")
        expected_group = member.get("availability_group_id") or source_profile.get("availability_group_id")
        checks = [("replica_id", expected_replica, actual.get("replica_id")),
                  ("availability_group_id", expected_group, actual.get("availability_group_id"))]
        if not expected_replica:
            checks.append(("server_name", member.get("server_name"), actual.get("server_name")))
        if member.get("instance_name"):
            checks.append(("instance_name", member["instance_name"], actual.get("instance_name")))
    for field, expected, observed in checks:
        if expected not in (None, "") and _identity(expected) != _identity(observed):
            raise ValueError(f"节点 {field} 与种子发现身份不符或未能验证，未确认该成员")
