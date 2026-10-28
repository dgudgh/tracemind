"""Retained legacy evidence must agree with the canonical inspection table.

Only the explicit maintenance repair may fill missing IDs in a nonempty target.
Neither startup nor repair overwrites conflicts or removes the retained source.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException
import hashlib
import json
import re

from sqlalchemy import BigInteger, DateTime, Integer, JSON, LargeBinary, MetaData, SmallInteger, String, Table, Text, cast, func, inspect, select, text

SOURCE = "db_check_result_new"
TARGET = "db_check_result"
FIELDS = ("id", "task_id", "instance_id", "db_type", "check_category", "check_item",
          "check_status", "metric_name", "metric_value", "threshold_value", "finding",
          "suggestion", "raw_data", "create_time")
BATCH_SIZE = 25
SAMPLE_SIZE = 20
MAX_ROW_BYTES = 1024 * 1024


class InspectionResultIntegrityError(RuntimeError):
    code = "legacy_inspection_results_inconsistent"

    def __init__(self, report):
        self.report = report
        super().__init__(
            "Retained inspection results need reconciliation; "
            f"state={report['state']}, missing={report['missing_rows']}, "
            f"conflicts={report['conflicting_rows']}, invalid={report['invalid_rows']}. "
            "Stop all writers, verify a backup, then run "
            "python scripts/migrate_schema.py inspection-results-check. "
            "Do not drop the legacy table or retry with automatic overwrite."
        )


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _require_lock(connection):
    from app.migrations.locking import _LOCKED

    if connection.info.get(_LOCKED) is not True:
        raise RuntimeError("inspection reconciliation requires an owned schema_lock")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_evidence(value):
    # Type-tag every node: converting Decimal to str alone would equate JSON numbers
    # with JSON strings; binary floats would lose retained evidence precision.
    if isinstance(value, dict):
        return ["object", [[key, _json_evidence(item)] for key, item in sorted(value.items())]]
    if isinstance(value, list):
        return ["array", [_json_evidence(item) for item in value]]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return [type(value).__name__, value]
    if type(value) in {int, float, Decimal}:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ValueError("non-finite JSON evidence")
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return ["number", sign if any(digits) else 0, digits, exponent if any(digits) else 0]
    raise ValueError("invalid JSON evidence type")


def _row_digest(row, *, columns, dialect):
    values = dict(row)
    for name, column in columns.items():
        value = values[name]
        if value is None:
            if not column.nullable:
                raise ValueError("required evidence field is null")
            continue
        if name != "task_id" and isinstance(column.type, Integer):
            if type(value) is str and re.fullmatch(r"-?(0|[1-9][0-9]*)", value, flags=re.ASCII):
                values[name] = int(value)
            elif type(value) is not int:
                raise ValueError("integer field cannot be represented losslessly")
            bits = 64 if dialect == "sqlite" or isinstance(column.type, BigInteger) else 16 if isinstance(column.type, SmallInteger) else 32
            unsigned = bool(getattr(column.type, "unsigned", False)) and dialect in {"mysql", "mariadb"}
            if not (0 if unsigned else -(2 ** (bits - 1))) <= values[name] < 2 ** (bits if unsigned else bits - 1):
                raise ValueError("integer exceeds the target representation")
        elif name != "raw_data" and isinstance(column.type, String):
            values[name] = str(value)
            if column.type.length is not None and len(values[name]) > column.type.length:
                raise ValueError("text field cannot be represented losslessly")
    task = values["task_id"]
    if task is not None:
        task = str(task)
        if len(task) > 30:
            raise ValueError("task identifier cannot be represented losslessly")
        values["task_id"] = task
    raw = values["raw_data"]
    if isinstance(columns["raw_data"].type, JSON):
        if raw is None:
            values["raw_data"] = ["sql_null"]
        else:
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw, object_pairs_hook=_unique_pairs, parse_float=Decimal)
                except (ValueError, DecimalException, RecursionError):
                    raise ValueError("invalid JSON evidence") from None
            values["raw_data"] = _json_evidence(raw)
    created = values["create_time"]
    if isinstance(created, str):
        if re.search(r"[.,][0-9]{7,}", created):
            raise ValueError("timestamp precision cannot be represented losslessly")
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            if isinstance(columns["create_time"].type, DateTime):
                raise ValueError("invalid evidence timestamp") from None
    if isinstance(created, datetime):
        if created.tzinfo is not None and not getattr(columns["create_time"].type, "timezone", False):
            raise ValueError("timezone cannot be represented losslessly")
        values["create_time"] = created.isoformat(timespec="microseconds")
    elif created is not None and isinstance(columns["create_time"].type, DateTime):
        raise ValueError("invalid evidence timestamp")
    return hashlib.sha256(_json(values).encode("ascii")).hexdigest()


def _tables(connection):
    inspector = inspect(connection)
    source = Table(SOURCE, MetaData(), autoload_with=connection) if inspector.has_table(SOURCE) else None
    target = Table(TARGET, MetaData(), autoload_with=connection) if inspector.has_table(TARGET) else None
    return source, target


def _select_columns(table):
    # Reflecting invalid legacy JSON/timestamps must not escape as decoder errors.
    return [(cast(table.c[name], Text).label(name) if isinstance(table.c[name].type, (JSON, DateTime))
             else table.c[name]) for name in FIELDS]


def _row_size(table, dialect):
    def size(column):
        value = cast(column, Text)
        return func.length(cast(value, LargeBinary)) if dialect == "sqlite" else func.octet_length(value)
    return sum(func.coalesce(size(column), 0) for column in table.c)


def _oversized_rows(connection, table):
    return connection.scalar(select(func.count()).select_from(table).where(
        _row_size(table, connection.dialect.name) > MAX_ROW_BYTES))


def _batches(connection, source, *, physical=False):
    last = None
    while True:
        columns = ([column if column.name == "id" else cast(column, Text).label(column.name)
                    for column in source.c] if physical else _select_columns(source))
        statement = select(*columns).order_by(source.c.id).limit(BATCH_SIZE)
        if last is not None:
            statement = statement.where(source.c.id > last)
        rows = connection.execute(statement).mappings().all()
        if not rows:
            return
        yield rows
        new_last = rows[-1]["id"]
        if type(new_last) is not int or (last is not None and new_last <= last):
            return
        last = new_last


def inspect_legacy_results(connection):
    """Read-only bounded-memory comparison; reports contain no finding/SQL payloads."""
    source, target = _tables(connection)
    report = {"source_table": SOURCE, "target_table": TARGET, "state": "absent",
              "comparison_fields": list(FIELDS), "source_extra_columns": [], "target_extra_columns": [],
              "source_rows": 0, "target_rows": 0, "matched_rows": 0, "missing_rows": 0,
              "conflicting_rows": 0, "invalid_rows": 0, "sample_missing_ids": [],
              "sample_conflicting_ids": [], "missing_columns": {}, "oversized_rows": {},
              "max_row_bytes": MAX_ROW_BYTES, "snapshot_hash": None}
    if source is None:
        return report
    report["source_extra_columns"] = sorted(set(source.c.keys()) - set(FIELDS))
    report["source_rows"] = connection.scalar(select(func.count()).select_from(source))
    if target is not None:
        report["target_rows"] = connection.scalar(select(func.count()).select_from(target))
        report["target_extra_columns"] = sorted(set(target.c.keys()) - set(FIELDS))
    for table in (source, target):
        if table is not None:
            missing = sorted(set(FIELDS) - set(table.c.keys()))
            if missing:
                report["missing_columns"][table.name] = missing
    digest = hashlib.sha256()
    digest.update(_json({"source": [(item.name, str(item.type)) for item in source.c],
                         "target": [(item.name, str(item.type)) for item in target.c] if target is not None else None}).encode("ascii"))
    if report["source_rows"] == 0:
        report["state"] = "consistent"
        report["snapshot_hash"] = hashlib.sha256(_json(report).encode("ascii") + digest.digest()).hexdigest()
        return report
    for table in (source, target):
        if table is not None:
            oversized = _oversized_rows(connection, table)
            if oversized:
                report["oversized_rows"][table.name] = oversized
    if report["oversized_rows"]:
        report["state"] = "evidence_too_large"
    elif report["missing_columns"]:
        report["state"] = "invalid_schema"
    else:
        if target is None:
            from app.models.check_result import DBCheckResult

            columns = dict(DBCheckResult.__table__.c.items())
        else:
            columns = dict(target.c.items())
        columns = {name: columns[name] for name in FIELDS}
        previous = None
        seen = 0
        for rows in _batches(connection, source):
            ids = [row["id"] for row in rows]
            actual = {} if target is None else {row["id"]: row for row in connection.execute(
                select(*_select_columns(target)).where(target.c.id.in_(ids))).mappings()}
            for row in rows:
                identity = row["id"]
                seen += 1
                if type(identity) is not int or identity <= 0 or (previous is not None and identity <= previous):
                    report["invalid_rows"] += 1
                    digest.update(b"invalid-id")
                    continue
                previous = identity
                try:
                    expected_hash = _row_digest(row, columns=columns, dialect=connection.dialect.name)
                    actual_hash = _row_digest(actual[identity], columns=columns, dialect=connection.dialect.name) if identity in actual else None
                except (ValueError, TypeError, OverflowError, RecursionError):
                    report["invalid_rows"] += 1
                    digest.update(_json({"invalid_id": identity}).encode("ascii"))
                    continue
                digest.update(_json({"id": identity, "source": expected_hash, "target": actual_hash}).encode("ascii"))
                if actual_hash is None:
                    report["missing_rows"] += 1
                    if len(report["sample_missing_ids"]) < SAMPLE_SIZE:
                        report["sample_missing_ids"].append(identity)
                elif actual_hash == expected_hash:
                    report["matched_rows"] += 1
                else:
                    report["conflicting_rows"] += 1
                    if len(report["sample_conflicting_ids"]) < SAMPLE_SIZE:
                        report["sample_conflicting_ids"].append(identity)
        if seen != report["source_rows"]:
            report["invalid_rows"] += 1
        report["state"] = ("invalid_rows" if report["invalid_rows"] else
                           "conflict" if report["conflicting_rows"] else
                           "missing" if report["missing_rows"] else "consistent")
    digest.update(_json({key: value for key, value in report.items() if key != "snapshot_hash"}).encode("ascii"))
    report["snapshot_hash"] = digest.hexdigest()
    return report


def require_legacy_integrity(connection, *, allow_initial_copy=False):
    report = inspect_legacy_results(connection)
    if report["state"] in {"absent", "consistent"}:
        return report
    if allow_initial_copy and report["state"] == "missing" and report["target_rows"] == 0:
        return report
    raise InspectionResultIntegrityError(report)


def _copy_rows(connection, source, target, ids=None):
    values = []
    for name in FIELDS:
        value = source.c[name]
        if name == "task_id":
            value = cast(value, String(30))
        elif connection.dialect.name == "postgresql" and isinstance(target.c[name].type, JSON):
            value = cast(value, target.c[name].type)
        values.append(value)
    statement = select(*values)
    if ids is not None:
        statement = statement.where(source.c.id.in_(ids))
    connection.execute(target.insert().from_select(FIELDS, statement))


def _advance_sequence(connection):
    if connection.dialect.name != "postgresql":
        return
    sequence = connection.scalar(text("SELECT pg_get_serial_sequence('db_check_result', 'id')"))
    if sequence:
        connection.execute(text(
            "SELECT setval(CAST(:sequence AS regclass), "
            "GREATEST((SELECT MAX(id) FROM db_check_result), nextval(CAST(:sequence AS regclass))), true)"
        ), {"sequence": sequence})


def _rows(connection, table, *, physical=False):
    for batch in _batches(connection, table, physical=physical):
        yield from batch


def _source_snapshot(connection, source):
    if _oversized_rows(connection, source):
        raise InspectionResultIntegrityError({"state": "evidence_too_large", "missing_rows": 0,
                                              "conflicting_rows": 0, "invalid_rows": 0})
    digest = hashlib.sha256()
    count = 0
    for row in _rows(connection, source, physical=True):
        digest.update(_json(dict(row)).encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def _target_snapshot(connection, target, *, source=None):
    # Ordered merge predicts the exact canonical target after missing-only inserts.
    # No unbounded ID set or payload list is kept in process memory.
    columns = {name: target.c[name] for name in FIELDS}
    if _oversized_rows(connection, target):
        raise InspectionResultIntegrityError({"state": "evidence_too_large", "missing_rows": 0,
                                              "conflicting_rows": 0, "invalid_rows": 0})
    existing = iter(_rows(connection, target))
    missing = iter(_rows(connection, source)) if source is not None else iter(())
    left, right = next(existing, None), next(missing, None)
    digest, count = hashlib.sha256(), 0
    while left is not None or right is not None:
        if right is None or (left is not None and left["id"] <= right["id"]):
            row = left
            if right is not None and left["id"] == right["id"]:
                right = next(missing, None)
            left = next(existing, None)
        else:
            row, right = right, next(missing, None)
        try:
            value = _row_digest(row, columns=columns, dialect=connection.dialect.name)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise InspectionResultIntegrityError({"state": "invalid_target_rows", "missing_rows": 0,
                                                  "conflicting_rows": 0, "invalid_rows": 1}) from None
        digest.update(value.encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def _copy_baseline(connection, source, target, report):
    if report["target_extra_columns"]:
        raise InspectionResultIntegrityError({**report, "state": "unsupported_target_columns"})
    _require_transactional_copy(connection, report)
    return _source_snapshot(connection, source), _target_snapshot(connection, target, source=source)


def _require_transactional_copy(connection, report):
    dialect = connection.dialect.name
    if dialect in {"mysql", "mariadb"}:
        rows = connection.execute(text(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (:source, :target)"
        ), {"source": SOURCE, "target": TARGET}).all()
        if len(rows) != 2 or any(str(row[1]).lower() != "innodb" for row in rows):
            raise InspectionResultIntegrityError({**report, "state": "unsupported_storage"})
        account = connection.scalar(text("SELECT CURRENT_USER()"))
        if not isinstance(account, str) or "@" not in account:
            raise InspectionResultIntegrityError({**report, "state": "trigger_visibility_unverified"})
        user, host = account.rsplit("@", 1)
        grantee = "'" + user.replace("'", "''") + "'@'" + host.replace("'", "''") + "'"
        visible = connection.scalar(text(
            "SELECT 1 FROM information_schema.SCHEMA_PRIVILEGES WHERE CAST(GRANTEE AS BINARY)=CAST(:grantee AS BINARY) "
            "AND PRIVILEGE_TYPE='TRIGGER' AND CAST(TABLE_SCHEMA AS BINARY)=CAST(DATABASE() AS BINARY) "
            "UNION ALL SELECT 1 FROM information_schema.TABLE_PRIVILEGES WHERE CAST(GRANTEE AS BINARY)=CAST(:grantee AS BINARY) "
            "AND PRIVILEGE_TYPE='TRIGGER' AND CAST(TABLE_SCHEMA AS BINARY)=CAST(DATABASE() AS BINARY) "
            "AND CAST(TABLE_NAME AS BINARY)=CAST(:target AS BINARY) LIMIT 1"
        ), {"grantee": grantee, "target": TARGET})
        if visible != 1:
            global_trigger = connection.scalar(text(
                "SELECT 1 FROM information_schema.USER_PRIVILEGES "
                "WHERE CAST(GRANTEE AS BINARY)=CAST(:grantee AS BINARY) AND PRIVILEGE_TYPE='TRIGGER' LIMIT 1"
            ), {"grantee": grantee})
            if global_trigger == 1:
                partial = connection.execute(text("SHOW GLOBAL VARIABLES LIKE 'partial_revokes'")).all()
                if len(partial) == 1 and str(partial[0][0]).lower() == "partial_revokes" and str(partial[0][1]).upper() in {"OFF", "0"}:
                    visible = 1
        if visible != 1:
            # TRIGGERS silently hides rows without TRIGGER privilege. Role-only or
            # wildcard grants not positively verified here require a maintenance account.
            raise InspectionResultIntegrityError({**report, "state": "trigger_visibility_unverified"})
        trigger = connection.scalar(text(
            "SELECT COUNT(*) FROM information_schema.triggers "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:target"
        ), {"target": TARGET})
    elif dialect == "postgresql":
        kinds = connection.execute(text(
            "SELECT relkind FROM pg_class WHERE oid IN (CAST(:source AS regclass), CAST(:target AS regclass))"
        ), {"source": SOURCE, "target": TARGET}).scalars().all()
        if len(kinds) != 2 or any(kind != "r" for kind in kinds):
            raise InspectionResultIntegrityError({**report, "state": "unsupported_storage"})
        trigger = connection.scalar(text(
            "SELECT COUNT(*) FROM pg_trigger WHERE tgrelid=CAST(:target AS regclass) AND NOT tgisinternal"
        ), {"target": TARGET})
    else:
        return
    # A user trigger may write nontransactional/foreign tables or external systems;
    # post-copy row hashes cannot roll those effects back.
    if trigger:
        raise InspectionResultIntegrityError({**report, "state": "unsupported_target_triggers"})


def _verify_copy(connection, source, target, baseline, report):
    # Checking only source-covered rows misses triggers deleting unrelated new results.
    try:
        if (baseline[0] != _source_snapshot(connection, source)
                or baseline[1] != _target_snapshot(connection, target)):
            raise InspectionResultIntegrityError({**report, "state": "copy_side_effect"})
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise InspectionResultIntegrityError({**report, "state": "copy_side_effect"}) from None
    return require_legacy_integrity(connection)


def copy_initial_legacy_results(connection):
    _require_lock(connection)
    report = require_legacy_integrity(connection, allow_initial_copy=True)
    if report["state"] in {"absent", "consistent"}:
        if report["source_rows"]:
            _advance_sequence(connection)
        return
    source, target = _tables(connection)
    if target is None:
        raise InspectionResultIntegrityError(report)
    baseline = _copy_baseline(connection, source, target, report)
    _copy_rows(connection, source, target)
    _verify_copy(connection, source, target, baseline, report)
    _advance_sequence(connection)


def reconcile_missing_legacy_results(connection, *, expected_snapshot_hash):
    """Caller owns schema_lock, verified backup and a quiesced maintenance window."""
    _require_lock(connection)
    report = inspect_legacy_results(connection)
    if (not isinstance(expected_snapshot_hash, str) or len(expected_snapshot_hash) != 64
            or expected_snapshot_hash != report["snapshot_hash"]):
        raise InspectionResultIntegrityError({**report, "state": "snapshot_changed"})
    if report["state"] in {"absent", "consistent"}:
        return {"before": report, "after": report, "copied_rows": 0}
    source, target = _tables(connection)
    if report["state"] != "missing" or source is None or target is None:
        raise InspectionResultIntegrityError(report)
    baseline = _copy_baseline(connection, source, target, report)
    copied = 0
    for rows in _batches(connection, source):
        ids = [row["id"] for row in rows]
        existing = set(connection.execute(select(target.c.id).where(target.c.id.in_(ids))).scalars())
        missing = [identity for identity in ids if identity not in existing]
        if missing:
            _copy_rows(connection, source, target, missing)
            copied += len(missing)
    after = _verify_copy(connection, source, target, baseline, report)
    _advance_sequence(connection)
    return {"before": report, "after": after, "copied_rows": copied}
