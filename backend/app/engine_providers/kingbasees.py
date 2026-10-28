"""KingbaseES source candidate. Never loads a PostgreSQL driver as a substitute.

身份链说明：KingbaseES 把 PostgreSQL 的 pg_ 系统目录统一改名为 sys_ 前缀
（本文件既有的 sys_stat_activity / sys_locks 即同一约定），因此实例身份取
sys_control_system().system_identifier —— 与 postgresql provider 用
pg_control_system() 取 system_identifier 完全同构。

在真库认证之前，这份 SQL 只保证「结构契约成立」，不表示厂商版本已确认：
  * identity_tokens = ('kingbase',) 保证 PostgreSQL 服务器不会被误认成金仓；
  * 若 sys_control_system() 在目标版本不存在或无权限，base.invoke 会把它映射为
    identity_unavailable（fail-closed），而不是放行任何一次读。
真机取证要求：在目标 KingbaseES 上确认该视图存在、字段名与权限，再据实更新本声明。
"""
from .base import ReadProvider

class Provider(ReadProvider):
    engine_id, driver = 'kingbasees', 'ksycopg2'
    identity_tokens = ('kingbase',)
    identity_sql = 'SELECT system_identifier::text AS system_identifier FROM sys_control_system()'
    identity_fields = ('system_identifier',)
    queries = {
        'identify': 'SELECT version() AS version',
        'version': 'SELECT version() AS version, current_database() AS database_name, current_user AS current_user',
        'sessions': 'SELECT COUNT(*) AS session_count FROM sys_stat_activity',
        'locks': 'SELECT COUNT(*) AS lock_count FROM sys_locks',
        'metrics': 'SELECT COUNT(*) AS session_count FROM sys_stat_activity',
    }
