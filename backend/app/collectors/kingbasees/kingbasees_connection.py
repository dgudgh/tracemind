"""人大金仓 KingbaseES 连接与连通性测试。

驱动：ksycopg2（金仓随数据库发布的 libkci 绑定，psycopg2 兼容）。
**不得改用 psycopg2/psycopg 冒充**——EP-R03 要求金仓有独立驱动身份，
协议兼容不等于产品身份成立；缺 ksycopg2 时直接报可操作的错误。

查询超时：通过 libpq 标准 `options='-c statement_timeout=<ms>'` 下发，
与 postgresql_connection 同一做法。该参数在 libpq 上是标准行为，
但**仍需在目标 KingbaseES 版本上确认**——确认记录写入本文件注释，不改口。

身份口径沿用 engine_providers/kingbasees.py：
  version() 判产品（含 kingbase），sys_control_system().system_identifier 判实例。
金仓把 PostgreSQL 的 pg_ 系统目录统一改名为 sys_ 前缀，故用 sys_control_system()。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, rows_to_dicts
from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.kingbasees.connection")

QUERY_TIMEOUT_SOURCE = "libpq_options"
"""libpq 标准参数下发语句级超时；目标版本需确认（未确认前不写 ready）。"""

IDENTITY_TOKENS = ("kingbase",)

IDENTITY_SQL = "SELECT system_identifier::text AS system_identifier FROM sys_control_system()"

VERSION_SQL = "SELECT version() AS version"


def _load_driver():
    try:
        import ksycopg2
    except ImportError as exc:
        raise RuntimeError(
            "ksycopg2 驱动未安装：KingbaseES 连接需要随数据库发布的 ksycopg2（libkci），"
            "不能用 psycopg2/psycopg 代替，否则无法区分金仓与 PostgreSQL 的产品身份"
        ) from exc
    return ksycopg2


@contextmanager
def kingbasees_connection(instance: DBInstance, *, statement_timeout_ms: int | None = None):
    ksycopg2 = _load_driver()
    timeout_ms = COLLECTION_QUERY_TIMEOUT_SECONDS * 1000 if statement_timeout_ms is None else statement_timeout_ms
    kwargs = {
        "host": instance.host,
        "port": int(instance.port),
        "user": instance.username,
        "password": decrypt_password(instance.password_cipher),
        "dbname": instance.database_name or "test",
        "connect_timeout": 5,
        "options": f"-c statement_timeout={max(1, int(timeout_ms))}",
    }
    if instance.sslmode:
        kwargs["sslmode"] = instance.sslmode
    logger.debug(
        "KingbaseES 连接 | host=%s:%s | user=%s | db=%s",
        instance.host, instance.port, instance.username, kwargs["dbname"],
    )
    conn = ksycopg2.connect(**kwargs)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            logger.debug("KingbaseES 连接关闭失败", exc_info=True)


def _fetch_one(cursor, sql: str) -> dict:
    cursor.execute(sql)
    rows = rows_to_dicts(cursor, cursor.fetchall())
    return rows[0] if rows else {}


def test_kingbasees_connection(instance: DBInstance) -> dict:
    warnings: list[str] = []
    version = ""
    identity: dict = {}
    with kingbasees_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            row = _fetch_one(cursor, VERSION_SQL)
            version = str(row.get("version") or "")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"version() 读取失败：{exc}")
        else:
            if not any(token in version.lower() for token in IDENTITY_TOKENS):
                # 典型情形：拿 psycopg2 连到了真 PostgreSQL。必须明确拒绝，不能当成金仓。
                warnings.append("版本横幅不含 kingbase，产品身份未确认（可能连到了 PostgreSQL）")
        try:
            identity = _fetch_one(cursor, IDENTITY_SQL)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"实例身份读取失败（sys_control_system 不存在或无权限）：{exc}"
            )
        else:
            blank = [k for k, v in identity.items() if v is None or str(v).strip() in ("", "0")]
            if blank:
                warnings.append("实例身份字段为空或为 0：" + ", ".join(sorted(blank)))
                identity = {}

    identity_ok = bool(identity.get("system_identifier"))
    return {
        "host": instance.host,
        "port": instance.port,
        "version": version or None,
        "account_type": instance.account_type or "system",
        "capability": "read_only_pending_certification" if identity_ok else "limited",
        "identity": identity or None,
        "identity_verified": identity_ok,
        "query_timeout_source": QUERY_TIMEOUT_SOURCE,
        "collection_budget_seconds": COLLECTION_QUERY_TIMEOUT_SECONDS,
        "warnings": warnings,
    }
