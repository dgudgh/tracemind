"""Database-scoped startup lock, held across DDL and journal writes."""

from __future__ import annotations

import hashlib
import math
import time
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection

_LOCKED = "tracemind_schema_lock"


@contextmanager
def schema_lock(bind, *, timeout: float = 30):
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("schema lock timeout must be finite and non-negative")
    if isinstance(bind, Connection) and bind.info.get(_LOCKED):
        yield bind
        return
    owned = not isinstance(bind, Connection)
    connection = bind.connect() if owned else bind
    acquired = False
    sqlite_timeout = None
    lock_key = None
    dialect = connection.dialect.name
    try:
        if connection.in_transaction():
            raise RuntimeError(
                "schema initialization needs a fresh or already schema-locked connection"
            )
        if dialect == "sqlite":
            sqlite_timeout = connection.exec_driver_sql(
                "PRAGMA busy_timeout"
            ).scalar_one()
            connection.exec_driver_sql(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            connection.rollback()
            # Explicit BEGIN makes SQLite DDL transactional even with legacy sqlite3 mode.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif dialect in {"mysql", "mariadb"}:
            database = connection.execute(text("SELECT DATABASE()")).scalar_one()
            if not database:
                raise RuntimeError("schema initialization requires a selected database")
            lock_key = (
                "tracemind:schema:" + hashlib.sha256(database.encode()).hexdigest()[:40]
            )
            result = connection.execute(
                text("SELECT GET_LOCK(:key, :timeout)"),
                {"key": lock_key, "timeout": timeout},
            ).scalar_one()
            if result != 1:
                raise TimeoutError("could not acquire the database schema lock")
            acquired = True
            connection.commit()
        elif dialect == "postgresql":
            # Lock the whole database: different search_paths can still resolve the same tables.
            identity = connection.execute(
                text("SELECT current_database()")
            ).scalar_one()
            lock_key = int.from_bytes(
                hashlib.sha256(("tracemind:schema:" + identity).encode()).digest()[:8],
                "big",
                signed=True,
            )
            deadline = time.monotonic() + timeout
            while True:
                if connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                ).scalar_one():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("could not acquire the database schema lock")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            acquired = True
            connection.commit()
        else:
            raise ValueError(f"unsupported platform database dialect: {dialect}")
        connection.info[_LOCKED] = True
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    finally:
        # A failed unlock must never return a session-scoped lock to the pool.
        try:
            if not getattr(connection, "invalidated", False) and not getattr(
                connection, "closed", False
            ):
                connection.info.pop(_LOCKED, None)
                if acquired:
                    connection.rollback()
                    statement = (
                        "SELECT RELEASE_LOCK(:key)"
                        if dialect in {"mysql", "mariadb"}
                        else "SELECT pg_advisory_unlock(:key)"
                    )
                    if (
                        connection.execute(
                            text(statement), {"key": lock_key}
                        ).scalar_one()
                        != 1
                    ):
                        raise RuntimeError("database schema lock release failed")
                    connection.commit()
                elif lock_key is not None:
                    # An interrupted acquisition may have succeeded on the server.
                    connection.invalidate()
                if sqlite_timeout is not None:
                    connection.rollback()
                    connection.exec_driver_sql(
                        f"PRAGMA busy_timeout={int(sqlite_timeout)}"
                    )
                    connection.rollback()
        except BaseException:
            connection.invalidate()
            raise
        finally:
            if owned:
                connection.close()
