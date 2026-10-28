import logging
import os
from urllib.parse import quote_plus

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text, inspect

from app.core.database_options import database_engine_options, redact_database_error

logger = logging.getLogger("app.api.setup")

router = APIRouter(prefix="/setup", tags=["安装引导"])


def _get_app_dir():
    import sys
    if getattr(sys, "frozen", False):
        from app.runtime.paths import ensure_frozen_runtime_root

        return str(ensure_frozen_runtime_root())
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


SETUP_FLAG = os.path.join(_get_app_dir(), ".env")


class MySQLConfig(BaseModel):
    host: str = Field(..., min_length=1, max_length=253)
    port: int = Field(3306, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=256)
    database: str = Field(..., pattern=r"^[A-Za-z0-9_]{1,64}$")
    admin_username: str = Field(..., min_length=1, max_length=80)
    admin_password: str = Field(..., min_length=8, max_length=256)


def is_configured() -> bool:
    return os.path.exists(SETUP_FLAG)


@router.get("/status")
def setup_status():
    return {"configured": is_configured()}


@router.get("/license")
def get_license_status():
    from app.license.verify import get_license_info
    return get_license_info()


@router.post("/license/upload")
def upload_license(data: dict):
    import json
    license_content = data.get("content", "")
    if not license_content:
        return {"ok": False, "message": "授权内容为空"}
    try:
        license_data = json.loads(license_content)
        required_fields = ["machine_code", "expire_date", "customer", "signature"]
        for f in required_fields:
            if f not in license_data:
                return {"ok": False, "message": f"授权文件缺少字段: {f}"}
    except json.JSONDecodeError:
        return {"ok": False, "message": "授权文件格式错误，不是有效的 JSON"}

    from app.license.verify import save_license_to_db
    save_result = save_license_to_db(license_data)
    if not save_result.get("ok"):
        return {"ok": False, "message": f"保存授权失败: {save_result.get('message')}"}

    from app.license.verify import get_license_info
    info = get_license_info()
    if info.get("valid"):
        return {"ok": True, "message": f"授权成功！客户: {info.get('customer')}，有效期至 {info.get('expire_date')}"}
    return {"ok": False, "message": f"授权校验失败: {info.get('reason')}"}


@router.post("/test-connection")
def test_mysql_connection(cfg: MySQLConfig):
    url = f"mysql+pymysql://{quote_plus(cfg.username)}:{quote_plus(cfg.password)}@{cfg.host}:{cfg.port}/"
    eng = None
    try:
        eng = create_engine(url, **database_engine_options(url, sql_echo=False))
        with eng.connect() as conn:
            result = conn.execute(text("SELECT VERSION()"))
            version = result.scalar()
        databases = []
        with eng.connect() as conn:
            result = conn.execute(text("SHOW DATABASES"))
            databases = [r[0] for r in result.fetchall()]
        db_exists = cfg.database in databases
        return {
            "ok": True,
            "message": f"连接成功 (MySQL {version})",
            "version": version,
            "database_exists": db_exists,
            "databases": databases,
        }
    except Exception as e:
        return {
            "ok": False,
            "message": f"连接失败: {redact_database_error(e)}",
            "version": None,
            "database_exists": False,
            "databases": [],
        }
    finally:
        if eng is not None:
            eng.dispose()


@router.post("/init-database")
def init_database(cfg: MySQLConfig):
    from fastapi import HTTPException
    from app.core.security import password_complexity_issues

    issues = password_complexity_issues(cfg.admin_password)
    if issues:
        raise HTTPException(status_code=400, detail="；".join(issues))
    url = f"mysql+pymysql://{quote_plus(cfg.username)}:{quote_plus(cfg.password)}@{cfg.host}:{cfg.port}/"
    eng = None
    try:
        eng = create_engine(url, **database_engine_options(url, sql_echo=False))
        with eng.connect() as conn:
            result = conn.execute(text("SHOW DATABASES"))
            databases = [r[0] for r in result.fetchall()]
            if cfg.database not in databases:
                conn.execute(text(f"CREATE DATABASE `{cfg.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
                conn.commit()
                logger.info("数据库 %s 已创建", cfg.database)
    except Exception as e:
        return {"ok": False, "message": f"创建数据库失败: {redact_database_error(e)}"}
    finally:
        if eng is not None:
            eng.dispose()

    db_url = f"mysql+pymysql://{quote_plus(cfg.username)}:{quote_plus(cfg.password)}@{cfg.host}:{cfg.port}/{cfg.database}"
    db_eng = None
    try:
        from app.core.database import initialize_schema
        from app.migrations.locking import schema_lock
        from app.services.sqlite_init_service import _init_admin_user, _init_module_config
        from sqlalchemy.orm import sessionmaker

        db_eng = create_engine(db_url, **database_engine_options(db_url, sql_echo=False))
        with schema_lock(db_eng) as connection:
            if inspect(connection).get_table_names():
                raise RuntimeError(
                    "The target database already contains tables. Installation cannot upgrade an existing "
                    "database; use the release migration CLI after a verified backup and service quiescence."
                )
            initialize_schema(connection, allow_upgrade=True)
        Session = sessionmaker(autocommit=False, autoflush=False, bind=db_eng)
        db = Session()
        try:
            _init_module_config(db)
            _init_admin_user(db, cfg.admin_username, cfg.admin_password)
            from app.services.inspection.policy_service import ensure_default_policies
            ensure_default_policies(db, commit=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "message": f"初始化表结构失败: {redact_database_error(e)}"}
    finally:
        if db_eng is not None:
            db_eng.dispose()

    try:
        safe_url = f"mysql+pymysql://{quote_plus(cfg.username)}:{quote_plus(cfg.password)}@{cfg.host}:{cfg.port}/{cfg.database}"
        env_content = (
            f"DATABASE_URL={safe_url}\n"
            f"DB_TYPE=mysql\n"
            f"APP_MODE=production\n"
            f"DEBUG=false\n"
            f"SECRET_KEY=mesguard-prod-{os.urandom(8).hex()}\n"
        )
        with open(SETUP_FLAG, "w", encoding="utf-8") as f:
            f.write(env_content)
        logger.info("配置已写入 %s", SETUP_FLAG)
    except Exception as e:
        return {"ok": False, "message": f"保存配置失败: {e}"}

    return {"ok": True, "message": "数据库初始化完成，正在自动重启..."}


@router.post("/restart")
def restart_app():
    import signal
    import subprocess
    import sys
    logger.info("用户请求重启应用以加载新配置")
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable])
    else:
        subprocess.Popen([sys.executable] + sys.argv)
    os.kill(os.getpid(), signal.SIGTERM)
    return {"ok": True, "message": "正在重启..."}


@router.post("/shutdown")
def shutdown_app():
    import signal
    import sys
    logger.info("用户请求关闭应用")
    os.kill(os.getpid(), signal.SIGTERM)
    return {"ok": True, "message": "正在关闭..."}
