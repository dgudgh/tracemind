import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings
from app.core.database_options import database_engine_options

logger = logging.getLogger("app.core.database")

engine = create_engine(
    settings.DATABASE_URL,
    **database_engine_options(settings.DATABASE_URL),
)

if settings.is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def initialize_schema(bind=None, *, allow_upgrade: bool | None = None, lock_timeout: float = 30):
    """Initialize the platform schema under one database lock; never back up external DBs."""
    from app import models  # noqa: F401 - register model metadata for every initialization entrypoint
    from app.migrations.initialization import initialize_schema as initialize

    return initialize(
        engine if bind is None else bind, Base.metadata,
        allow_upgrade=allow_upgrade, lock_timeout=lock_timeout,
    )


def verify_schema_ready(bind=None):
    """Read-only managed-hot gate: no DDL or bootstrap is performed."""
    from app.migrations.contract import verify_schema_ready as verify
    return verify(engine if bind is None else bind, Base.metadata)


def ensure_runtime_schema(bind=None, *, allow_upgrade: bool | None = None, lock_timeout: float = 30):
    """Compatibility API for callers that only upgrade existing runtime tables."""
    from app.migrations.initialization import ensure_runtime_schema as ensure

    return ensure(
        engine if bind is None else bind,
        allow_upgrade=allow_upgrade, lock_timeout=lock_timeout,
    )
