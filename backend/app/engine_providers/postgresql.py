from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'postgresql', 'psycopg2'
    identity_tokens = ('postgresql',)
    excluded_tokens = ('kingbase', 'opengauss', 'greenplum', 'cockroach', 'yugabyte')
    identity_sql = 'SELECT system_identifier::text AS system_identifier FROM pg_control_system()'
    identity_fields = ('system_identifier',)
    queries = {
        'identify': 'SELECT version() AS version',
        'version': 'SELECT version() AS version, current_database() AS database_name, current_user AS current_user',
        'sessions': 'SELECT COUNT(*) AS session_count FROM pg_stat_activity',
        'locks': 'SELECT COUNT(*) AS lock_count FROM pg_locks',
        'metrics': 'SELECT COUNT(*) AS session_count FROM pg_stat_activity',
    }
