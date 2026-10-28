import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from app.runtime.paths import ensure_frozen_runtime_root


def _log_dir() -> str:
    if getattr(sys, "frozen", False):
        return str(ensure_frozen_runtime_root() / "logs")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "log",
    )


LOG_DIR = _log_dir()


def setup_logging(debug: bool = False) -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app_handler = _file_handler("app.log", formatter)
    error_handler = _file_handler("error.log", formatter, level=logging.ERROR)
    access_handler = _file_handler("access.log", formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.addHandler(app_handler)
    app_logger.addHandler(error_handler)
    app_logger.addHandler(console_handler)

    access_logger = logging.getLogger("app.access")
    access_logger.setLevel(level)
    access_logger.addHandler(access_handler)

    _suppress_noisy_loggers()

    return app_logger


def _file_handler(filename: str, formatter: logging.Formatter, level: int = logging.DEBUG) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, filename),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _suppress_noisy_loggers():
    for name in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
