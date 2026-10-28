"""Bounded adapters for three fingerprinted user-script sections, never SQL input."""

from __future__ import annotations

from hashlib import sha256
import re
from time import monotonic


SECTION_HASHES = {
    "database_patch": "6ff79d96bdfe26178c144596f209b57231a0e1a565fc2e3d2c977e229e950deb",
    "range_par_extend": "f2870fd04f50d187eb51f8c195766835c65584b12f45bd85aecebfe4eee86427",
    "autotask_windows": "15cf9f7001159bb746b7f9157392a355844a991cd8b85ba27a40061fc309d836",
}
MAX_CONTAINERS = 64
SECTION_TIMEOUT_SECONDS = 60
IDENTITY_SQL = "SELECT SYS_CONTEXT('USERENV', 'CON_ID') CON_ID, SYS_CONTEXT('USERENV', 'CON_NAME') CON_NAME FROM dual"
CONTAINERS_SQL = "SELECT con_id, name, open_mode, restricted FROM v$containers WHERE con_id <> 2 ORDER BY con_id"
PDBS_SQL = "SELECT pdb_id, pdb_name, status FROM dba_pdbs WHERE pdb_id <> 2 ORDER BY pdb_id"
COLUMNS_SQL = "SELECT column_name, data_type FROM all_tab_columns WHERE owner = 'SYS' AND table_name = :view_name ORDER BY column_id"

RANGE_SQL = """SELECT p.table_owner username,
       p.table_name tablename,
       p.partition_name partname,
       p.high_value max_range
  FROM dba_tab_partitions p
  JOIN (SELECT b.table_owner, b.table_name,
               MAX(b.partition_position) - 1 partition_position
          FROM dba_tab_partitions b
          JOIN dba_users u ON u.username = b.table_owner
         WHERE u.account_status = 'OPEN'
         GROUP BY b.table_owner, b.table_name) penultimate
    ON penultimate.table_owner = p.table_owner
   AND penultimate.table_name = p.table_name
   AND penultimate.partition_position = p.partition_position
 WHERE p.table_owner NOT IN ('SYS', 'SYSTEM', 'DBSNMP', 'MGMT_VIEW', 'SYSMAN', 'SH', 'MDSYS', 'AUDSYS')
   AND p.interval = 'NO'
   AND NOT EXISTS (SELECT 1 FROM dba_recyclebin r
                    WHERE r.owner = p.table_owner AND r.object_name = p.table_name)
   AND EXISTS (SELECT 1 FROM dba_part_key_columns k
                 JOIN dba_tab_columns tc ON tc.owner = k.owner
                  AND tc.table_name = k.name AND tc.column_name = k.column_name
                WHERE k.owner = p.table_owner AND k.name = p.table_name
                  AND (tc.data_type = 'DATE' OR tc.data_type LIKE 'TIMESTAMP%'))
 ORDER BY p.table_owner, p.table_name"""

RANGE_EXCLUDED_OWNERS = "('SYS','SYSTEM','DBSNMP','MGMT_VIEW','SYSMAN','SH','MDSYS','AUDSYS')"
RANGE_USERS_SQL = f"""SELECT username FROM dba_users
 WHERE account_status = 'OPEN' AND username NOT IN {RANGE_EXCLUDED_OWNERS}
 ORDER BY username"""
RANGE_METADATA_SQL = f"""SELECT table_owner, table_name, partition_name, partition_position, interval
 FROM dba_tab_partitions WHERE table_owner NOT IN {RANGE_EXCLUDED_OWNERS}
 ORDER BY table_owner, table_name, partition_position, partition_name"""
RANGE_KEYS_SQL = f"""SELECT owner, name, column_name FROM dba_part_key_columns
 WHERE owner NOT IN {RANGE_EXCLUDED_OWNERS} ORDER BY owner, name, column_name"""
RANGE_TYPES_SQL = """SELECT column_name, data_type FROM dba_tab_columns
 WHERE owner = :owner AND table_name = :table_name
   AND (data_type = 'DATE' OR data_type LIKE 'TIMESTAMP%') ORDER BY column_name"""
RANGE_RECYCLE_SQL = """SELECT 1 found FROM dba_recyclebin
 WHERE owner = :owner AND object_name = :table_name AND ROWNUM = 1"""
RANGE_BOUNDARY_SQL = """SELECT table_owner username, table_name tablename, partition_name partname,
       partition_position, interval, high_value max_range
 FROM dba_tab_partitions
 WHERE table_owner = :owner AND table_name = :table_name AND partition_name = :partition_name"""
MAX_PARTITION_METADATA_ROWS = 20000
MAX_PARTITION_CANDIDATES = 200

WINDOWS_SQL = """SELECT w.window_name, c.optimizer_stats, w.schedule_type,
       w.repeat_interval, w.duration,
       TO_CHAR(w.next_start_date, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD TZH:TZM') next_start_date,
       TO_CHAR(w.last_start_date, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD TZH:TZM') last_start_date,
       CASE WHEN w.enabled = 'TRUE' THEN
         '<div align="left"><font color="green"><b>' || w.enabled || '</b></font></div>'
       ELSE '<div align="left"><font color="green"><b>' || w.enabled || '</b></font></div>' END enabled
  FROM dba_autotask_window_clients c
  JOIN dba_scheduler_windows w ON c.window_name = w.window_name
 WHERE c.optimizer_stats = 'ENABLED'
 ORDER BY w.window_name"""

NOTES = {
    "database_patch": "Probe each container's SQLPATCH dictionary. VERSION uses TARGET_VERSION on modern dictionaries; SOURCE_VERSION and TARGET_VERSION are also retained. Without BUNDLE_ID, keep both SQLPATCH and REGISTRY_HISTORY records with RECORD_SOURCE rather than inventing a deduplication key.",
    "range_par_extend": "Read DBA_TAB_PARTITIONS.HIGH_VALUE as LONG separately in every non-seed container. Preserve penultimate-position, open-account, interval, recyclebin and date/timestamp-key predicates. MAX_RANGE remains the original boundary expression, not an evaluated date.",
    "autotask_windows": "Convert timestamps on the server with FF9, TZR, TZD and TZH:TZM; preserve fractional seconds, named region, daylight abbreviation and offset. Join windows/clients inside the verified container.",
}


class OracleCompatibilityError(RuntimeError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def _identifier(name):
    # Only inventory names are used, and unusual names fail closed, not interpolated.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]{0,127}", name):
        raise ValueError("Unsupported container identifier")
    return '"' + name + '"'


class _Reader:
    def __init__(self, conn, evidence):
        self.conn = conn
        self.evidence = evidence
        self.container = None
        self.deadline = monotonic() + SECTION_TIMEOUT_SECONDS

    def query(self, sql, *, binds=None, limit=MAX_CONTAINERS, kind="probe", restoring=False):
        if not restoring and monotonic() >= self.deadline:
            raise TimeoutError("Oracle compatibility section deadline exceeded")
        self.conn.call_timeout = 10000 if restoring else max(1, min(10000, int((self.deadline - monotonic()) * 1000)))
        entry = {"sql": sql, "binds": binds or {}, "container": self.container, "kind": kind, "status": "running"}
        self.evidence["actual_statements"].append(entry)
        started = monotonic()
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql, binds or {})
            columns = [d[0] for d in (cursor.description or [])]
            if not restoring:
                remaining = self.deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Oracle compatibility section deadline exceeded before fetch")
                self.conn.call_timeout = max(1, min(10000, int(remaining * 1000)))
            values = cursor.fetchmany(limit + 1) if columns else []
            if not restoring and monotonic() > self.deadline:
                raise TimeoutError("Oracle compatibility section deadline exceeded during fetch")
            rows = [dict(zip(columns, row)) for row in values[:limit]]
            entry.update(status="success", columns=columns, rows=rows,
                         row_count=len(rows), truncated=len(values) > limit)
            if entry["truncated"]:
                self.evidence["truncated"] = True
                raise RuntimeError(f"{kind} exceeded its {limit}-row evidence limit")
            return columns, rows
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            raise
        finally:
            entry["elapsed_seconds"] = round(monotonic() - started, 6)
            cursor.close()

    def identity(self, *, restoring=False):
        _, rows = self.query(IDENTITY_SQL, limit=1, restoring=restoring)
        if len(rows) != 1 or not rows[0].get("CON_NAME"):
            raise RuntimeError("Cannot establish actual Oracle container")
        identity = {"con_id": int(rows[0]["CON_ID"]), "name": rows[0]["CON_NAME"]}
        _identifier(identity["name"])
        self.container = identity
        return identity

    def switch(self, target, *, restoring=False):
        self.query("ALTER SESSION SET CONTAINER = " + _identifier(target["name"]),
                   kind="session_container", restoring=restoring)
        actual = self.identity(restoring=restoring)
        if actual != target:
            raise RuntimeError("Actual Oracle container differs from requested container")

    def dictionary(self, view_name):
        _, rows = self.query(COLUMNS_SQL, binds={"view_name": view_name}, limit=200, kind="dictionary")
        return {r["COLUMN_NAME"]: r["DATA_TYPE"] for r in rows}


def _patch_queries(columns):
    required = {"PATCH_ID", "DESCRIPTION", "ACTION", "STATUS", "ACTION_TIME"}
    if not required <= columns.keys():
        raise RuntimeError("SQLPATCH dictionary is missing required patch fields")
    if {"SOURCE_VERSION", "TARGET_VERSION"} <= columns.keys():
        version = "p.target_version"
        extra = ", p.source_version, p.target_version"
    elif "VERSION" in columns:
        version = "p.version"
        extra = ""
    else:
        raise RuntimeError("SQLPATCH dictionary has no supported version representation")
    sqlpatch = f"""SELECT TO_CHAR(p.patch_id) patch_id, p.description, {version} version,
       p.action || ' / ' || p.status action_status,
       TO_CHAR(p.action_time, 'YYYY-MM-DD HH24:MI:SS.FF6') action_time,
       'SQLPATCH' record_source{extra}
  FROM dba_registry_sqlpatch p ORDER BY p.action_time DESC, p.patch_id"""
    # Legacy deduplication is valid only if both of its original columns exist.
    dedup = ""
    if {"BUNDLE_ID", "VERSION"} <= columns.keys():
        dedup = """ WHERE NOT EXISTS (SELECT 1 FROM dba_registry_sqlpatch p
        WHERE p.bundle_id = h.id AND p.action = h.action
          AND NVL(p.version, '-') = NVL(h.version, '-') AND p.action_time = h.action_time)"""
    history = """SELECT NVL(TO_CHAR(h.id), 'HISTORY') patch_id, h.comments description,
       h.version, h.action || ' / HISTORY' action_status,
       TO_CHAR(h.action_time, 'YYYY-MM-DD HH24:MI:SS.FF6') action_time,
       'REGISTRY_HISTORY' record_source
  FROM dba_registry_history h""" + dedup + " ORDER BY h.action_time DESC, h.id"
    return [sqlpatch, history]


def _inventory(reader):
    _, containers = reader.query(CONTAINERS_SQL, kind="inventory")
    _, pdbs = reader.query(PDBS_SQL, kind="inventory")
    actual = {(int(r["CON_ID"]), r["NAME"]) for r in containers}
    expected = {(1, "CDB$ROOT")} | {(int(r["PDB_ID"]), r["PDB_NAME"]) for r in pdbs}
    if actual != expected or len(actual) != len(containers):
        raise RuntimeError("V$CONTAINERS does not cover the DBA_PDBS inventory")
    for row in containers:
        _identifier(row["NAME"])
        if row["OPEN_MODE"] not in {"READ WRITE", "READ ONLY"} or row["RESTRICTED"] != "NO":
            raise RuntimeError(f"Container {row['NAME']} is closed or restricted; coverage is incomplete")
    return containers, pdbs


def _partition_catalog(reader, phase):
    return {
        "users": reader.query(RANGE_USERS_SQL, limit=4096, kind=f"partition_{phase}_users")[1],
        "partitions": reader.query(RANGE_METADATA_SQL, limit=MAX_PARTITION_METADATA_ROWS, kind=f"partition_{phase}_metadata")[1],
        "keys": reader.query(RANGE_KEYS_SQL, limit=MAX_PARTITION_METADATA_ROWS, kind=f"partition_{phase}_keys")[1],
    }


def _partition_candidates(catalog):
    owners = {row["USERNAME"] for row in catalog["users"]}
    tables = {}
    for row in catalog["partitions"]:
        if row["TABLE_OWNER"] not in owners:
            continue
        position = row["PARTITION_POSITION"]
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise RuntimeError("Invalid partition position in dictionary evidence")
        key = (row["TABLE_OWNER"], row["TABLE_NAME"])
        positions = tables.setdefault(key, {})
        if position in positions:
            raise RuntimeError("Duplicate partition position in dictionary evidence")
        positions[position] = row
    candidates = []
    for key, positions in sorted(tables.items()):
        # Keep MAX(position)-1, not the second row or the second-largest position.
        row = positions.get(max(positions) - 1)
        if row is not None and row["INTERVAL"] == "NO":
            candidates.append(row)
    if len(candidates) > MAX_PARTITION_CANDIDATES:
        raise RuntimeError("Partition candidate count exceeded the bounded inspection limit")
    return candidates, len(tables)


def _partition_eligibility(reader, row, phase):
    binds = {"owner": row["TABLE_OWNER"], "table_name": row["TABLE_NAME"]}
    # Exact table lookups avoid expanding the whole recyclebin inside a correlated join.
    return {
        "types": reader.query(RANGE_TYPES_SQL, binds=binds, limit=4096, kind=f"partition_{phase}_types")[1],
        "recycled": reader.query(RANGE_RECYCLE_SQL, binds=binds, limit=1, kind=f"partition_{phase}_recycle")[1],
    }


def _partition_boundary(reader, candidate, phase):
    binds = {"owner": candidate["TABLE_OWNER"], "table_name": candidate["TABLE_NAME"],
             "partition_name": candidate["PARTITION_NAME"]}
    _, rows = reader.query(RANGE_BOUNDARY_SQL, binds=binds, limit=1, kind=f"partition_{phase}_long")
    if len(rows) != 1:
        raise RuntimeError("Selected partition disappeared or its identity is ambiguous")
    row = rows[0]
    if (row["USERNAME"], row["TABLENAME"], row["PARTNAME"], row["PARTITION_POSITION"], row["INTERVAL"]) != (
        candidate["TABLE_OWNER"], candidate["TABLE_NAME"], candidate["PARTITION_NAME"],
        candidate["PARTITION_POSITION"], candidate["INTERVAL"],
    ):
        raise RuntimeError("Selected partition changed before its boundary was read")
    if not isinstance(row.get("MAX_RANGE"), str) or not row["MAX_RANGE"].strip():
        raise RuntimeError("Partition LONG boundary was not fully collected")
    return row


def _collect_partition_rows(reader, max_rows):
    scan = {"strategy": "bounded_catalog_then_exact_long", "complete": False,
            "metadata_row_limit": MAX_PARTITION_METADATA_ROWS, "candidate_limit": MAX_PARTITION_CANDIDATES,
            "result_limit": max_rows, "consistency": "catalog, eligibility and LONG reread; not a transactional snapshot"}
    reader.evidence["partition_scan"] = scan
    before = _partition_catalog(reader, "before")
    candidates, table_count = _partition_candidates(before)
    key_columns = {}
    for key in before["keys"]:
        key_columns.setdefault((key["OWNER"], key["NAME"]), set()).add(key["COLUMN_NAME"])
    scan.update(metadata_rows=len(before["partitions"]), open_owner_tables=table_count,
                penultimate_candidates=len(candidates), decisions=[])
    eligibility = []
    selected = []
    for candidate in candidates:
        state = _partition_eligibility(reader, candidate, "before")
        eligibility.append(state)
        columns = key_columns.get((candidate["TABLE_OWNER"], candidate["TABLE_NAME"]), set())
        date_key = any(row["COLUMN_NAME"] in columns for row in state["types"])
        qualifies = date_key and not state["recycled"]
        scan["decisions"].append({"owner": candidate["TABLE_OWNER"], "table_name": candidate["TABLE_NAME"],
                                  "partition_name": candidate["PARTITION_NAME"], "date_key": date_key,
                                  "recycled": bool(state["recycled"]), "selected": qualifies})
        if qualifies:
            selected.append(candidate)
    if len(selected) > max_rows:
        reader.evidence["truncated"] = True
        raise RuntimeError("Matching partitions exceed section row limit; coverage is incomplete")
    boundaries = [_partition_boundary(reader, candidate, "before") for candidate in selected]
    # Detect catalog changes rather than publishing a silently mixed observation.
    if _partition_catalog(reader, "after") != before:
        raise RuntimeError("Partition catalog changed during collection")
    for candidate, state in zip(candidates, eligibility):
        if _partition_eligibility(reader, candidate, "after") != state:
            raise RuntimeError("Partition eligibility changed during collection")
    for candidate, boundary in zip(selected, boundaries):
        if _partition_boundary(reader, candidate, "after") != boundary:
            raise RuntimeError("Partition LONG boundary changed during collection")
    scan.update(complete=True, selected_partitions=len(selected), long_reads=len(selected) * 2)
    columns = ["USERNAME", "TABLENAME", "PARTNAME", "MAX_RANGE"]
    return columns, [{key: row[key] for key in columns} for row in boundaries]


def execute_connected_pdb_section(section, conn, *, max_rows, expected_container=None):
    """Use the configured PDB only; never switch sessions or imply CDB-wide coverage."""
    evidence = {
        "adapter": "oracle_connected_pdb_compat_v1",
        "adaptation": {
            "database_patch": "Probe the connected PDB dictionary. Preserve SOURCE_VERSION/TARGET_VERSION and use TARGET_VERSION as VERSION on modern dictionaries. Without BUNDLE_ID retain both sources with RECORD_SOURCE.",
            "range_par_extend": "Scan partition metadata once per verification pass, keep MAX(position)-1 and original eligibility predicates, then fetch LONG only by exact partition identity. Recheck catalog, eligibility and boundaries; MAX_RANGE remains the unevaluated expression.",
            "autotask_windows": "Read the connected PDB's scheduler windows; render timestamps server-side with FF9, TZR, TZD and TZH:TZM.",
        }[section.key],
        "original_sql": section.sql, "original_sql_sha256": sha256(section.sql.encode()).hexdigest(),
        "template_sha256": section.template_sha256,
        "actual_statements": [], "container_coverage": [], "columns": [], "rows": [],
        "truncated": False, "coverage_complete": False, "cdb_coverage_complete": False,
        "scope": "connected_pdb_only", "read_only": True, "session_changes_only": False,
        "session_changes": [],
        "max_rows": max_rows,
        "limitations": ["Only the configured PDB is inspected; other containers were not queried."],
    }
    original_timeout = conn.call_timeout
    try:
        if evidence["original_sql_sha256"] != SECTION_HASHES[section.key]:
            raise RuntimeError("Oracle template SQL changed; adapter requires explicit review")
        reader = _Reader(conn, evidence)
        identity = reader.identity()
        evidence["original_container"] = identity
        if identity["con_id"] <= 2:
            raise RuntimeError("Read-only PDB adapter cannot provide CDB-wide coverage from root or seed")
        if expected_container and identity["name"] != expected_container:
            raise RuntimeError("Connected PDB differs from the detected inspection profile")
        if section.key == "database_patch":
            queries = _patch_queries(reader.dictionary("DBA_REGISTRY_SQLPATCH"))
        elif section.key == "range_par_extend":
            if reader.dictionary("DBA_TAB_PARTITIONS").get("HIGH_VALUE") != "LONG":
                raise RuntimeError("Expected DBA_TAB_PARTITIONS.HIGH_VALUE LONG dictionary field")
            queries = []
        else:
            queries = [WINDOWS_SQL]
        batches = [_collect_partition_rows(reader, max_rows)] if section.key == "range_par_extend" else (
            reader.query(sql, limit=max_rows, kind="data") for sql in queries
        )
        for columns, rows in batches:
            for column in ["CONTAINER_NAME", "CON_ID", *columns]:
                if column not in evidence["columns"]:
                    evidence["columns"].append(column)
            for row in rows:
                if section.key == "range_par_extend" and not row.get("MAX_RANGE"):
                    raise RuntimeError("Partition boundary was not collected")
                if len(evidence["rows"]) >= max_rows:
                    evidence["truncated"] = True
                    raise RuntimeError("Combined result exceeded section row limit")
                evidence["rows"].append({"CONTAINER_NAME": identity["name"], "CON_ID": identity["con_id"], **row})
        if reader.identity() != identity:
            raise RuntimeError("Connected container changed during collection")
        evidence["container_coverage"] = [{**identity, "row_count": len(evidence["rows"]),
                                           "status": "success" if evidence["rows"] else "no_matching_rows"}]
        evidence["coverage_complete"] = True
        return evidence
    except Exception as exc:
        evidence["error"] = str(exc)
        raise OracleCompatibilityError(str(exc), evidence) from exc
    finally:
        conn.call_timeout = original_timeout


def execute_cdb_section(section, connection_factory, *, max_rows, shared_connection=None):
    """Own and close a new connection; never switch the caller's collector session."""
    evidence = {
        "adapter": "oracle_cdb_compat_v1", "adaptation": NOTES[section.key],
        "original_sql": section.sql, "original_sql_sha256": sha256(section.sql.encode()).hexdigest(),
        "template_sha256": section.template_sha256,
        "actual_statements": [], "container_coverage": [], "columns": [], "rows": [],
        "truncated": False, "coverage_complete": False, "original_container_restored": False,
        "scope": "all_non_seed_containers", "excluded_con_ids": [2],
        "read_only": True, "session_changes_only": True, "max_rows": max_rows,
    }
    try:
        if evidence["original_sql_sha256"] != SECTION_HASHES[section.key]:
            raise RuntimeError("Oracle template SQL changed; adapter requires explicit review")
        if connection_factory is None:
            raise RuntimeError("A dedicated Oracle connection is required for container-aware compatibility")
        with connection_factory() as conn:
            if conn is shared_connection:
                raise RuntimeError("Refusing to switch the shared collector connection")
            conn.call_timeout = 10000
            if conn.transaction_in_progress:
                raise RuntimeError("Refusing a connection with an active transaction")
            reader = _Reader(conn, evidence)
            original = reader.identity()
            evidence["original_container"] = original
            error = None
            try:
                if original != {"con_id": 1, "name": "CDB$ROOT"}:
                    raise RuntimeError("CDB-wide adapter requires a root connection; PDB-only coverage is not complete")
                _, database = reader.query("SELECT dbid, name, cdb FROM v$database", limit=1)
                if len(database) != 1 or database[0]["CDB"] != "YES":
                    raise RuntimeError("Dedicated connection is not a CDB")
                evidence["database"] = database[0]
                _, version = reader.query("SELECT version FROM v$instance", limit=1)
                evidence["version"] = version
                if int(version[0]["VERSION"].split(".")[0]) >= 18:
                    _, evidence["version_full"] = reader.query("SELECT version_full FROM v$instance", limit=1)
                before = _inventory(reader)
                evidence["expected_containers"] = before[0]
                for container in before[0]:
                    target = {"con_id": int(container["CON_ID"]), "name": container["NAME"]}
                    coverage = {**target, "status": "running", "row_count": 0}
                    evidence["container_coverage"].append(coverage)
                    try:
                        if reader.container != target:
                            reader.switch(target)
                        elif reader.identity() != target:
                            raise RuntimeError("Container identity changed before collection")
                        if section.key == "database_patch":
                            queries = _patch_queries(reader.dictionary("DBA_REGISTRY_SQLPATCH"))
                        elif section.key == "range_par_extend":
                            dictionary = reader.dictionary("DBA_TAB_PARTITIONS")
                            if dictionary.get("HIGH_VALUE") != "LONG":
                                raise RuntimeError("Expected DBA_TAB_PARTITIONS.HIGH_VALUE LONG dictionary field")
                            queries = [RANGE_SQL]
                        else:
                            queries = [WINDOWS_SQL]
                        for sql in queries:
                            columns, rows = reader.query(sql, limit=max_rows, kind="data")
                            for column in ["CONTAINER_NAME", "CON_ID", *columns]:
                                if column not in evidence["columns"]:
                                    evidence["columns"].append(column)
                            for row in rows:
                                if section.key == "range_par_extend" and not row.get("MAX_RANGE"):
                                    raise RuntimeError("Partition boundary was not collected")
                                if len(evidence["rows"]) >= max_rows:
                                    evidence["truncated"] = True
                                    raise RuntimeError("Combined container result exceeded section row limit")
                                evidence["rows"].append({"CONTAINER_NAME": target["name"], "CON_ID": target["con_id"], **row})
                                coverage["row_count"] += 1
                        if reader.identity() != target:
                            raise RuntimeError("Container identity changed during collection")
                        coverage["status"] = "success" if coverage["row_count"] else "no_matching_rows"
                    except Exception as exc:
                        coverage.update(status="failed", error=str(exc))
                        raise
                if reader.container != original:
                    reader.switch(original)
                if _inventory(reader) != before:
                    raise RuntimeError("Container inventory changed during collection")
            except Exception as exc:
                error = exc
            finally:
                # Restore even after a failed switch: server-side state may have changed.
                try:
                    if reader.identity(restoring=True) != original:
                        reader.switch(original, restoring=True)
                    evidence["original_container_restored"] = True
                except Exception as restore_error:
                    evidence["restore_error"] = str(restore_error)
                    error = RuntimeError(f"{error or 'Collection finished'}; original container restoration failed: {restore_error}")
            if error is not None:
                raise error
        evidence["coverage_complete"] = True
        return evidence
    except Exception as exc:
        evidence["error"] = str(exc)
        raise OracleCompatibilityError(str(exc), evidence) from exc
