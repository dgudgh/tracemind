"""DM8 source candidate. Requires dmPython and target-specific certification."""
from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'dm', 'dmPython'
    identity_tokens = ('dm database server',)
    identity_sql = 'SELECT r.DB_MAGIC AS db_magic, i.INSTANCE_NAME AS instance_name FROM V$RLOG r CROSS JOIN V$INSTANCE i'
    identity_fields = ('db_magic', 'instance_name')
    queries = {
        'identify': "SELECT BANNER AS version FROM V$VERSION WHERE BANNER LIKE 'DM Database Server%'",
        'version': 'SELECT INSTANCE_NAME, HOST_NAME, SVR_VERSION, DB_VERSION, STATUS$, MODE$ FROM V$INSTANCE',
        'sessions': 'SELECT COUNT(*) AS session_count FROM V$SESSIONS',
        'locks': 'SELECT COUNT(*) AS lock_count FROM V$LOCK',
        'metrics': 'SELECT COUNT(*) AS session_count FROM V$SESSIONS',
    }
