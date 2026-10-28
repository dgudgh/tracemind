import logging

from app.collectors.mysql.mysql_connection import mysql_connection
from app.collectors.utils import COLLECTION_QUERY_TIMEOUT_SECONDS, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.collectors.mysql.health")


MYSQL_QUERIES = {
    "version_info": """
        SELECT @@hostname AS hostname,
               @@version AS version,
               @@port AS port,
               @@server_id AS server_id,
               @@read_only AS read_only,
               @@super_read_only AS super_read_only
    """,
    "database_size": """
        SELECT table_schema,
               ROUND(SUM(data_length + index_length) / 1024 / 1024 / 1024, 2) AS size_gb
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
        GROUP BY table_schema
        ORDER BY size_gb DESC
    """,
    "large_tables": """
        SELECT table_schema, table_name, table_rows,
               ROUND(data_length / 1024 / 1024 / 1024, 2) AS data_gb,
               ROUND(index_length / 1024 / 1024 / 1024, 2) AS index_gb,
               ROUND((data_length + index_length) / 1024 / 1024 / 1024, 2) AS total_gb
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
        ORDER BY data_length + index_length DESC
        LIMIT 20
    """,
    "fragmentation": """
        SELECT table_schema, table_name, engine, table_rows,
               ROUND(data_length / 1024 / 1024, 2) AS data_mb,
               ROUND(index_length / 1024 / 1024, 2) AS index_mb,
               ROUND(data_free / 1024 / 1024, 2) AS free_mb,
               ROUND(data_free / NULLIF(data_length + index_length + data_free, 0) * 100, 2) AS free_pct
        FROM information_schema.tables
        WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
          AND data_free > 0
        ORDER BY data_free DESC
        LIMIT 50
    """,
    "slow_sql": """
        SELECT DIGEST, SCHEMA_NAME, COUNT_STAR,
               ROUND(SUM_TIMER_WAIT / 1000000000000, 2) AS total_seconds,
               ROUND(AVG_TIMER_WAIT / 1000000000000, 4) AS avg_seconds,
               SUM_ROWS_EXAMINED, SUM_ROWS_SENT, DIGEST_TEXT
        FROM performance_schema.events_statements_summary_by_digest
        WHERE SCHEMA_NAME NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
        ORDER BY SUM_TIMER_WAIT DESC
        LIMIT 20
    """,
    "locks": """
        SELECT 1 AS compatibility_placeholder WHERE 1 = 0
    """,
    "innodb_buffer_pool": """
        SHOW GLOBAL STATUS WHERE Variable_name IN (
            'Innodb_buffer_pool_reads',
            'Innodb_buffer_pool_read_requests',
            'Innodb_buffer_pool_pages_free',
            'Innodb_buffer_pool_pages_total'
        )
    """,
    "binlog": """
        SHOW VARIABLES WHERE Variable_name IN (
            'log_bin',
            'binlog_expire_logs_seconds',
            'expire_logs_days',
            'sync_binlog'
        )
    """,
}


class MySQLHealthCollector:
    def __init__(self, instance: DBInstance):
        self.instance = instance

    def collect(self) -> dict[str, list[dict]]:
        metrics: dict[str, list[dict]] = {}
        account_type = self.instance.account_type or "system"
        metrics["account_capability"] = [{
            "account_type": account_type,
            "status": "warning" if account_type == "business" else "normal",
            "message": (
                "业务用户模式：优先采集当前账号可见的库表、容量和 SQL 摘要，复制/Binlog/全局状态可能降级。"
                if account_type == "business"
                else "系统用户模式：执行完整 MySQL 巡检，权限不足的单项会单独标记。"
            ),
        }]
        logger.info("MySQL 采集开始 | instance=%s | account_type=%s", self.instance.instance_name, account_type)
        with mysql_connection(self.instance, read_timeout=COLLECTION_QUERY_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cursor:
                try:
                    metrics["connection_status"] = self._connection_status(cursor)
                except Exception as exc:
                    metrics["connection_status"] = [{"error": str(exc)}]
                metrics["replication"] = [] if account_type == "business" else self._replication_status(cursor)
                for name, sql in MYSQL_QUERIES.items():
                    if account_type == "business" and name in {"binlog", "innodb_buffer_pool"}:
                        metrics[name] = [{
                            "warning": "业务用户模式已跳过该系统级采集项，请使用 System 用户获取完整结果。"
                        }]
                        continue
                    if name == "locks":
                        metrics[name] = self._lock_waits(cursor)
                        logger.debug("MySQL 采集项完成 | %s | 行数=%d", name, len(metrics[name]))
                        continue
                    try:
                        cursor.execute(sql)
                        metrics[name] = safe_rows(_lower_keys(list(cursor.fetchall())))
                        logger.debug("MySQL 采集项完成 | %s | 行数=%d", name, len(metrics[name]))
                    except Exception as exc:
                        metrics[name] = [{"error": str(exc)}]
                        logger.warning("MySQL 采集项失败 | %s | error=%s", name, exc)
        logger.info("MySQL 采集完成 | instance=%s | 采集项=%d", self.instance.instance_name, len(metrics))
        return metrics

    def _connection_status(self, cursor) -> list[dict]:
        result = {}
        for sql in (
            "SHOW GLOBAL STATUS LIKE 'Threads_connected'",
            "SHOW GLOBAL STATUS LIKE 'Max_used_connections'",
            "SHOW VARIABLES LIKE 'max_connections'",
        ):
            cursor.execute(sql)
            row = cursor.fetchone()
            if row:
                key = row.get("Variable_name") or row.get("variable_name") or row.get("VARIABLE_NAME")
                value = row.get("Value") or row.get("value") or row.get("VARIABLE_VALUE")
                if key:
                    result[key] = value
        return [result]

    def _replication_status(self, cursor) -> list[dict]:
        for sql in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                cursor.execute(sql)
                row = cursor.fetchone()
                return safe_rows([row] if row else [])
            except Exception:
                continue
        return []

    def _table_exists(self, cursor, schema: str, table_name: str) -> bool:
        try:
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND UPPER(table_name) = UPPER(%s)
                """,
                (schema, table_name),
            )
            row = cursor.fetchone() or {}
            return int(row.get("cnt") or row.get("COUNT(*)") or 0) > 0
        except Exception:
            return False

    def _lock_waits(self, cursor) -> list[dict]:
        if self._table_exists(cursor, "performance_schema", "data_lock_waits") and self._table_exists(cursor, "performance_schema", "data_locks"):
            cursor.execute(
                """
                SELECT
                  w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx_id,
                  wr.trx_mysql_thread_id AS waiting_thread,
                  wr.trx_query AS waiting_query,
                  w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx_id,
                  br.trx_mysql_thread_id AS blocking_thread,
                  br.trx_query AS blocking_query,
                  r.OBJECT_SCHEMA AS object_schema,
                  r.OBJECT_NAME AS object_name,
                  r.INDEX_NAME AS index_name,
                  r.LOCK_TYPE AS waiting_lock_type,
                  r.LOCK_MODE AS waiting_lock_mode,
                  b.LOCK_MODE AS blocking_lock_mode
                FROM performance_schema.data_lock_waits w
                LEFT JOIN performance_schema.data_locks r
                  ON w.REQUESTING_ENGINE_LOCK_ID = r.ENGINE_LOCK_ID
                LEFT JOIN performance_schema.data_locks b
                  ON w.BLOCKING_ENGINE_LOCK_ID = b.ENGINE_LOCK_ID
                LEFT JOIN information_schema.innodb_trx wr
                  ON wr.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
                LEFT JOIN information_schema.innodb_trx br
                  ON br.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
                """
            )
            return safe_rows(_lower_keys(list(cursor.fetchall())))

        if self._table_exists(cursor, "information_schema", "innodb_lock_waits"):
            cursor.execute(
                """
                SELECT r.trx_id AS waiting_trx_id,
                       r.trx_mysql_thread_id AS waiting_thread,
                       r.trx_query AS waiting_query,
                       b.trx_id AS blocking_trx_id,
                       b.trx_mysql_thread_id AS blocking_thread,
                       b.trx_query AS blocking_query
                FROM information_schema.innodb_lock_waits w
                JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id
                JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id
                """
            )
            return safe_rows(_lower_keys(list(cursor.fetchall())))

        logger.info("MySQL lock wait views are unavailable; returning empty lock wait list")
        return []


def _lower_keys(rows: list[dict]) -> list[dict]:
    return [{str(key).lower(): value for key, value in row.items()} for row in rows]
