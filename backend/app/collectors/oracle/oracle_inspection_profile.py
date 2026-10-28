from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts"

SCRIPT_CATALOG = {
    "10g": {
        "script_name": "dbcheck10g.sql",
        "label": "Oracle legacy / 10g compatible",
    },
    "noncdb": {
        "script_name": "dbcheck_noncdb.sql",
        "label": "Oracle non-CDB 11g/12c+",
    },
    "cdb": {
        "script_name": "dbcheck_cdb.sql",
        "label": "Oracle CDB 12c+",
    },
}


@dataclass
class OracleInspectionProfile:
    version: str
    major_version: int | None
    cdb: str | None
    con_name: str | None
    script_key: str
    script_name: str
    script_path: str
    script_label: str
    selection_reason: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["script_exists"] = Path(self.script_path).exists()
        return data


def parse_major_version(version: str | None) -> int | None:
    match = re.match(r"\s*(\d+)", str(version or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def select_oracle_script(version: str | None, cdb: str | None = None) -> tuple[str, str]:
    major = parse_major_version(version)
    cdb_value = str(cdb or "").strip().upper()
    if major is not None and major <= 10:
        return "10g", "version major <= 10, use legacy SQL compatible with dbcheck10g.sql"
    if cdb_value == "YES":
        return "cdb", "v$database.cdb=YES, use CDB SQL with cdb_* / PDB awareness"
    return "noncdb", "non-CDB or pre-12c database, use dbcheck_noncdb.sql"


def detect_oracle_profile(conn) -> OracleInspectionProfile:
    warnings: list[str] = []
    version = _safe_scalar(conn, "SELECT version FROM v$instance", warnings, "version") or ""
    major = parse_major_version(version)
    if major is not None and major >= 18:
        version = (
            _safe_scalar(
                conn,
                "SELECT version_full FROM v$instance",
                warnings,
                "full version",
            )
            or version
        )

    cdb: str | None = None
    if major is None or major >= 12:
        cdb = _safe_scalar(conn, "SELECT cdb FROM v$database", warnings, "cdb")

    con_name: str | None = None
    if str(cdb or "").strip().upper() == "YES":
        con_name = _safe_scalar(
            conn,
            "SELECT sys_context('USERENV', 'CON_NAME') FROM dual",
            warnings,
            "container name",
        )

    script_key, reason = select_oracle_script(version, cdb)
    script = SCRIPT_CATALOG[script_key]
    script_path = str(SCRIPT_DIR / script["script_name"])
    if not Path(script_path).exists():
        warnings.append(f"Script file not found: {script_path}")

    return OracleInspectionProfile(
        version=version,
        major_version=major,
        cdb=str(cdb).upper() if cdb is not None else None,
        con_name=con_name,
        script_key=script_key,
        script_name=script["script_name"],
        script_path=script_path,
        script_label=script["label"],
        selection_reason=reason,
        warnings=warnings,
    )


def profile_from_values(version: str | None, cdb: str | None = None) -> OracleInspectionProfile:
    script_key, reason = select_oracle_script(version, cdb)
    script = SCRIPT_CATALOG[script_key]
    return OracleInspectionProfile(
        version=str(version or ""),
        major_version=parse_major_version(version),
        cdb=str(cdb).upper() if cdb is not None else None,
        con_name=None,
        script_key=script_key,
        script_name=script["script_name"],
        script_path=str(SCRIPT_DIR / script["script_name"]),
        script_label=script["label"],
        selection_reason=reason,
        warnings=[],
    )


def _safe_scalar(conn, sql: str, warnings: list[str], label: str) -> str | None:
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        row = cursor.fetchone()
        if not row:
            return None
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()), None)
        return str(value).strip() if value is not None else None
    except Exception as exc:
        warnings.append(f"{label} detection failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        if cursor is not None:
            cursor.close()
