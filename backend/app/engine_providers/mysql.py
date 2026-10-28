from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'mysql', 'pymysql'
    identity_tokens = ('mysql',)
    excluded_tokens = ('mariadb', 'tidb', 'oceanbase', 'percona')
    identity_sql = 'SELECT @@server_uuid AS server_uuid'
    identity_fields = ('server_uuid',)
    queries = {
        'identify': "SELECT CONCAT(@@version, ' ', @@version_comment) AS version",
        'version': 'SELECT @@hostname AS hostname, @@port AS port, @@version AS version',
        'sessions': 'SELECT COUNT(*) AS session_count FROM information_schema.processlist',
        'locks': 'SELECT COUNT(*) AS lock_count FROM performance_schema.data_locks',
        'metrics': 'SELECT COUNT(*) AS session_count FROM information_schema.processlist',
    }
