from __future__ import annotations

import re
from typing import Any

from sqlalchemy.engine import make_url

from app.core.config import settings


_URL_CREDENTIALS = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@")
_PASSWORD_ARGUMENT = re.compile(r"(?i)(password\s*[=:]\s*)([^\s,;]+)")


def database_engine_options(database_url: str, *, sql_echo: bool | None = None) -> dict[str, Any]:
    """Return bounded, driver-compatible SQLAlchemy engine options."""
    url = make_url(database_url)
    options: dict[str, Any] = {
        "echo": settings.SQL_ECHO if sql_echo is None else sql_echo,
    }
    connect_args: dict[str, Any] = {}

    if url.get_backend_name() == "sqlite":
        connect_args.update(check_same_thread=False, timeout=30)
    else:
        options.update(
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_timeout=settings.DATABASE_POOL_TIMEOUT_SECONDS,
        )

    if url.drivername == "mysql+pymysql":
        connect_args.update(
            connect_timeout=settings.DATABASE_CONNECT_TIMEOUT_SECONDS,
            read_timeout=settings.DATABASE_READ_TIMEOUT_SECONDS,
            write_timeout=settings.DATABASE_WRITE_TIMEOUT_SECONDS,
        )

    options["connect_args"] = connect_args
    return options


def database_target_summary(database_url: str) -> dict[str, str | int | None]:
    """Describe a database target without exposing account credentials."""
    url = make_url(database_url)
    return {
        "driver": url.drivername,
        "host": url.host or "local",
        "port": url.port,
        "database": url.database or "",
    }


def redact_database_error(error: BaseException | str) -> str:
    message = str(error)
    message = _URL_CREDENTIALS.sub(r"\1***:***@", message)
    message = _PASSWORD_ARGUMENT.sub(r"\1***", message)
    return message[:2000]
