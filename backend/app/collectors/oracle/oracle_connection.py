import hashlib
import logging
import re
import threading
import time
from contextlib import contextmanager

from app.core.crypto import decrypt_password
from app.services.connection_security import oracle_wallet_settings
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.oracle.connection")

ORACLE_CONNECT_TIMEOUT_SECONDS = 2.0
ORACLE_FAILURE_CACHE_TTL_SECONDS = 30.0
ORACLE_CONNECT_INFLIGHT_WAIT_SECONDS = 0.75
_oracle_failure_cache: dict[tuple[object, ...], tuple[float, str]] = {}
_oracle_connection_locks: dict[tuple[object, ...], threading.Lock] = {}
_oracle_failure_cache_lock = threading.Lock()


def _oracle_interval_seconds(value: object) -> float | None:
    """Convert an Oracle DAY TO SECOND interval string to seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"(?P<sign>[+-])?\s*(?:(?P<days>\d+)\s+)?"
        r"(?P<hours>\d{1,2}):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d+)?)",
        text,
    )
    if not match:
        return None
    seconds = (
        int(match.group("days") or 0) * 86400
        + int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + float(match.group("seconds"))
    )
    if match.group("sign") == "-":
        seconds *= -1
    return round(seconds, 3)


def _oracle_full_version(cursor, fallback: object = "") -> str:
    fallback_text = str(fallback or "").strip()
    try:
        cursor.execute("SELECT version_full FROM v$instance")
        row = cursor.fetchone() or ()
        candidate = str(row[0] if len(row) > 0 else "").strip()
        if re.match(r"^\d+(?:\.\d+)+", candidate):
            return candidate
    except Exception:
        pass
    return fallback_text


def _oracle_capability_evidence(
    result: dict,
    probes: dict[str, tuple[str, str]],
    rows: list[dict],
) -> dict:
    return {
        **result,
        "sql": [sql for _label, sql in probes.values()],
        "columns": ["capability", "label", "status", "message"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": False,
        "read_only": True,
        "evidence_source": "live_capability_probe",
    }


def _oracle_connection_key(instance: DBInstance) -> tuple[object, ...]:
    credential_fingerprint = hashlib.sha256(
        str(instance.password_cipher or "").encode("utf-8")
    ).hexdigest()[:16]
    return (
        (instance.host or "").strip(),
        int(instance.port or 1521),
        (instance.username or "").strip().upper(),
        (instance.oracle_connect_type or "service").strip().lower(),
        (instance.sid or "").strip().upper(),
        (instance.service_name or "").strip().upper(),
        effective_oracle_account_type(instance),
        credential_fingerprint,
    )


def clear_oracle_connection_failure_cache() -> None:
    with _oracle_failure_cache_lock:
        _oracle_failure_cache.clear()


def _recent_failure_message(key: tuple[object, ...]) -> str | None:
    now = time.monotonic()
    with _oracle_failure_cache_lock:
        cached = _oracle_failure_cache.get(key)
        if not cached:
            return None
        failed_at, message = cached
        if now - failed_at <= ORACLE_FAILURE_CACHE_TTL_SECONDS:
            return message
        _oracle_failure_cache.pop(key, None)
    return None


def _remember_failure(key: tuple[object, ...], message: str) -> None:
    with _oracle_failure_cache_lock:
        _oracle_failure_cache[key] = (time.monotonic(), message)


def _forget_failure(key: tuple[object, ...]) -> None:
    with _oracle_failure_cache_lock:
        _oracle_failure_cache.pop(key, None)


def _connection_lock(key: tuple[object, ...]) -> threading.Lock:
    with _oracle_failure_cache_lock:
        lock = _oracle_connection_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _oracle_connection_locks[key] = lock
        return lock


def effective_oracle_account_type(instance: DBInstance) -> str:
    """Return the runtime account type used for Oracle capability checks."""
    username = (instance.username or "").strip().upper()
    account_type = (instance.account_type or "system").strip().lower()
    if username == "SYS" or account_type == "sysdba":
        return "sysdba"
    if account_type == "business":
        return "business"
    return "system"


def probe_oracle_inspection_capability(conn, account_type: str) -> dict:
    """Probe the catalog access used by inspection instead of trusting the account label."""
    normalized_type = str(account_type or "system").strip().lower()
    if normalized_type == "business":
        return {
            "account_type": "business",
            "capability": "business_limited",
            "capability_label": "业务账号降级巡检",
            "status": "warning",
            "message": "业务账号仅采集当前账号可见对象，DBA、AWR、ASM、FRA 和 Data Guard 等检查会自动跳过。",
            "available_capabilities": ["business_objects"],
            "missing_capabilities": ["dynamic_performance", "dba_catalog", "awr_catalog"],
            "warnings": ["当前配置为业务账号，不执行数据库级完整巡检。"],
        }

    probes = {
        "dynamic_performance": ("动态性能视图", "SELECT 1 FROM v$instance WHERE ROWNUM = 1"),
        "dba_catalog": ("DBA 数据字典", "SELECT 1 FROM dba_data_files WHERE ROWNUM = 1"),
        "awr_catalog": ("AWR 历史视图", "SELECT 1 FROM dba_hist_snapshot WHERE ROWNUM = 1"),
    }
    available: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    evidence_rows: list[dict] = []
    for capability, (label, sql) in probes.items():
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            cursor.fetchone()
            available.append(capability)
            evidence_rows.append(
                {
                    "capability": capability,
                    "label": label,
                    "status": "available",
                    "message": "查询成功",
                }
            )
        except Exception as exc:
            missing.append(capability)
            warnings.append(f"{label}不可访问：{exc}")
            evidence_rows.append(
                {
                    "capability": capability,
                    "label": label,
                    "status": "unavailable",
                    "message": str(exc),
                }
            )
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            cursor.close()

    if missing:
        return _oracle_capability_evidence(
            {
                "account_type": normalized_type,
                "capability": "limited_inspection",
                "capability_label": "权限受限巡检",
                "status": "warning",
                "message": f"账号连接成功，但 {len(missing)} 类巡检数据不可访问；报告已按实际权限降级。",
                "available_capabilities": available,
                "missing_capabilities": missing,
                "warnings": warnings,
            },
            probes,
            evidence_rows,
        )

    is_sysdba = normalized_type == "sysdba"
    return _oracle_capability_evidence(
        {
            "account_type": normalized_type,
            "capability": "full_inspection" if is_sysdba else "standard_inspection",
            "capability_label": "SYSDBA 完整巡检" if is_sysdba else "SYSTEM 标准巡检",
            "status": "normal",
            "message": (
                "SYSDBA 连接且巡检所需视图均可访问。"
                if is_sysdba
                else "SYSTEM 账号的动态性能、DBA 数据字典和 AWR 视图均可访问；SYS 内部基表类可选检查会安全跳过。"
            ),
            "available_capabilities": available,
            "missing_capabilities": [],
            "warnings": [],
        },
        probes,
        evidence_rows,
    )


def build_oracle_dsn(instance: DBInstance) -> str:
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("oracledb driver is not installed") from exc

    if instance.oracle_connect_type == "sid" and instance.sid:
        return oracledb.makedsn(instance.host, instance.port, sid=instance.sid)
    return oracledb.makedsn(
        instance.host, instance.port, service_name=instance.service_name
    )


def _fetch_dicts(cursor) -> list[dict]:
    columns = [str(item[0]).lower() for item in (cursor.description or [])]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _oracle_parenthesized_sections(value: str, section_name: str) -> list[str]:
    """Return balanced ``(SECTION=...)`` fragments from an Oracle descriptor."""
    text = str(value or "")
    pattern = re.compile(rf"\(\s*{re.escape(section_name)}\s*=", re.IGNORECASE)
    sections: list[str] = []
    for match in pattern.finditer(text):
        depth = 0
        for index in range(match.start(), len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    sections.append(text[match.start():index + 1])
                    break
    return sections


def _oracle_descriptor_values(value: str, key: str) -> list[str]:
    values = re.findall(
        rf"\(\s*{re.escape(key)}\s*=\s*([^()]+?)\s*\)",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return [item.strip().strip("'\"") for item in values if item.strip().strip("'\"")]


def _oracle_service_clause(value: object) -> str:
    """Extract only the SERVICE connect identifier from archive-destination text."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(?:^|\s)SERVICE\s*=\s*", text, flags=re.IGNORECASE)
    if not match:
        return text
    tail = text[match.end():].lstrip()
    if not tail:
        return ""
    if tail[0] in {'"', "'"}:
        quote = tail[0]
        end = tail.find(quote, 1)
        return tail[1:end] if end >= 1 else tail[1:]
    if tail[0] == "(":
        depth = 0
        for index, char in enumerate(tail):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return tail[:index + 1]
    return tail.split()[0]


def _parse_oracle_connect_identifier(value: object) -> dict:
    """Parse endpoint evidence without resolving aliases or retaining raw descriptors."""
    identifier = _oracle_service_clause(value).strip()
    result = {
        "host": "",
        "port": None,
        "oracle_connect_type": "",
        "service_name": "",
        "sid": "",
        "connect_identifier": "",
        "endpoint_unresolved": True,
        "endpoint_candidates": [],
    }
    if not identifier:
        return result

    easy = re.fullmatch(
        r"//(?P<host>\[[^\]]+\]|[^/:\s()]+):(?P<port>\d{1,5})/"
        r"(?P<service>[^/?\s:]+)(?::[^/?\s]+)?(?:/[^?\s]+)?(?:\?[^\s]+)?",
        identifier,
        flags=re.IGNORECASE,
    )
    if easy:
        port = int(easy.group("port"))
        if 1 <= port <= 65535:
            result.update({
                "host": easy.group("host").strip("[]"),
                "port": port,
                "oracle_connect_type": "service_name",
                "service_name": easy.group("service"),
                "endpoint_unresolved": False,
            })
        return result

    address_sections = _oracle_parenthesized_sections(identifier, "ADDRESS")
    if address_sections:
        endpoints: list[dict] = []
        for address in address_sections:
            hosts = _oracle_descriptor_values(address, "HOST")
            ports = _oracle_descriptor_values(address, "PORT")
            if len(hosts) != 1 or len(ports) != 1:
                continue
            try:
                port = int(ports[0])
            except (TypeError, ValueError):
                continue
            if not 1 <= port <= 65535:
                continue
            candidate = {"host": hosts[0].strip("[]"), "port": port}
            if candidate not in endpoints:
                endpoints.append(candidate)

        connect_sections = _oracle_parenthesized_sections(identifier, "CONNECT_DATA")
        connect_text = " ".join(connect_sections)
        service_names = list(dict.fromkeys(_oracle_descriptor_values(connect_text, "SERVICE_NAME")))
        sids = list(dict.fromkeys(_oracle_descriptor_values(connect_text, "SID")))
        if len(service_names) == 1 and not sids:
            result["oracle_connect_type"] = "service_name"
            result["service_name"] = service_names[0]
        elif len(sids) == 1 and not service_names:
            result["oracle_connect_type"] = "sid"
            result["sid"] = sids[0]

        result["endpoint_candidates"] = endpoints
        # Multiple ADDRESS clauses may be load-balancing or failover endpoints.
        # A later direct probe must disambiguate them; never choose the first one.
        if len(address_sections) == 1 and len(endpoints) == 1:
            result["host"] = endpoints[0]["host"]
            result["port"] = endpoints[0]["port"]
            result["endpoint_candidates"] = []
            result["endpoint_unresolved"] = not bool(result["oracle_connect_type"])
        return result

    # A bare SERVICE value is a TNS connect identifier, not a DNS hostname.
    if re.fullmatch(r"[A-Za-z0-9_.-]+", identifier):
        result["connect_identifier"] = identifier
    return result


def _oracle_destination_metadata(*values: object) -> dict:
    """Combine status/catalog evidence while preserving endpoint ambiguity."""
    parsed = [_parse_oracle_connect_identifier(value) for value in values if str(value or "").strip()]
    explicit = [item for item in parsed if item.get("host") and item.get("port")]
    candidates: list[dict] = []
    for item in parsed:
        item_candidates = item.get("endpoint_candidates") or []
        if item.get("host") and item.get("port"):
            item_candidates = [{"host": item["host"], "port": item["port"]}]
        for candidate in item_candidates:
            if candidate not in candidates:
                candidates.append(candidate)

    if len(candidates) == 1 and explicit and not any(item.get("endpoint_candidates") for item in parsed):
        selected = next(
            (item.copy() for item in explicit if item.get("oracle_connect_type")),
            explicit[0].copy(),
        )
        selected["endpoint_candidates"] = []
        return selected

    result = next((item.copy() for item in parsed if item.get("oracle_connect_type")), None)
    if result is None:
        result = next((item.copy() for item in parsed if item.get("connect_identifier")), None)
    if result is None:
        result = _parse_oracle_connect_identifier("")
    result.update({
        "host": "",
        "port": None,
        "endpoint_unresolved": True,
        "endpoint_candidates": candidates,
    })
    aliases = list(dict.fromkeys(
        item.get("connect_identifier") for item in parsed if item.get("connect_identifier")
    ))
    result["connect_identifier"] = aliases[0] if len(aliases) == 1 else ""
    return result


@contextmanager
def oracle_connection(
    instance: DBInstance,
    *,
    force_live: bool = False,
    call_timeout_ms: int | None = None,
):
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("oracledb driver is not installed") from exc

    if call_timeout_ms is not None:
        call_timeout_ms = max(1, int(call_timeout_ms))

    key = _oracle_connection_key(instance)
    recent_failure = None if force_live else _recent_failure_message(key)
    if recent_failure:
        raise RuntimeError(
            f"最近 Oracle 连接失败，已在 {int(ORACLE_FAILURE_CACHE_TTL_SECONDS)} 秒内快速降级：{recent_failure}"
        )

    lock = _connection_lock(key)
    lock_wait_seconds = (
        ORACLE_CONNECT_TIMEOUT_SECONDS + 1.0
        if force_live
        else ORACLE_CONNECT_INFLIGHT_WAIT_SECONDS
    )
    if not lock.acquire(timeout=lock_wait_seconds):
        recent_failure = None if force_live else _recent_failure_message(key)
        if recent_failure:
            raise RuntimeError(
                f"最近 Oracle 连接失败，已在 {int(ORACLE_FAILURE_CACHE_TTL_SECONDS)} 秒内快速降级：{recent_failure}"
            )
        raise RuntimeError("Oracle 连接检测正在进行，已快速降级，请稍后重试")

    try:
        recent_failure = None if force_live else _recent_failure_message(key)
        if recent_failure:
            raise RuntimeError(
                f"最近 Oracle 连接失败，已在 {int(ORACLE_FAILURE_CACHE_TTL_SECONDS)} 秒内快速降级：{recent_failure}"
            )

        dsn = build_oracle_dsn(instance)
        logger.debug("Oracle 连接 | host=%s:%s | user=%s | dsn=%s", instance.host, instance.port, instance.username, dsn)
        connect_kwargs = {
            "user": instance.username,
            "password": decrypt_password(instance.password_cipher),
            "dsn": dsn,
            "tcp_connect_timeout": ORACLE_CONNECT_TIMEOUT_SECONDS,
            "retry_count": 0,
            "retry_delay": 0,
        }
        connect_kwargs.update(oracle_wallet_settings(instance))
        if effective_oracle_account_type(instance) == "sysdba":
            connect_kwargs["mode"] = oracledb.SYSDBA
        try:
            conn = oracledb.connect(**connect_kwargs)
        except Exception as exc:
            logger.error("Oracle 连接失败 | host=%s:%s | user=%s | error=%s", instance.host, instance.port, instance.username, exc)
            _remember_failure(key, str(exc))
            raise
        _forget_failure(key)
    finally:
        lock.release()

    try:
        if call_timeout_ms is not None:
            # Timeout configuration can fail after acquisition; still close the connection.
            conn.call_timeout = call_timeout_ms
        yield conn
    finally:
        conn.close()


def test_oracle_connection(instance: DBInstance, *, force_live: bool = False) -> dict:
    with oracle_connection(instance, force_live=force_live) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM dual")
        cursor.fetchone()
        account_type = effective_oracle_account_type(instance)
        capability = probe_oracle_inspection_capability(conn, account_type)
        database_inspection_mode = account_type in ("system", "sysdba")
        detail = {
            "dsn": build_oracle_dsn(instance),
            "account_type": account_type,
            "capability": capability["capability"],
            "capability_label": capability["capability_label"],
            "capability_detail": capability,
            "warnings": list(capability["warnings"]),
        }

        if database_inspection_mode:
            database_name = ""
            db_unique_name = ""
            database_role = "UNKNOWN"
            open_mode = ""
            protection_mode = ""
            try:
                cursor.execute(
                    "SELECT name, db_unique_name, database_role, open_mode, protection_mode "
                    "FROM v$database"
                )
                row = cursor.fetchone() or ()
                database_name = str(row[0] if len(row) > 0 else "")
                db_unique_name = str(row[1] if len(row) > 1 else database_name)
                database_role = str(row[2] if len(row) > 2 else "UNKNOWN").upper()
                open_mode = str(row[3] if len(row) > 3 else "")
                protection_mode = str(row[4] if len(row) > 4 else "")
            except Exception as exc:
                detail["warnings"].append(f"Oracle 数据库角色识别失败：{exc}")

            local_instance_name = ""
            local_host_name = ""
            local_version = ""
            try:
                cursor.execute("SELECT instance_name, host_name, version, status FROM v$instance")
                row = cursor.fetchone() or ()
                local_instance_name = str(row[0] if len(row) > 0 else "")
                local_host_name = str(row[1] if len(row) > 1 else "")
                local_version = str(row[2] if len(row) > 2 else "")
                local_version = _oracle_full_version(cursor, local_version)
            except Exception as exc:
                detail["warnings"].append(f"Oracle 实例身份识别失败：{exc}")

            rac_instances = []
            try:
                try:
                    cursor.execute(
                        "SELECT inst_id, instance_number, instance_name, host_name, "
                        "utl_inaddr.get_host_address(host_name) AS host_ip, "
                        "status, version, thread# "
                        "FROM gv$instance ORDER BY instance_number"
                    )
                    rac_instances = _fetch_dicts(cursor)
                except Exception:
                    cursor.execute(
                        "SELECT inst_id, instance_number, instance_name, host_name, "
                        "status, version, thread# "
                        "FROM gv$instance ORDER BY instance_number"
                    )
                    rac_instances = _fetch_dicts(cursor)
            except Exception as exc:
                detail["warnings"].append(f"Oracle RAC 成员识别失败：{exc}")

            rac_services_by_instance: dict[str, list[str]] = {}
            if len(rac_instances) > 1 and (instance.oracle_connect_type or "service").lower() != "sid":
                try:
                    cursor.execute(
                        "SELECT inst_id, name, network_name FROM gv$active_services "
                        "WHERE name NOT IN ('SYS$BACKGROUND', 'SYS$USERS') ORDER BY inst_id, name"
                    )
                    for service_row in _fetch_dicts(cursor):
                        inst_id = str(service_row.get("inst_id") or "").strip()
                        service_name = str(
                            service_row.get("network_name") or service_row.get("name") or ""
                        ).strip()
                        if not inst_id or not service_name:
                            continue
                        services = rac_services_by_instance.setdefault(inst_id, [])
                        if service_name not in services:
                            services.append(service_name)
                except Exception as exc:
                    detail["warnings"].append(f"Oracle RAC 服务身份识别失败：{exc}")

            dataguard_names = []
            dataguard_config = []
            try:
                try:
                    cursor.execute("SELECT db_unique_name, parent_dbun, dest_role FROM v$dataguard_config ORDER BY db_unique_name")
                    dataguard_config = _fetch_dicts(cursor)
                except Exception:
                    cursor.execute("SELECT db_unique_name FROM v$dataguard_config ORDER BY db_unique_name")
                    dataguard_config = _fetch_dicts(cursor)
                dataguard_names = [
                    str(row.get("db_unique_name") or "").strip()
                    for row in dataguard_config
                    if str(row.get("db_unique_name") or "").strip()
                ]
            except Exception:
                dataguard_names = []

            dataguard_destinations = []
            try:
                cursor.execute(
                    "SELECT dest_id, status, type, database_mode, recovery_mode, gap_status, "
                    "db_unique_name, destination FROM v$archive_dest_status "
                    "WHERE status <> 'INACTIVE' ORDER BY dest_id"
                )
                dataguard_destinations = [
                    row for row in _fetch_dicts(cursor)
                    if str(row.get("db_unique_name") or "").upper() not in {"", "NONE", db_unique_name.upper()}
                    or any(token in str(row.get("type") or "").upper() for token in ("PHYSICAL", "LOGICAL", "FAR SYNC"))
                ]
            except Exception:
                dataguard_destinations = []

            archive_destinations_by_id = {}
            if dataguard_destinations:
                try:
                    cursor.execute(
                        "SELECT dest_id, destination FROM v$archive_dest ORDER BY dest_id"
                    )
                    archive_destinations_by_id = {
                        str(row.get("dest_id")): row.get("destination")
                        for row in _fetch_dicts(cursor)
                        if row.get("dest_id") is not None and row.get("destination")
                    }
                except Exception:
                    # v$archive_dest may be unavailable to otherwise usable monitoring
                    # accounts. The status-view evidence remains valid and discovery
                    # degrades to an unresolved identifier instead of failing login.
                    archive_destinations_by_id = {}

            role_type = "primary" if database_role == "PRIMARY" else (
                "standby" if database_role not in {"", "UNKNOWN"} else "unknown"
            )
            instance_count = max(1, len(rac_instances))
            has_dataguard = (
                role_type == "standby"
                or len(set(dataguard_names)) > 1
                or bool(dataguard_destinations)
            )
            dataguard_stats = []
            if has_dataguard:
                try:
                    cursor.execute(
                        "SELECT name, value, unit, time_computed, datum_time "
                        "FROM v$dataguard_stats "
                        "WHERE name IN ('transport lag', 'apply lag', 'apply finish time') "
                        "ORDER BY name"
                    )
                    dataguard_stats = _fetch_dicts(cursor)
                except Exception as exc:
                    detail["warnings"].append(f"Oracle Data Guard 延迟识别失败：{exc}")
            dataguard_stats_by_name = {
                str(row.get("name") or "").strip().lower(): row
                for row in dataguard_stats
                if str(row.get("name") or "").strip()
            }
            transport_lag_row = dataguard_stats_by_name.get("transport lag") or {}
            apply_lag_row = dataguard_stats_by_name.get("apply lag") or {}
            apply_finish_row = dataguard_stats_by_name.get("apply finish time") or {}
            transport_lag = transport_lag_row.get("value")
            apply_lag = apply_lag_row.get("value")
            apply_finish_time = apply_finish_row.get("value")
            transport_lag_seconds = _oracle_interval_seconds(transport_lag)
            apply_lag_seconds = _oracle_interval_seconds(apply_lag)
            lag_values = [
                max(0.0, value)
                for value in (transport_lag_seconds, apply_lag_seconds)
                if value is not None
            ]
            replication_delay_seconds = max(lag_values) if lag_values else None
            architecture_type = "rac" if instance_count > 1 else ("data_guard" if has_dataguard else "standalone")
            members = []
            seen_members = set()
            local_host_ip = ""

            if instance_count > 1:
                for rac_member in rac_instances:
                    instance_name = str(rac_member.get("instance_name") or "").strip()
                    if instance_name and instance_name.upper() == local_instance_name.upper():
                        local_host_ip = str(rac_member.get("host_ip") or "").strip()
                        continue
                    member_key = f"{db_unique_name or database_name}:{instance_name}".lower()
                    if not instance_name or member_key in seen_members:
                        continue
                    seen_members.add(member_key)
                    status = str(rac_member.get("status") or "").upper()
                    member_hostname = str(rac_member.get("host_name") or "").strip()
                    member_host_ip = str(rac_member.get("host_ip") or "").strip()
                    connect_type = (instance.oracle_connect_type or "service").strip().lower()
                    member_services = rac_services_by_instance.get(
                        str(rac_member.get("inst_id") or "").strip(), []
                    )
                    service_name = ""
                    sid = ""
                    if connect_type == "sid":
                        sid = instance_name
                    else:
                        configured_service = str(instance.service_name or "").strip()
                        active_by_key = {item.lower(): item for item in member_services}
                        if configured_service.lower() in active_by_key:
                            service_name = active_by_key[configured_service.lower()]
                        elif len(member_services) == 1:
                            service_name = member_services[0]
                    member = {
                        "member_key": member_key,
                        "name": instance_name,
                        "host": member_host_ip or member_hostname,
                        "reported_host": member_hostname,
                        "hostname": member_hostname,
                        "port": instance.port,
                        "role_type": role_type,
                        "health_status": "healthy" if status == "OPEN" else "warning",
                        "member_state": status,
                        "db_version": rac_member.get("version"),
                        "thread": rac_member.get("thread#"),
                        "instance_name": instance_name,
                        "db_unique_name": db_unique_name or database_name,
                        "oracle_connect_type": "sid" if connect_type == "sid" else "service_name",
                        "service_name": service_name,
                        "sid": sid,
                        "endpoint_unresolved": not bool(
                            (member_host_ip or member_hostname) and (service_name or sid)
                        ),
                    }
                    if not service_name and not sid and member_services:
                        member["service_name_candidates"] = member_services
                    members.append(member)

            destination_by_name = {
                str(row.get("db_unique_name") or "").upper(): row
                for row in dataguard_destinations
                if str(row.get("db_unique_name") or "").strip()
            }
            config_by_name = {str(row.get("db_unique_name") or "").upper(): row for row in dataguard_config}
            for remote_name in dataguard_names or list(destination_by_name):
                if not remote_name or remote_name.upper() == db_unique_name.upper():
                    continue
                member_key = remote_name.lower()
                if member_key in seen_members:
                    continue
                seen_members.add(member_key)
                destination = destination_by_name.get(remote_name.upper(), {})
                destination_status = str(destination.get("status") or "").upper()
                destination_role = str(config_by_name.get(remote_name.upper(), {}).get("dest_role") or "").upper()
                remote_role = "primary" if destination_role == "PRIMARY DATABASE" else (
                    "standby" if destination_role in {"PHYSICAL STANDBY", "LOGICAL STANDBY", "SNAPSHOT STANDBY", "FAR SYNC STANDBY"}
                    or str(destination.get("type") or "").upper() in {"PHYSICAL", "LOGICAL", "SNAPSHOT", "FAR SYNC"} else "unknown")
                gap_status = str(destination.get("gap_status") or "").upper()
                destination_metadata = _oracle_destination_metadata(
                    destination.get("destination"),
                    archive_destinations_by_id.get(str(destination.get("dest_id") or "")),
                )
                member = {
                    "member_key": member_key,
                    "name": remote_name,
                    "host": destination_metadata["host"],
                    "reported_host": destination_metadata["host"],
                    "port": destination_metadata["port"] or instance.port,
                    "role_type": remote_role,
                    "health_status": (
                        "healthy"
                        if destination_status in {"VALID", ""} and gap_status in {"NO GAP", "", "UNKNOWN"}
                        else "warning"
                    ),
                    "member_state": destination_status,
                    "database_mode": destination.get("database_mode"),
                    "recovery_mode": destination.get("recovery_mode"),
                    "gap_status": destination.get("gap_status"),
                    "db_unique_name": remote_name,
                    "oracle_connect_type": destination_metadata["oracle_connect_type"],
                    "service_name": destination_metadata["service_name"],
                    "sid": destination_metadata["sid"],
                    "connect_identifier": destination_metadata["connect_identifier"],
                    "endpoint_unresolved": destination_metadata["endpoint_unresolved"],
                }
                destination_type = str(destination.get("type") or "").upper()
                if (db_unique_name and destination.get("dest_id") is not None
                        and destination_status == "VALID"
                        and destination_type in {"PHYSICAL", "LOGICAL", "FAR SYNC", "SNAPSHOT"}):
                    member["transport_source_db_unique_name"] = db_unique_name
                    member["transport_destination_id"] = destination["dest_id"]
                if destination_metadata["endpoint_candidates"]:
                    member["endpoint_candidates"] = destination_metadata["endpoint_candidates"]
                members.append(member)

            group_names = sorted(set(name.lower() for name in dataguard_names if name))
            if architecture_type == "rac":
                topology_group = f"oracle:rac:{(db_unique_name or database_name or instance.host).lower()}"
                local_member_key = f"{db_unique_name or database_name}:{local_instance_name or instance.instance_name}".lower()
            elif architecture_type == "data_guard":
                group_anchor = ":".join(group_names) or (db_unique_name or database_name or instance.host).lower()
                topology_group = f"oracle:data_guard:{group_anchor}"
                local_member_key = (db_unique_name or database_name or instance.instance_name).lower()
            else:
                topology_group = f"oracle:standalone:{(db_unique_name or database_name or instance.host).lower()}"
                local_member_key = (db_unique_name or database_name or instance.instance_name).lower()

            detail["version"] = local_version
            architecture = {
                "architecture_type": architecture_type,
                "role_type": role_type,
                "topology_group": topology_group,
                "cluster_name": instance.system_name or db_unique_name or database_name or instance.instance_name,
                "member_key": local_member_key,
                "db_version": local_version,
                "database_name": database_name,
                "db_unique_name": db_unique_name,
                "instance_name": local_instance_name,
                "hostname": local_host_name,
                "host_ip": local_host_ip,
                "connection_endpoint": f"{instance.host}:{instance.port}",
                "instance_count": instance_count,
                "database_role": database_role,
                "open_mode": open_mode,
                "protection_mode": protection_mode,
                "has_dataguard": has_dataguard,
                "members": members,
            }
            local_parents = {
                str(row.get("parent_dbun") or "").strip()
                for row in dataguard_config
                if str(row.get("db_unique_name") or "").casefold() == db_unique_name.casefold()
                and str(row.get("parent_dbun") or "").strip().upper() not in {"", "NONE", "NULL", "UNKNOWN"}
            }
            if len(local_parents) == 1:
                architecture["upstream_db_unique_name"] = local_parents.pop()
            if dataguard_stats:
                architecture.update({
                    "transport_lag": transport_lag,
                    "apply_lag": apply_lag,
                    "apply_finish_time": apply_finish_time,
                    "transport_lag_seconds": transport_lag_seconds,
                    "apply_lag_seconds": apply_lag_seconds,
                    "replication_delay_seconds": replication_delay_seconds,
                    "lag_time_computed": (
                        apply_lag_row.get("time_computed")
                        or transport_lag_row.get("time_computed")
                    ),
                    "lag_datum_time": (
                        apply_lag_row.get("datum_time")
                        or transport_lag_row.get("datum_time")
                    ),
                })
            detail["architecture"] = architecture
        else:
            detail["warnings"].append(
                "业务用户连接成功。程序将按业务账号权限降级采集，表空间、ASM、FRA、Data Guard 等 DBA 视图可能不可用。"
            )
            try:
                cursor.execute("SELECT USER FROM dual")
                row = cursor.fetchone()
                detail["current_user"] = row[0] if row else instance.username
            except Exception:
                detail["current_user"] = instance.username
        return detail
