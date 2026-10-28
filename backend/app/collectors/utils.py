from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

# Source-database query budgets for unattended reads.
#
# ``sqlserver_connection`` has always defaulted to a 10s query timeout, but the Oracle,
# MySQL and PostgreSQL connectors default to *unbounded* (``None``) -- so a call site that
# omits a budget silently gets none, and one slow catalog query pins an API worker (and
# the scheduler slot queued behind it) indefinitely. Bounding the connector defaults
# instead is not safe: ``call_timeout`` / ``read_timeout`` / ``statement_timeout`` would
# also cut short legitimate long running maintenance (backup, recovery, DDL, archive).
#
# So each unattended read states its budget explicitly, from this one place. Two rules:
#   1. a function that already bounds its SQL Server branch must use the same budget on
#      every other engine -- otherwise the guard only protects one of four engines;
#   2. new collectors use COLLECTION_QUERY_TIMEOUT_SECONDS unless they have evidence for
#      something else. Long-running *maintenance* operations must pass their own, larger
#      budget rather than relying on a default.
COLLECTION_QUERY_TIMEOUT_SECONDS = 10
SNAPSHOT_QUERY_TIMEOUT_SECONDS = 5


def rows_to_dicts(cursor, rows: Sequence) -> list[dict]:
    columns = [col[0].lower() for col in cursor.description or []]
    return [dict(zip(columns, row)) for row in rows]


def json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gbk", "latin1"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.hex()
    if isinstance(value, bytearray):
        return json_safe(bytes(value))
    if isinstance(value, (set, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def safe_rows(rows: list[dict]) -> list[dict]:
    return [json_safe(row) for row in rows]
