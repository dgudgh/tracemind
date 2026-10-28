import logging
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.database import SessionLocal, engine

logger = logging.getLogger("app.sqlite_init")


def get_sqlite_db_path() -> str:
    from app.core.config import settings

    url = make_url(settings.DATABASE_URL)
    if not url.database or url.database == ":memory:":
        return os.path.abspath(os.path.join("data", "mesguard.db"))
    return os.path.abspath(url.database)


def is_sqlite_mode() -> bool:
    from app.core.config import settings
    return settings.is_sqlite


def sqlite_db_exists() -> bool:
    return os.path.isfile(get_sqlite_db_path())


def is_initialized() -> bool:
    if not sqlite_db_exists():
        return False
    db = None
    try:
        db = SessionLocal()
        has_user_table = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='sys_user'")
        ).first()
        if not has_user_table:
            return False
        return bool(db.execute(text("SELECT 1 FROM sys_user WHERE status = 'active' LIMIT 1")).first())
    except Exception as exc:
        logger.debug("SQLite init status check failed: %s", exc)
        return False
    finally:
        if db is not None:
            db.close()


def ensure_data_dirs():
    db_path = get_sqlite_db_path()
    Path(os.path.dirname(db_path) or ".").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)


def init_sqlite_database(admin_username: str, admin_password: str):
    if engine.dialect.name != "sqlite":
        raise RuntimeError("SQLite initialization cannot upgrade an external database; use the release migration CLI")
    ensure_data_dirs()

    from app.core.database import initialize_schema

    logger.info("Creating SQLite tables...")
    initialize_schema(engine, allow_upgrade=True)

    db = SessionLocal()
    try:
        _init_module_config(db)
        _init_admin_user(db, admin_username, admin_password)
        from app.services.inspection.policy_service import ensure_default_policies
        ensure_default_policies(db, commit=False)
        db.commit()
        logger.info("SQLite database initialized successfully")
    except Exception as e:
        db.rollback()
        logger.error("SQLite init failed: %s", e)
        raise
    finally:
        db.close()


def _init_module_config(db):
    from app.models.module_config import ModuleConfig
    existing = db.query(ModuleConfig).count()
    if existing > 0:
        return

    from app.api.v1.module_config import DEFAULT_MODULES
    for m in DEFAULT_MODULES:
        db.add(ModuleConfig(
            module_key=m["module_key"],
            module_name=m["module_name"],
            category=m.get("category", "operations"),
            sort_order=m.get("sort_order", 0),
            enabled=1,
        ))
    logger.info("Initialized %d modules", len(DEFAULT_MODULES))


def _init_admin_user(db, username: str, password: str):
    from app.models.user import User
    from app.core.security import hash_password

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return

    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        status="active",
    )
    db.add(user)
    logger.info("Created admin user: %s", username)
