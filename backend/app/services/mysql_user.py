import logging

from app.models.db_instance import DBInstance
from app.collectors.mysql.mysql_connection import mysql_connection

logger = logging.getLogger("app.services.mysql_user")


def get_users(instance: DBInstance) -> list:
    sql = """
    SELECT user, host, plugin, account_locked, password_expired
    FROM mysql.user
    ORDER BY user, host
    """
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
    except Exception as e:
        logger.error("get_users failed: %s", e)
        return []


def get_user_grants(instance: DBInstance, user: str, host: str) -> list:
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW GRANTS FOR %s@%s", (user, host))
                return [row.get("Grants for {0}@{1}".format(user, host), "") or row.get(list(row.keys())[0], "") for row in cur.fetchall()]
    except Exception as e:
        logger.error("get_user_grants failed: %s", e)
        return [f"查询失败: {e}"]
