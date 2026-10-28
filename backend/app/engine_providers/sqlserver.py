from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'sqlserver', 'pymssql'
    identity_tokens = ('microsoft sql server',)
    identity_sql = "SELECT CONVERT(VARCHAR(36), database_guid) AS database_guid, CONVERT(NVARCHAR(256), SERVERPROPERTY('ServerName')) AS server_name FROM sys.database_recovery_status WHERE database_id = DB_ID()"
    identity_fields = ('database_guid', 'server_name')
    queries = {
        'identify': 'SELECT @@VERSION AS version',
        'version': 'SELECT @@VERSION AS version, DB_NAME() AS database_name, SYSTEM_USER AS current_user',
        'sessions': 'SELECT COUNT(*) AS session_count FROM sys.dm_exec_sessions',
        'locks': 'SELECT COUNT(*) AS lock_count FROM sys.dm_tran_locks',
        'metrics': 'SELECT COUNT(*) AS session_count FROM sys.dm_exec_sessions',
    }
