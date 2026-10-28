from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'oracle', 'oracledb'
    identity_tokens = ('oracle database',)
    identity_sql = 'SELECT TO_CHAR(d.dbid) AS dbid, d.db_unique_name AS db_unique_name, i.instance_name AS instance_name FROM v$database d CROSS JOIN v$instance i'
    identity_fields = ('dbid', 'db_unique_name', 'instance_name')
    queries = {
        'identify': "SELECT banner AS version FROM v$version WHERE banner LIKE 'Oracle Database%' AND ROWNUM = 1",
        'version': 'SELECT instance_name, host_name, version, status FROM v$instance',
        'sessions': 'SELECT COUNT(*) AS session_count FROM v$session',
        'locks': 'SELECT COUNT(*) AS lock_count FROM v$lock',
        'metrics': 'SELECT COUNT(*) AS session_count FROM v$session',
    }
