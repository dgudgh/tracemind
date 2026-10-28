import ipaddress
import logging
from contextlib import contextmanager

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance
from app.services.connection_security import database_tls_material

logger = logging.getLogger("app.collectors.mysql.connection")


def _lower_row(row) -> dict:
    return {str(key).lower(): value for key, value in (row or {}).items()}


def _first_rows(cursor, statements: tuple[str, ...]) -> list[dict]:
    for sql in statements:
        try:
            cursor.execute(sql)
            rows = [_lower_row(row) for row in cursor.fetchall()]
            if rows:
                return rows
        except Exception:
            continue
    return []


def _reported_member_host(
    reported_host: object,
    member_server_id: object,
    local_server_id: object,
    connection_host: str,
) -> str:
    """Use server_id as an IP only when the local node proves that convention."""
    host = str(reported_host or "").strip()
    try:
        local_ip = ipaddress.ip_address(connection_host)
        encoded_local_ip = ipaddress.ip_address(int(str(local_server_id)))
        if local_ip == encoded_local_ip:
            return str(ipaddress.ip_address(int(str(member_server_id))))
    except (TypeError, ValueError):
        pass
    return host


def _member_key(server_uuid: object, server_id: object, host: object, port: object) -> str:
    return str(server_uuid or server_id or f"{host}:{port}").strip().lower()


def _processlist_client_address(value: object) -> str | None:
    """Return the IP portion of a PROCESSLIST Host endpoint."""
    endpoint = str(value or "").strip()
    if not endpoint:
        return None

    if endpoint.startswith("["):
        closing_bracket = endpoint.find("]")
        if closing_bracket < 0:
            return None
        host = endpoint[1:closing_bracket]
        port_text = endpoint[closing_bracket + 1 :]
        if not port_text.startswith(":"):
            return None
        port_text = port_text[1:]
    else:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            return None

    if not port_text.isdigit() or not 0 < int(port_text) <= 65535:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _replica_peer_endpoint_candidates(cursor, member_ports: list[int]) -> list[dict]:
    """Build unpaired replica endpoint candidates from binlog dump clients."""
    cursor.execute("SHOW PROCESSLIST")
    client_hosts: list[str] = []
    seen_hosts: set[str] = set()
    for raw_row in cursor.fetchall():
        row = _lower_row(raw_row)
        command = str(row.get("command") or "").strip().lower()
        if not command.startswith("binlog dump"):
            continue
        host = _processlist_client_address(row.get("host"))
        if host and host not in seen_hosts:
            seen_hosts.add(host)
            client_hosts.append(host)

    candidates: list[dict] = []
    seen_endpoints: set[tuple[str, int]] = set()
    for host in client_hosts:
        for port in member_ports:
            endpoint = (host, port)
            if endpoint in seen_endpoints:
                continue
            seen_endpoints.add(endpoint)
            candidates.append({"host": host, "port": port})
            if len(candidates) >= 32:
                return candidates
    return candidates


class LowerDictCursor:
    """Wrapper that lowercases all dict keys from pymysql DictCursor"""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, args=None):
        return self._cursor.execute(sql, args)

    def executemany(self, sql, args=None):
        return self._cursor.executemany(sql, args)

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [{k.lower(): v for k, v in r.items()} for r in rows]

    def fetchone(self):
        row = self._cursor.fetchone()
        if row:
            return {k.lower(): v for k, v in row.items()}
        return row

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size)
        return [{k.lower(): v for k, v in row.items()} for row in rows]

    def close(self):
        self._cursor.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def description(self):
        return self._cursor.description


@contextmanager
def mysql_connection(instance: DBInstance, *, read_timeout: int | None = None):
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("pymysql driver is not installed") from exc

    logger.debug("MySQL 连接 | host=%s:%s | user=%s | db=%s", instance.host, instance.port, instance.username, instance.database_name)
    with database_tls_material(instance) as tls:
        connect_kwargs = dict(
            host=instance.host,
            port=instance.port,
            user=instance.username,
            password=decrypt_password(instance.password_cipher),
            database=instance.database_name or None,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
        if read_timeout is not None:
            connect_kwargs["read_timeout"] = read_timeout
        if tls.get("ca"):
            connect_kwargs["ssl_ca"] = tls["ca"]
        if tls.get("cert"):
            connect_kwargs["ssl_cert"] = tls["cert"]
        if tls.get("key"):
            connect_kwargs["ssl_key"] = tls["key"]
        if instance.certificate_profile_id:
            connect_kwargs["ssl_verify_cert"] = bool(instance.tls_verify)
            connect_kwargs["ssl_verify_identity"] = bool(instance.tls_verify)
        try:
            conn = pymysql.connect(**connect_kwargs)
        except Exception as exc:
            logger.error("MySQL 连接失败 | host=%s:%s | user=%s | error=%s", instance.host, instance.port, instance.username, exc)
            raise
        # Wrap cursor() to lowercase dict keys
        _orig_cursor = conn.cursor
        conn.cursor = lambda *a, **kw: LowerDictCursor(_orig_cursor(*a, **kw))
        try:
            yield conn
        finally:
            conn.close()


def test_mysql_connection(instance: DBInstance) -> dict:
    with mysql_connection(instance) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            cursor.fetchone()
            detail = {
                "host": instance.host,
                "port": instance.port,
                "account_type": instance.account_type or "system",
                "capability": "full_inspection" if (instance.account_type or "system") == "system" else "business_limited",
                "warnings": [],
            }
            if detail["account_type"] == "business":
                detail["warnings"].append(
                    "业务用户连接成功。程序将按业务账号权限降级采集，复制状态、全局变量、performance_schema 等可能受权限限制。"
                )
            else:
                for sql_name, sql in {
                    "SHOW GLOBAL STATUS": "SHOW GLOBAL STATUS LIKE 'Threads_connected'",
                    "performance_schema": "SELECT COUNT(*) AS count FROM performance_schema.events_statements_summary_by_digest",
                }.items():
                    try:
                        cursor.execute(sql)
                        cursor.fetchone()
                    except Exception as exc:
                        detail["warnings"].append(f"{sql_name} 权限检查失败，系统账号巡检能力会受限：{exc}")
                try:
                    cursor.execute(
                        "SELECT @@hostname AS hostname, @@port AS port, @@version AS version, "
                        "@@server_id AS server_id, @@server_uuid AS server_uuid, "
                        "@@read_only AS read_only, @@super_read_only AS super_read_only"
                    )
                    local = _lower_row(cursor.fetchone())
                    cursor.execute(
                        "SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                        "('group_replication_group_name','group_replication_single_primary_mode')"
                    )
                    variables = {
                        str(row.get("variable_name") or "").lower(): row.get("value")
                        for row in cursor.fetchall()
                    }

                    downstream = _first_rows(cursor, ("SHOW REPLICAS", "SHOW SLAVE HOSTS"))
                    upstream = _first_rows(cursor, ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"))
                    group_members = _first_rows(cursor, (
                        "SELECT MEMBER_ID, MEMBER_HOST, MEMBER_PORT, MEMBER_STATE, "
                        "MEMBER_ROLE, MEMBER_VERSION "
                        "FROM performance_schema.replication_group_members",
                    ))

                    local_server_id = local.get("server_id")
                    local_server_uuid = local.get("server_uuid")
                    local_hostname = str(local.get("hostname") or instance.instance_name or instance.host)
                    local_port = int(local.get("port") or instance.port)
                    read_only = bool(int(local.get("read_only") or 0))
                    super_read_only = bool(int(local.get("super_read_only") or 0))
                    members: list[dict] = []
                    peer_endpoint_candidates: list[dict] = []
                    upstream_facts: dict[str, object] = {}
                    replication_delay_seconds = None

                    group_name = str(variables.get("group_replication_group_name") or "").strip()
                    if group_members:
                        architecture_type = "mgr"
                        topology_group = f"mysql:mgr:{group_name or local_server_uuid or local_server_id}"
                        role_type = "replica" if read_only or super_read_only else "primary"
                        for row in group_members:
                            member_uuid = row.get("member_id")
                            reported_host = row.get("member_host")
                            member_port = int(row.get("member_port") or instance.port)
                            is_local = str(member_uuid or "") == str(local_server_uuid or "") or (
                                str(reported_host or "").lower() == local_hostname.lower()
                                and member_port == local_port
                            )
                            member_role = "primary" if str(row.get("member_role") or "").upper() == "PRIMARY" else "replica"
                            if is_local:
                                role_type = member_role
                                continue
                            members.append({
                                "member_key": _member_key(member_uuid, None, reported_host, member_port),
                                "server_uuid": member_uuid,
                                "name": str(reported_host or member_uuid or "MySQL 成员"),
                                "host": str(reported_host or ""),
                                "reported_host": str(reported_host or ""),
                                "port": member_port,
                                "role_type": member_role,
                                "health_status": "healthy" if str(row.get("member_state") or "").upper() == "ONLINE" else "warning",
                                "member_state": row.get("member_state"),
                                "db_version": row.get("member_version"),
                            })
                    elif downstream or upstream:
                        architecture_type = "replication"
                        role_type = "replica" if upstream or read_only or super_read_only else "primary"
                        source = upstream[0] if upstream else {}
                        source_uuid = source.get("source_uuid") or source.get("master_uuid")
                        source_server_id = source.get("source_server_id") or source.get("master_server_id")
                        if len(upstream) == 1:
                            source_host = source.get("source_host") or source.get("master_host")
                            source_port = source.get("source_port") or source.get("master_port")
                            if source_host not in (None, "") or source_server_id not in (None, ""):
                                resolved_source_host = _reported_member_host(
                                    source_host,
                                    source_server_id,
                                    local_server_id,
                                    instance.host,
                                )
                                if resolved_source_host:
                                    upstream_facts["upstream_host"] = resolved_source_host
                            if source_port not in (None, ""):
                                upstream_facts["upstream_port"] = int(source_port)
                            if source_uuid not in (None, ""):
                                upstream_facts["upstream_uuid"] = source_uuid
                            if source_server_id not in (None, ""):
                                upstream_facts["upstream_server_id"] = source_server_id
                        topology_anchor = (
                            source_uuid
                            or source_server_id
                            or source.get("source_host")
                            or source.get("master_host")
                            or local_server_uuid
                            or local_server_id
                        )
                        topology_group = f"mysql:replication:{str(topology_anchor).strip().lower()}"

                        unresolved_member_ports: list[int] = []
                        for row in downstream:
                            member_server_id = row.get("server_id")
                            member_uuid = row.get("replica_uuid") or row.get("slave_uuid")
                            reported_host = row.get("host")
                            member_port = int(row.get("port") or instance.port)
                            member_host = _reported_member_host(
                                reported_host,
                                member_server_id,
                                local_server_id,
                                instance.host,
                            )
                            if not member_host and member_port not in unresolved_member_ports:
                                unresolved_member_ports.append(member_port)
                            members.append({
                                "member_key": _member_key(member_uuid, member_server_id, member_host, member_port),
                                "server_id": member_server_id,
                                "server_uuid": member_uuid,
                                "name": str(reported_host or member_host or member_uuid or "MySQL 从库"),
                                "host": member_host,
                                "reported_host": str(reported_host or ""),
                                "port": member_port,
                                "role_type": "replica",
                                "health_status": "unknown",
                            })

                        if unresolved_member_ports:
                            try:
                                peer_endpoint_candidates = _replica_peer_endpoint_candidates(
                                    cursor,
                                    unresolved_member_ports,
                                )
                            except Exception as exc:
                                detail["warnings"].append(
                                    "MySQL 副本端点候选探测失败，保留成员 host 未知状态："
                                    f"{exc}"
                                )

                        for row in upstream:
                            source_host = row.get("source_host") or row.get("master_host")
                            source_port = int(row.get("source_port") or row.get("master_port") or instance.port)
                            member_host = _reported_member_host(
                                source_host,
                                row.get("source_server_id") or row.get("master_server_id"),
                                local_server_id,
                                instance.host,
                            )
                            io_running = str(row.get("replica_io_running") or row.get("slave_io_running") or "").lower()
                            sql_running = str(row.get("replica_sql_running") or row.get("slave_sql_running") or "").lower()
                            replication_delay_seconds = row.get("seconds_behind_source")
                            if replication_delay_seconds is None:
                                replication_delay_seconds = row.get("seconds_behind_master")
                            members.append({
                                "member_key": _member_key(source_uuid, source_server_id, member_host, source_port),
                                "server_id": source_server_id,
                                "server_uuid": source_uuid,
                                "name": str(source_host or member_host or "MySQL 主库"),
                                "host": member_host,
                                "reported_host": str(source_host or ""),
                                "port": source_port,
                                "role_type": "primary",
                                "health_status": "healthy" if io_running == "yes" and sql_running == "yes" else "unknown",
                            })
                    else:
                        architecture_type = "replication" if read_only or super_read_only else "standalone"
                        role_type = "replica" if read_only or super_read_only else "primary"
                        topology_group = (
                            f"mysql:replication:{local_server_uuid or local_server_id}"
                            if architecture_type == "replication"
                            else f"mysql:standalone:{local_server_uuid or local_server_id or instance.host}"
                        )

                    detail["version"] = local.get("version")
                    architecture = {
                        "architecture_type": architecture_type,
                        "role_type": role_type,
                        "topology_group": topology_group,
                        "cluster_name": instance.system_name or (
                            f"{local_hostname} {'MGR 集群' if architecture_type == 'mgr' else '复制拓扑'}"
                            if architecture_type != "standalone"
                            else local_hostname
                        ),
                        "hostname": local_hostname,
                        "server_id": local_server_id,
                        "server_uuid": local_server_uuid,
                        "member_key": _member_key(local_server_uuid, local_server_id, instance.host, instance.port),
                        "db_version": local.get("version"),
                        "read_only": read_only,
                        "super_read_only": super_read_only,
                        "replication_delay_seconds": replication_delay_seconds,
                        "members": members,
                    }
                    if peer_endpoint_candidates:
                        architecture["peer_endpoint_candidates"] = peer_endpoint_candidates
                    architecture.update(upstream_facts)
                    detail["architecture"] = architecture
                except Exception as exc:
                    detail["warnings"].append(f"MySQL 架构状态识别失败：{exc}")
        return detail
