import logging

from app.models.db_instance import DBInstance
from app.collectors.mysql.mysql_connection import mysql_connection

logger = logging.getLogger("app.services.mysql_capacity")


def get_schema_sizes(instance: DBInstance) -> list:
    sql = """
    SELECT LOWER(t.table_schema) AS table_schema,
           ROUND(SUM(t.data_length + t.index_length) / 1024 / 1024, 2) AS total_mb,
           ROUND(SUM(t.data_length) / 1024 / 1024, 2) AS data_mb,
           ROUND(SUM(t.index_length) / 1024 / 1024, 2) AS index_mb,
           COUNT(*) AS table_count
    FROM information_schema.tables t
    WHERE t.table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
    GROUP BY t.table_schema
    ORDER BY total_mb DESC
    """
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
    except Exception as e:
        logger.error("get_schema_sizes failed: %s", e)
        return []


def get_large_tables(instance: DBInstance, limit: int = 50) -> list:
    sql = f"""
    SELECT LOWER(t.table_schema) AS table_schema,
           t.table_name AS table_name,
           t.engine AS engine,
           t.table_rows AS table_rows,
           ROUND(t.data_length / 1024 / 1024, 2) AS data_mb,
           ROUND(t.index_length / 1024 / 1024, 2) AS index_mb,
           ROUND((t.data_length + t.index_length) / 1024 / 1024, 2) AS total_mb,
           ROUND(t.data_free / 1024 / 1024, 2) AS free_mb,
           t.update_time
    FROM information_schema.tables t
    WHERE t.table_schema NOT IN ('mysql','information_schema','performance_schema','sys')
      AND t.table_type = 'BASE TABLE'
    ORDER BY total_mb DESC
    LIMIT {limit}
    """
    try:
        with mysql_connection(instance) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
    except Exception as e:
        logger.error("get_large_tables failed: %s", e)
        return []
