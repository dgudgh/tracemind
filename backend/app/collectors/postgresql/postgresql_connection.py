import logging
from contextlib import contextmanager

from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance
from app.services.connection_security import database_tls_material

logger = logging.getLogger("app.collectors.postgresql.connection")


def _valid_port(value) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _replication_health(state: str, *, lag_known: bool) -> str:
    if not state:
        return "unknown"
    if state != "streaming":
        return "warning"
    return "healthy" if lag_known else "unknown"


@contextmanager
def postgresql_connection(instance: DBInstance, *, statement_timeout_ms: int | None = None):
    logger.debug("PostgreSQL 连接 | host=%s:%s | user=%s | db=%s", instance.host, instance.port, instance.username, instance.database_name)
    base_kwargs = {
        "host": instance.host,
        "port": instance.port,
        "user": instance.username,
        "password": decrypt_password(instance.password_cipher),
        "dbname": instance.database_name or "postgres",
        "connect_timeout": 5,
    }
    if statement_timeout_ms is not None:
        # Server-side cap for every statement on this connection; connect_timeout
        # alone only bounds the handshake, not the query itself.
        base_kwargs["options"] = f"-c statement_timeout={max(1, int(statement_timeout_ms))}"
    if instance.sslmode:
        base_kwargs["sslmode"] = instance.sslmode

    with database_tls_material(instance) as tls:
        if tls.get("ca"):
            base_kwargs["sslrootcert"] = tls["ca"]
        if tls.get("cert"):
            base_kwargs["sslcert"] = tls["cert"]
        if tls.get("key"):
            base_kwargs["sslkey"] = tls["key"]
        if instance.certificate_profile_id and not instance.sslmode:
            base_kwargs["sslmode"] = "verify-full" if instance.tls_verify else "require"
        conn = None
        try:
            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(**base_kwargs, cursor_factory=psycopg2.extras.RealDictCursor)
        except ImportError:
            try:
                import psycopg
                from psycopg.rows import dict_row

                conn = psycopg.connect(**base_kwargs, row_factory=dict_row)
            except ImportError as exc:
                raise RuntimeError("PostgreSQL driver is not installed, install psycopg[binary] or psycopg2-binary") from exc

        try:
            yield conn
        finally:
            if conn is not None:
                conn.close()


def test_postgresql_connection(instance: DBInstance) -> dict:
    with postgresql_connection(instance) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT version() AS version, current_database() AS database_name, current_user AS current_user")
            row = cursor.fetchone() or {}
            warnings = []
            architecture = {
                "architecture_type": "standalone",
                "role_type": "primary",
                "in_recovery": False,
                "streaming_replica_count": 0,
                "members": [],
            }
            try:
                cursor.execute(
                    "SELECT pg_is_in_recovery() AS in_recovery, "
                    "inet_server_addr()::text AS host_ip, inet_server_port() AS port"
                )
                recovery_row = cursor.fetchone() or {}
                in_recovery = bool(recovery_row.get("in_recovery"))
                local_host_ip = str(recovery_row.get("host_ip") or "").strip()
                local_listen_port = _valid_port(recovery_row.get("port"))
                endpoint_port = local_listen_port or int(instance.port)
                members = []
                topology_host = instance.host
                topology_port = endpoint_port
                upstream_facts = {}
                if not in_recovery:
                    try:
                        cursor.execute(
                            "SELECT application_name, client_addr::text AS client_addr, "
                            "client_hostname, state, sync_state, "
                            "EXTRACT(EPOCH FROM replay_lag) AS replay_lag_seconds "
                            "FROM pg_stat_replication ORDER BY application_name, client_addr"
                        )
                        replication_rows = list(cursor.fetchall() or [])
                        for index, replica in enumerate(replication_rows, start=1):
                            replica = dict(replica)
                            application_name = str(replica.get("application_name") or "").strip()
                            client_hostname = str(replica.get("client_hostname") or "").strip()
                            client_addr = str(replica.get("client_addr") or "").strip()
                            # application_name is a client-controlled label, not an endpoint.
                            replica_host = client_addr or client_hostname
                            member_key = (
                                f"{replica_host.lower()}:{endpoint_port}"
                                if replica_host
                                else f"postgresql-application:{application_name.lower()}"
                                if application_name
                                else f"postgresql-replica:{index}"
                            )
                            state = str(replica.get("state") or "").lower()
                            lag_value = replica.get("replay_lag_seconds")
                            try:
                                replication_delay_seconds = (
                                    None if lag_value is None else float(lag_value)
                                )
                            except (TypeError, ValueError):
                                replication_delay_seconds = None
                            members.append({
                                "member_key": member_key,
                                "name": client_hostname or application_name or replica_host or "PostgreSQL 从库",
                                "host": replica_host,
                                "reported_host": client_hostname,
                                "application_name": application_name,
                                # pg_stat_replication.client_port is the WAL client's
                                # ephemeral source port. The source listener is only a
                                # same-port probe candidate until the peer proves identity.
                                "port": endpoint_port,
                                "port_inferred": True,
                                "role_type": "replica",
                                "health_status": _replication_health(
                                    state,
                                    lag_known=replication_delay_seconds is not None,
                                ),
                                "member_state": state,
                                "sync_state": replica.get("sync_state"),
                                "replication_delay_seconds": replication_delay_seconds,
                            })
                    except Exception as exc:
                        warnings.append(f"pg_stat_replication 成员识别失败：{exc}")
                        conn.rollback()
                else:
                    try:
                        cursor.execute(
                            "SELECT status, sender_host, sender_port, slot_name "
                            "FROM pg_stat_wal_receiver LIMIT 1"
                        )
                        receiver = dict(cursor.fetchone() or {})
                        sender_host = str(receiver.get("sender_host") or "").strip()
                        sender_port = _valid_port(receiver.get("sender_port"))
                        if sender_host:
                            topology_host = sender_host
                            topology_port = sender_port or endpoint_port
                            status = str(receiver.get("status") or "").lower()
                            upstream_member = {
                                "member_key": f"{sender_host.lower()}:{sender_port or endpoint_port}",
                                "name": sender_host,
                                "host": sender_host,
                                "reported_host": sender_host,
                                "port": sender_port or endpoint_port,
                                "role_type": "primary",
                                "health_status": _replication_health(status, lag_known=False),
                                "member_state": status,
                                "slot_name": receiver.get("slot_name"),
                            }
                            if sender_port is None:
                                upstream_member["port_inferred"] = True
                            members.append(upstream_member)
                            if sender_port is not None:
                                upstream_facts = {
                                    "upstream_host": sender_host,
                                    "upstream_port": sender_port,
                                }
                    except Exception as exc:
                        warnings.append(f"pg_stat_wal_receiver 上游识别失败：{exc}")
                        conn.rollback()
                streaming_replica_count = sum(
                    1 for member in members
                    if member.get("role_type") == "replica" and member.get("member_state") == "streaming"
                )
                is_replication = in_recovery or bool(members)
                topology_anchor = instance.system_name or f"{str(topology_host).lower()}:{topology_port}"
                architecture = {
                    "architecture_type": "streaming_replication" if is_replication else "standalone",
                    "role_type": "replica" if in_recovery else "primary",
                    "topology_group": (
                        f"postgresql:streaming:{str(topology_anchor).lower()}"
                        if is_replication
                        else f"postgresql:standalone:{str(instance.host).lower()}:{instance.port}"
                    ),
                    "cluster_name": instance.system_name or f"{row.get('database_name') or instance.database_name or 'PostgreSQL'} 流复制",
                    "member_key": (
                        f"{local_host_ip.lower()}:{local_listen_port}"
                        if local_host_ip and local_listen_port is not None
                        else f"{str(instance.host).lower()}:{instance.port}"
                    ),
                    "host_ip": local_host_ip or None,
                    "port": local_listen_port,
                    "db_version": row.get("version"),
                    "in_recovery": in_recovery,
                    "streaming_replica_count": streaming_replica_count,
                    "members": members,
                }
                architecture.update(upstream_facts)
            except Exception as exc:
                warnings.append(f"PostgreSQL 架构状态识别失败：{exc}")
                conn.rollback()
            for name, sql in {
                "pg_stat_activity": "SELECT COUNT(*) AS cnt FROM pg_stat_activity",
                "pg_stat_database": "SELECT COUNT(*) AS cnt FROM pg_stat_database",
            }.items():
                try:
                    cursor.execute(sql)
                    cursor.fetchone()
                except Exception as exc:
                    warnings.append(f"{name} 权限检查失败，巡检能力会降级：{exc}")
                    conn.rollback()
            try:
                cursor.execute(
                    "SELECT system_identifier::text AS system_identifier "
                    "FROM pg_control_system()"
                )
                identity_row = cursor.fetchone() or {}
                system_identifier = str(identity_row.get("system_identifier") or "").strip()
                if system_identifier:
                    architecture["system_identifier"] = system_identifier
                    topology_kind = (
                        "streaming"
                        if architecture.get("architecture_type") == "streaming_replication"
                        else "standalone"
                    )
                    architecture["topology_group"] = (
                        f"postgresql:{topology_kind}:"
                        f"{str(instance.system_name or system_identifier).lower()}"
                    )
            except Exception as exc:
                warnings.append(
                    "pg_control_system 系统标识读取失败，节点身份校验能力会降级："
                    f"{exc}"
                )
                conn.rollback()
            return {
                "host": instance.host,
                "port": instance.port,
                "database": row.get("database_name") or instance.database_name,
                "current_user": row.get("current_user") or instance.username,
                "version": row.get("version"),
                "architecture": architecture,
                "capability": "full_inspection" if not warnings else "limited",
                "warnings": warnings,
            }
