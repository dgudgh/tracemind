import logging
import re
from contextlib import contextmanager

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.sqlserver.connection")


def _valid_tcp_port(value) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _replica_server_identity(value: object) -> tuple[str, str | None, int | None]:
    """Split SQL Server's ``host\\instance,port`` identity safely.

    SQL Server uses a comma (not a colon) for an explicit TDS port. A named
    instance without a comma does not publish its TDS port, so callers must
    treat any configured seed port as a candidate until that instance is
    contacted and its identity is checked.
    """
    server_name = str(value or "").strip()
    if server_name.lower().startswith("tcp:"):
        server_name = server_name[4:].strip()

    explicit_port = None
    identity = server_name
    before_port, separator, port_text = server_name.rpartition(",")
    if separator:
        parsed_port = _valid_tcp_port(port_text.strip())
        if parsed_port is not None:
            explicit_port = parsed_port
            identity = before_port.strip()

    host, separator, instance_name = identity.partition("\\")
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    return host, instance_name.strip() if separator and instance_name.strip() else None, explicit_port


def _hadr_endpoint_identity(value: object) -> tuple[str, int | None]:
    """Return HADR transport metadata; its port is never a TDS login port."""
    endpoint = str(value or "").strip()
    match = re.match(r"(?i)^tcp://(?:\[([^]]+)\]|([^:/]+))(?::(\d+))?/?$", endpoint)
    if not match:
        return "", None
    return str(match.group(1) or match.group(2) or "").strip(), _valid_tcp_port(match.group(3))


def _availability_member_health(role_desc: str, connected: str, sync_health: str) -> str:
    if role_desc not in {"PRIMARY", "SECONDARY"} or not connected or not sync_health:
        return "unknown"
    if connected == "CONNECTED" and sync_health == "HEALTHY":
        return "healthy"
    return "warning"


def _encryption_level(value: str | None) -> str:
    normalized = str(value or "request").strip().lower()
    if normalized in {"yes", "true", "on", "require", "required", "mandatory"}:
        return "require"
    if normalized in {"no", "false", "off", "disable", "disabled"}:
        return "off"
    if normalized in {"request", "prefer", "preferred", "optional", "default"}:
        return "request"
    raise ValueError(f"Unsupported SQL Server encryption setting: {value}")


@contextmanager
def sqlserver_connection(
    instance: DBInstance,
    *,
    query_timeout_seconds: int = 10,
    read_only: bool = False,
):
    try:
        import pymssql
    except ImportError as exc:
        raise RuntimeError("pymssql driver is not installed") from exc

    if getattr(instance, "certificate_profile_id", None):
        # pymssql exposes an encryption level only - no CA, no client certificate,
        # no hostname pinning. Accepting a profile would publish a trust promise
        # that is never enforced, which is worse than refusing: the operator would
        # believe the channel is pinned while it is merely encrypted.
        raise ValueError(
            "SQL Server 驱动不支持证书材质（CA/客户端证书/主机名校验）；"
            "请移除该实例的证书配置，或改用 encryption 级别加密"
        )

    logger.debug("SQL Server 连接 | host=%s:%s | user=%s | db=%s", instance.host, instance.port, instance.username, instance.database_name)
    conn = pymssql.connect(
        server=instance.host,
        port=instance.port,
        user=instance.username,
        password=decrypt_password(instance.password_cipher),
        database=instance.database_name or "master",
        login_timeout=5,
        timeout=max(1, int(query_timeout_seconds)),
        as_dict=True,
        encryption=_encryption_level(instance.encrypt),
        read_only=bool(read_only),
    )
    try:
        yield conn
    finally:
        conn.close()


def test_sqlserver_connection(instance: DBInstance) -> dict:
    with sqlserver_connection(instance) as conn:
        with conn.cursor(as_dict=True) as cursor:
            warnings = []
            try:
                cursor.execute(
                    "SELECT @@VERSION AS version, DB_NAME() AS database_name, "
                    "SUSER_SNAME() AS current_login, "
                    "CONVERT(NVARCHAR(256), SERVERPROPERTY('ServerName')) AS server_name, "
                    "CONVERT(NVARCHAR(256), SERVERPROPERTY('MachineName')) AS machine_name, "
                    "CONVERT(NVARCHAR(256), SERVERPROPERTY('InstanceName')) AS instance_name, "
                    "TRY_CONVERT(INT, CONNECTIONPROPERTY('local_tcp_port')) AS local_tcp_port"
                )
                row = cursor.fetchone() or {}
            except Exception as exc:
                warnings.append(f"SQL Server 本地身份或监听端口识别失败，已降级采集：{exc}")
                cursor.execute(
                    "SELECT @@VERSION AS version, DB_NAME() AS database_name, "
                    "SUSER_SNAME() AS current_login"
                )
                row = cursor.fetchone() or {}

            local_server_name = str(row.get("server_name") or "").strip()
            machine_name = str(row.get("machine_name") or "").strip()
            local_instance_name = str(row.get("instance_name") or "").strip() or None
            actual_listen_port = _valid_tcp_port(row.get("local_tcp_port"))
            local_port = actual_listen_port or int(instance.port)
            local_port_inferred = actual_listen_port is None
            if local_port_inferred:
                warnings.append(
                    "SQL Server 未返回当前连接的实际 TDS 监听端口；配置端口仅作为候选，需在成员身份核对后使用"
                )
            architecture = {
                "architecture_type": "standalone",
                "role_type": "primary",
                "hadr_enabled": False,
                "availability_group_role": None,
            }
            try:
                cursor.execute("SELECT CAST(SERVERPROPERTY('IsHadrEnabled') AS INT) AS is_hadr_enabled")
                hadr_row = cursor.fetchone() or {}
                hadr_enabled = bool(int(hadr_row.get("is_hadr_enabled") or 0))
                role_desc = None
                group_id = None
                group_name = None
                local_replica_id = None
                upstream_server_name = None
                availability_group_ambiguous = False
                members = []
                if hadr_enabled:
                    try:
                        cursor.execute(
                            "SELECT TOP (1) ars.role_desc, "
                            "CONVERT(VARCHAR(36), ar.replica_id) AS replica_id, "
                            "CONVERT(VARCHAR(36), ag.group_id) AS group_id, "
                            "ag.name AS group_name, "
                            "(SELECT COUNT(DISTINCT group_id) FROM sys.dm_hadr_availability_replica_states "
                            "WHERE is_local = 1) AS local_group_count "
                            "FROM sys.dm_hadr_availability_replica_states ars "
                            "JOIN sys.availability_replicas ar "
                            "ON ar.replica_id = ars.replica_id AND ar.group_id = ars.group_id "
                            "JOIN sys.availability_groups ag ON ag.group_id = ars.group_id "
                            "WHERE ars.is_local = 1 "
                            "ORDER BY ag.group_id"
                        )
                        role_row = cursor.fetchone() or {}
                        role_desc = str(role_row.get("role_desc") or "").upper() or None
                        local_replica_id = str(role_row.get("replica_id") or "").strip() or None
                        group_id = str(role_row.get("group_id") or "").strip() or None
                        group_name = str(role_row.get("group_name") or "").strip() or None
                        if int(role_row.get("local_group_count") or 1) > 1:
                            availability_group_ambiguous = True
                            role_desc = group_id = group_name = local_replica_id = None
                            warnings.append("SQL Server 实例属于多个可用性组，目标组尚未明确，未任取一个组建立拓扑")
                    except Exception as exc:
                        warnings.append(f"SQL Server Always On 本地角色识别失败：{exc}")
                    if not group_id:
                        warnings.append("SQL Server Always On 未取得本地可用性组标识，未枚举未经成员归属核对的副本")
                    else:
                        if role_desc == "SECONDARY":
                            try:
                                cursor.execute(
                                    "SELECT CONVERT(VARCHAR(36), group_id) AS availability_group_id, primary_replica "
                                    "FROM sys.dm_hadr_availability_group_states "
                                    "WHERE CONVERT(VARCHAR(36), group_id) = %s", (group_id,),
                                )
                                primary_names = {
                                    str(item.get("primary_replica") or "").strip().casefold(): str(item.get("primary_replica") or "").strip()
                                    for item in list(cursor.fetchall() or [])
                                    if str(item.get("availability_group_id") or "").casefold() == group_id.casefold()
                                    and str(item.get("primary_replica") or "").strip()
                                }
                                if len(primary_names) == 1:
                                    candidate = next(iter(primary_names.values()))
                                    if candidate.casefold() != local_server_name.casefold():
                                        upstream_server_name = candidate
                            except Exception:
                                warnings.append("SQL Server 可用性组主副本方向未采集，复制关系保持未确认")
                        try:
                            cursor.execute(
                                "SELECT CONVERT(VARCHAR(36), ar.replica_id) AS replica_id, "
                                "CONVERT(VARCHAR(36), ar.group_id) AS availability_group_id, "
                                "ar.replica_server_name, ar.endpoint_url, ars.is_local, ars.role_desc, "
                                "ars.connected_state_desc, ars.synchronization_health_desc "
                                "FROM sys.availability_replicas ar "
                                "LEFT JOIN sys.dm_hadr_availability_replica_states ars "
                                "ON ars.replica_id = ar.replica_id AND ars.group_id = ar.group_id "
                                "WHERE CONVERT(VARCHAR(36), ar.group_id) = %s "
                                "ORDER BY ar.replica_server_name",
                                (group_id,),
                            )
                            for raw_replica in list(cursor.fetchall() or []):
                                replica = dict(raw_replica)
                                replica_id = str(replica.get("replica_id") or "").strip() or None
                                replica_group_id = str(replica.get("availability_group_id") or "").strip() or None
                                if replica_group_id and replica_group_id.lower() != group_id.lower():
                                    warnings.append(
                                        f"SQL Server Always On 忽略不属于本地可用性组的成员：{replica_id or 'unknown'}"
                                    )
                                    continue
                                if bool(replica.get("is_local")) or (
                                    local_replica_id and replica_id and local_replica_id.lower() == replica_id.lower()
                                ):
                                    continue

                                server_name = str(replica.get("replica_server_name") or "").strip()
                                if not server_name or not replica_id:
                                    warnings.append("SQL Server Always On 忽略缺少 replica_id 或 server_name 的成员记录")
                                    continue
                                host, named_instance, explicit_tds_port = _replica_server_identity(server_name)
                                endpoint_url = str(replica.get("endpoint_url") or "").strip()
                                hadr_host, hadr_port = _hadr_endpoint_identity(endpoint_url)
                                host = host or hadr_host
                                if not host:
                                    warnings.append(f"SQL Server Always On 成员 {server_name} 未提供可用主机地址")
                                    continue

                                replica_role = str(replica.get("role_desc") or "").upper()
                                connected = str(replica.get("connected_state_desc") or "").upper()
                                sync_health = str(replica.get("synchronization_health_desc") or "").upper()
                                role_type = {
                                    "PRIMARY": "primary",
                                    "SECONDARY": "replica",
                                }.get(replica_role, "unknown")
                                port_inferred = explicit_tds_port is None
                                members.append({
                                    "member_key": replica_id.lower(),
                                    "name": server_name,
                                    "host": host,
                                    "reported_host": host,
                                    "server_name": server_name,
                                    "instance_name": named_instance,
                                    "port": explicit_tds_port or int(instance.port),
                                    "port_inferred": port_inferred,
                                    "port_source": "replica_server_name" if explicit_tds_port else "seed_candidate",
                                    "role_type": role_type,
                                    "health_status": _availability_member_health(
                                        replica_role,
                                        connected,
                                        sync_health,
                                    ),
                                    "member_state": connected or None,
                                    "synchronization_health": sync_health or None,
                                    "replica_id": replica_id,
                                    "availability_group_id": group_id,
                                    "endpoint_url": endpoint_url,
                                    "hadr_endpoint_host": hadr_host or None,
                                    "hadr_endpoint_port": hadr_port,
                                    "identity_verification_required": port_inferred or bool(named_instance),
                                })
                        except Exception as exc:
                            warnings.append(f"SQL Server Always On 成员识别失败：{exc}")
                role_type = "unknown"
                if role_desc == "PRIMARY" or not hadr_enabled:
                    role_type = "primary"
                elif role_desc == "SECONDARY":
                    role_type = "replica"
                architecture = {
                    "architecture_type": "always_on" if hadr_enabled else "standalone",
                    "role_type": role_type,
                    "topology_group": (
                        f"sqlserver:always_on:{str(group_id or group_name or instance.system_name or instance.host).lower()}"
                        if hadr_enabled
                        else f"sqlserver:standalone:{str(instance.host).lower()}:{instance.port}"
                    ),
                    "cluster_name": instance.system_name or group_name or f"{instance.instance_name} 可用性组",
                    "member_key": str(
                        local_replica_id
                        or local_server_name
                        or f"{instance.host}:{local_port}"
                    ).lower(),
                    "hostname": machine_name or instance.host,
                    "server_name": local_server_name or None,
                    "machine_name": machine_name or None,
                    "instance_name": local_instance_name,
                    "port": local_port,
                    "port_inferred": local_port_inferred,
                    "db_version": row.get("version"),
                    "hadr_enabled": hadr_enabled,
                    "availability_group_role": role_desc,
                    "replica_id": local_replica_id,
                    "availability_group_id": group_id,
                    "availability_group_name": group_name,
                    "availability_group_ambiguous": availability_group_ambiguous,
                    "members": members,
                }
                if upstream_server_name:
                    architecture["upstream_server_name"] = upstream_server_name
            except Exception as exc:
                warnings.append(f"SQL Server 架构状态识别失败：{exc}")
            for name, sql in {
                "sys.dm_exec_sessions": "SELECT COUNT(*) AS cnt FROM sys.dm_exec_sessions",
                "sys.databases": "SELECT COUNT(*) AS cnt FROM sys.databases",
            }.items():
                try:
                    cursor.execute(sql)
                    cursor.fetchone()
                except Exception as exc:
                    warnings.append(f"{name} 权限检查失败，巡检能力会降级：{exc}")
            return {
                "host": instance.host,
                "port": instance.port,
                "database": row.get("database_name") or instance.database_name,
                "current_user": row.get("current_login") or instance.username,
                "version": row.get("version"),
                "architecture": architecture,
                "capability": "full_inspection" if not warnings else "limited",
                "warnings": warnings,
            }
