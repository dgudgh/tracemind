"""Materialize SQL CLOBs before closing their Oracle cursor/connection."""

from time import monotonic

MAX_SQL_TEXT_BYTES = 4 * 1024 * 1024
MAX_SQL_BATCH_BYTES = 8 * 1024 * 1024
STORED_SQL_TEXT_BYTES = 65535


def check_sql_deadline(connection, deadline, *, max_timeout_ms=10000):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("SQL text batch deadline exceeded; batch was not saved")
    if connection is not None:
        connection.call_timeout = max(1, min(max_timeout_ms, int(remaining * 1000)))


def read_sql_text(value, *, before_io=None):
    if value is None:
        return None
    if hasattr(value, "read"):
        if before_io:
            before_io()
        if value.size() > MAX_SQL_TEXT_BYTES:
            raise ValueError("SQL CLOB exceeds the 4 MiB read limit; text was not truncated")
        if before_io:
            before_io()
        value = value.read()
    if not isinstance(value, str):
        raise ValueError("SQL CLOB did not return text")
    if len(value.encode("utf-8")) > MAX_SQL_TEXT_BYTES:
        raise ValueError("SQL text exceeds the 4 MiB read limit; text was not truncated")
    return value


def materialize_sql_rows(rows, *, connection=None, deadline=None):
    deadline = deadline if deadline is not None else monotonic() + 30
    previous_timeout = getattr(connection, "call_timeout", None)
    def check_budget():
        check_sql_deadline(connection, deadline, max_timeout_ms=previous_timeout or 10000)
    try:
        check_budget()
        byte_count = 0
        for row in rows:
            check_budget()
            row["sql_text"] = read_sql_text(row.get("sql_text"), before_io=check_budget)
            check_budget()
            byte_count += len((row["sql_text"] or "").encode("utf-8"))
            if byte_count > MAX_SQL_BATCH_BYTES:
                raise ValueError("SQL text batch exceeds 8 MiB; batch was not truncated or saved")
            row["sql_text_status"] = "complete" if row["sql_text"] else "not_collected"
        check_budget()
        return rows
    finally:
        if connection is not None and previous_timeout is not None:
            connection.call_timeout = previous_timeout


def validate_stored_sql_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("SQL text is missing; collection was not saved")
    # Existing MySQL history uses TEXT, whose capacity is bytes, not characters.
    if len(value.encode("utf-8")) > STORED_SQL_TEXT_BYTES:
        raise ValueError("SQL exceeds the history TEXT capacity (65535 UTF-8 bytes); history preserved, no truncation")
    return value
