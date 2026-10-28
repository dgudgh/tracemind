import logging

from app.collectors.oracle.oracle_connection import oracle_connection
from app.collectors.utils import rows_to_dicts, safe_rows
from app.models.db_instance import DBInstance

logger = logging.getLogger("app.services.user_mgmt")


def list_users(instance: DBInstance) -> dict:
    logger.info("查询用户列表 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, account_status, expiry_date, default_tablespace, temporary_tablespace, "
            "created, profile, authentication_type FROM dba_users ORDER BY username"
        )
        users = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    return {"users": users}


def list_roles(instance: DBInstance) -> dict:
    logger.info("查询角色列表 | instance=%s", instance.instance_name)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role, password_required FROM dba_roles ORDER BY role")
        roles = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
    return {"roles": roles}


def list_user_privs(instance: DBInstance, username: str) -> dict:
    logger.info("查询用户权限 | instance=%s | user=%s", instance.instance_name, username)
    with oracle_connection(instance) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT privilege, admin_option FROM dba_sys_privs WHERE grantee = :1 ORDER BY privilege",
                [username.upper()]
            )
            sys_privs = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            sys_privs = []
        try:
            cursor.execute(
                "SELECT granted_role, admin_option, default_role FROM dba_role_privs WHERE grantee = :1 ORDER BY granted_role",
                [username.upper()]
            )
            role_privs = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            role_privs = []
        try:
            cursor.execute(
                "SELECT owner, table_name, privilege, grantable FROM dba_tab_privs WHERE grantee = :1 ORDER BY owner, table_name",
                [username.upper()]
            )
            obj_privs = safe_rows(rows_to_dicts(cursor, cursor.fetchall()))
        except Exception:
            obj_privs = []
    return {"username": username, "system_privileges": sys_privs, "role_privileges": role_privs, "object_privileges": obj_privs}


def generate_create_user_script(username: str, password: str, default_tablespace: str = "USERS", temp_tablespace: str = "TEMP") -> dict:
    sql = (
        f"CREATE USER {username} IDENTIFIED BY \"{password}\"\n"
        f"DEFAULT TABLESPACE {default_tablespace}\n"
        f"TEMPORARY TABLESPACE {temp_tablespace}\n"
        f"QUOTA UNLIMITED ON {default_tablespace};\n"
        f"GRANT CREATE SESSION TO {username};"
    )
    return {"status": "plan_only", "message": "已生成创建用户脚本。", "sql": sql, "warnings": ["请确认表空间存在。", "建议使用强密码。"]}


def generate_drop_user_script(username: str, cascade: bool = False) -> dict:
    cas = " CASCADE" if cascade else ""
    sql = f"DROP USER {username}{cas};"
    return {"status": "plan_only", "message": "已生成删除用户脚本。", "sql": sql, "warnings": ["CASCADE 会删除该用户所有对象！", "请确认用户已无业务使用。"]}


def generate_lock_user_script(username: str, action: str = "lock") -> dict:
    act = "LOCK" if action == "lock" else "UNLOCK"
    sql = f"ALTER USER {username} ACCOUNT {act};"
    return {"status": "plan_only", "message": f"已生成{act}用户脚本。", "sql": sql, "warnings": []}


def generate_password_script(username: str, new_password: str) -> dict:
    sql = f"ALTER USER {username} IDENTIFIED BY \"{new_password}\";"
    return {"status": "plan_only", "message": "已生成修改密码脚本。", "sql": sql, "warnings": ["请使用强密码。", "修改密码会影响正在使用的连接。"]}


def generate_grant_script(username: str, privileges: str, on_object: str | None = None) -> dict:
    if on_object:
        sql = f"GRANT {privileges} ON {on_object} TO {username};"
    else:
        sql = f"GRANT {privileges} TO {username};"
    return {"status": "plan_only", "message": "已生成授权脚本。", "sql": sql, "warnings": ["请遵循最小权限原则。"]}


def generate_revoke_script(username: str, privileges: str, on_object: str | None = None) -> dict:
    if on_object:
        sql = f"REVOKE {privileges} ON {on_object} FROM {username};"
    else:
        sql = f"REVOKE {privileges} FROM {username};"
    return {"status": "plan_only", "message": "已生成回收权限脚本。", "sql": sql, "warnings": ["回收权限可能影响业务运行。"]}
