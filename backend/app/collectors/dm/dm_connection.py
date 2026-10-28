"""达梦 DM8 连接与连通性测试。

驱动：dmPython（随 DM 客户端安装，不是公共 pip 包）。缺驱动时给可操作的错误，
而不是让 ImportError 冒泡成 500 —— 与 mysql/postgresql/sqlserver 同口径。

**查询超时预算（如实声明，不假装有）**：`dmPython` 未提供经过验证的语句级超时参数，
本模块因此声明 `QUERY_TIMEOUT_SUPPORTED = False`。调用方必须以 host deadline / 进程级
超时兜底。禁止在这里写一个不生效的 timeout= 参数来"看起来有预算"。
真机验收时必须确认目标版本的 dmPython 是否有可用超时手段，确认后再改这个常量。

身份口径沿用 engine_providers/dm.py：
  V$VERSION.BANNER 判产品，V$RLOG.DB_MAGIC + V$INSTANCE.INSTANCE_NAME 判实例。
两者都必须有值：banner 对了但实例身份取不到时，只报告"身份不足"，不放行后续能力。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, rows_to_dicts
from app.core.crypto import decrypt_password
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.dm.connection")

QUERY_TIMEOUT_SUPPORTED = False
"""dmPython 无经验证的语句级超时；保持 False 以免调用方误以为读操作有界。"""

IDENTITY_TOKENS = ("dm database server",)

IDENTITY_SQL = (
    "SELECT r.DB_MAGIC AS db_magic, i.INSTANCE_NAME AS instance_name "
    "FROM V$RLOG r CROSS JOIN V$INSTANCE i"
)

VERSION_SQL = "SELECT BANNER AS version FROM V$VERSION WHERE BANNER LIKE 'DM Database Server%'"


def _load_driver():
    try:
        import dmPython
    except ImportError as exc:
        raise RuntimeError(
            "dmPython 驱动未安装：达梦连接需要随 DM 客户端安装的 dmPython，"
            "不能改用 oracledb/psycopg2 等协议兼容驱动代替"
        ) from exc
    return dmPython


@contextmanager
def dm_connection(instance: DBInstance):
    dmPython = _load_driver()
    logger.debug(
        "DM 连接 | host=%s:%s | user=%s", instance.host, instance.port, instance.username
    )
    conn = dmPython.connect(
        user=instance.username,
        password=decrypt_password(instance.password_cipher),
        server=instance.host,
        port=int(instance.port),
    )
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败不能覆盖上面的真实错误
            logger.debug("DM 连接关闭失败", exc_info=True)


def _fetch_one(cursor, sql: str) -> dict:
    cursor.execute(sql)
    rows = rows_to_dicts(cursor, cursor.fetchall())
    return rows[0] if rows else {}


def test_dm_connection(instance: DBInstance) -> dict:
    """返回结构与其它引擎一致：host/port/version/capability/warnings。

    banner 不匹配时 capability 降级为 limited 并给出 warning，而不是抛异常：
    连通性成立与身份成立是两件事，界面需要能分开显示。
    """
    warnings: list[str] = []
    version = ""
    identity: dict = {}
    with dm_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            row = _fetch_one(cursor, VERSION_SQL)
            version = str(row.get("version") or "")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"V$VERSION 读取失败：{exc}")
        else:
            if not any(token in version.lower() for token in IDENTITY_TOKENS):
                warnings.append("版本横幅不匹配达梦 DM Database Server，引擎身份未确认")
        try:
            identity = _fetch_one(cursor, IDENTITY_SQL)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"实例身份读取失败（V$RLOG/V$INSTANCE 权限不足或不兼容）：{exc}")
        else:
            blank = [k for k, v in identity.items() if v is None or str(v).strip() in ("", "0")]
            if blank:
                warnings.append("实例身份字段为空或为 0：" + ", ".join(sorted(blank)))
                identity = {}

    identity_ok = bool(identity) and "db_magic" in identity and "instance_name" in identity
    return {
        "host": instance.host,
        "port": instance.port,
        "version": version or None,
        "account_type": instance.account_type or "system",
        "capability": "read_only_pending_certification" if identity_ok else "limited",
        "identity": identity or None,
        "identity_verified": identity_ok,
        "query_timeout_supported": QUERY_TIMEOUT_SUPPORTED,
        "collection_budget_seconds": COLLECTION_QUERY_TIMEOUT_SECONDS,
        "warnings": warnings,
    }
