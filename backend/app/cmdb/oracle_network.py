"""Resolve Oracle Net aliases through an already configured seed host.

The resolver is deliberately narrow: it opens one SSH session, runs fixed
read-only discovery commands, and turns ``tnsping`` descriptors into endpoint
candidates.  A successful result is not proof of database identity; callers
must still connect and verify ``db_unique_name`` before binding an endpoint.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any

from app.collectors.oracle.oracle_connection import _parse_oracle_connect_identifier
from app.services.ssh_executor import ssh_connection


MAX_ALIASES = 8
MAX_ORACLE_HOMES = 4
SSH_CONNECT_TIMEOUT_SECONDS = 8
SSH_COMMAND_TIMEOUT_SECONDS = 8

_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ATTEMPT_MARKER = re.compile(r"Attempting\s+to\s+contact\s+", re.IGNORECASE)
_OK_MARKER = re.compile(r"(?:^|\n)\s*OK\s*\(", re.IGNORECASE)

# The command does not interpolate instance data.  It only emits selected path
# values and never returns the rest of a process environment.
_DISCOVER_ORACLE_PATHS = r'''
(
  printf 'ENV|%s|%s\n' "${ORACLE_HOME-}" "${TNS_ADMIN-}"
  pmon_count=0
  for proc_dir in /proc/[0-9]*; do
    [ "$pmon_count" -ge 16 ] && break
    comm=$(cat "$proc_dir/comm" 2>/dev/null) || continue
    case "$comm" in
      ora_pmon_*|asm_pmon_*) ;;
      *) continue ;;
    esac
    executable=$(readlink -f "$proc_dir/exe" 2>/dev/null) || executable=''
    case "$executable" in
      */bin/oracle) process_home=${executable%/bin/oracle} ;;
      *) process_home='' ;;
    esac
    process_tns=$(tr '\000' '\n' < "$proc_dir/environ" 2>/dev/null | sed -n 's/^TNS_ADMIN=//p' | head -1)
    printf 'PMON|%s|%s|%s\n' "$comm" "$process_home" "$process_tns"
    pmon_count=$((pmon_count + 1))
  done
  if [ -r /etc/oratab ]; then
    awk -F: '
      /^[[:space:]]*#/ { next }
      NF >= 2 && $1 != "" && $1 != "*" && $2 ~ /^\// { print "ORATAB|" $1 "|" $2 }
    ' /etc/oratab | head -16
  fi
) | head -40
'''.strip()


@dataclass(frozen=True)
class _OracleHome:
    path: str
    tns_admin: str = ""
    origin: str = ""
    identity: str = ""


def _safe_remote_path(value: Any) -> str:
    """Accept a bounded absolute POSIX path observed on the remote host."""
    text = str(value or "").strip()
    if not text or len(text) > 512 or "\x00" in text or "\n" in text or "\r" in text:
        return ""
    if not text.startswith("/"):
        return ""
    normalized = posixpath.normpath(text)
    if normalized in {"/", "."} or not normalized.startswith("/"):
        return ""
    return normalized


def _configured_aliases(aliases: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in aliases or []:
        alias = str(value or "").strip()
        key = alias.casefold()
        if not _ALIAS_PATTERN.fullmatch(alias) or key in seen:
            continue
        seen.add(key)
        selected.append(alias)
        if len(selected) >= MAX_ALIASES:
            break
    return selected


def _source_identity(source: Any) -> set[str]:
    return {
        str(value or "").strip().casefold()
        for value in (
            getattr(source, "instance_name", ""),
            getattr(source, "sid", ""),
            getattr(source, "database_name", ""),
        )
        if str(value or "").strip()
    }


def _parse_oracle_homes(output: Any, source: Any) -> list[_OracleHome]:
    env_rows: list[_OracleHome] = []
    pmon_rows: list[_OracleHome] = []
    oratab_rows: list[_OracleHome] = []
    identities = _source_identity(source)

    for raw_line in str(output or "").splitlines()[:40]:
        fields = raw_line.split("|", 3)
        if not fields:
            continue
        kind = fields[0]
        if kind == "ENV" and len(fields) >= 3:
            home = _safe_remote_path(fields[1])
            if home:
                env_rows.append(_OracleHome(home, _safe_remote_path(fields[2]), "env"))
        elif kind == "PMON" and len(fields) >= 4:
            home = _safe_remote_path(fields[2])
            if home:
                process_name = str(fields[1] or "").strip()
                sid = process_name.split("ora_pmon_", 1)[-1] if "ora_pmon_" in process_name else process_name
                pmon_rows.append(_OracleHome(
                    home,
                    _safe_remote_path(fields[3]),
                    "pmon",
                    sid.casefold(),
                ))
        elif kind == "ORATAB" and len(fields) >= 3:
            home = _safe_remote_path(fields[2])
            if home:
                oratab_rows.append(_OracleHome(home, "", "oratab", str(fields[1]).strip().casefold()))

    pmon_rows.sort(key=lambda item: (item.identity not in identities, item.identity, item.path))
    oratab_rows.sort(key=lambda item: (item.identity not in identities, item.identity, item.path))

    homes: list[_OracleHome] = []
    indexes: dict[str, int] = {}
    for item in [*env_rows, *pmon_rows, *oratab_rows]:
        existing_index = indexes.get(item.path)
        if existing_index is not None:
            existing = homes[existing_index]
            if not existing.tns_admin and item.tns_admin:
                homes[existing_index] = _OracleHome(
                    existing.path, item.tns_admin, existing.origin, existing.identity,
                )
            continue
        if len(homes) >= MAX_ORACLE_HOMES:
            break
        indexes[item.path] = len(homes)
        homes.append(item)
    return homes


def _balanced_descriptor(output: Any) -> str:
    """Extract only the balanced descriptor following tnsping's marker."""
    text = str(output or "")
    marker = _ATTEMPT_MARKER.search(text)
    if marker is None:
        return ""
    start = text.find("(", marker.end())
    if start < 0:
        return ""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
            if depth < 0:
                return ""
    return ""


def _command_succeeded(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return False
    try:
        return int(result.get("exit_code", 0)) == 0
    except (TypeError, ValueError):
        return False


def _tnsping_command(home: _OracleHome, alias: str) -> str:
    executable = posixpath.join(home.path, "bin", "tnsping")
    assignments = ["LC_ALL=C", f"ORACLE_HOME={shlex.quote(home.path)}"]
    if home.tns_admin:
        assignments.append(f"TNS_ADMIN={shlex.quote(home.tns_admin)}")
    return (
        f"if [ -x {shlex.quote(executable)} ]; then "
        f"env {' '.join(assignments)} {shlex.quote(executable)} {shlex.quote(alias)}; "
        "fi"
    )


def _metadata_key(metadata: dict[str, Any]) -> tuple[Any, ...]:
    candidates = tuple(
        sorted(
            (str(item.get("host") or ""), int(item.get("port") or 0))
            for item in metadata.get("endpoint_candidates") or []
            if isinstance(item, dict)
        )
    )
    return (
        metadata.get("host") or "",
        int(metadata.get("port") or 0),
        metadata.get("oracle_connect_type") or "",
        metadata.get("service_name") or "",
        metadata.get("sid") or "",
        bool(metadata.get("endpoint_unresolved")),
        candidates,
    )


def _ambiguous_metadata(alias: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = []
    for item in observations:
        candidates = list(item.get("endpoint_candidates") or [])
        if item.get("host") and item.get("port"):
            candidates.append({"host": item["host"], "port": item["port"]})
        for candidate in candidates:
            endpoint = {
                "host": str(candidate.get("host") or "").strip(),
                "port": candidate.get("port"),
            }
            if endpoint["host"] and endpoint["port"] and endpoint not in endpoints:
                endpoints.append(endpoint)

    connect_identities = {
        (
            item.get("oracle_connect_type") or "",
            item.get("service_name") or "",
            item.get("sid") or "",
        )
        for item in observations
    }
    connect_type = service_name = sid = ""
    if len(connect_identities) == 1:
        connect_type, service_name, sid = next(iter(connect_identities))
    return {
        "host": "",
        "port": None,
        "oracle_connect_type": connect_type,
        "service_name": service_name,
        "sid": sid,
        "connect_identifier": alias,
        "endpoint_unresolved": True,
        "endpoint_candidates": endpoints,
        "resolution_source": "oracle_tnsping",
    }


def resolve_oracle_aliases(source: Any, aliases: list[str]) -> dict[str, dict]:
    """Resolve bounded Oracle aliases into unverified, sanitized endpoint metadata.

    Empty/invalid configuration, SSH failures, missing descriptors and failed
    ``tnsping`` checks all degrade to an empty result without exposing remote
    command output or exception details.
    """
    if str(getattr(source, "db_type", "") or "").strip().lower() != "oracle":
        return {}
    if not all((
        getattr(source, "ssh_host", None),
        getattr(source, "ssh_username", None),
        getattr(source, "ssh_password_cipher", None),
    )):
        return {}
    selected_aliases = _configured_aliases(aliases)
    if not selected_aliases:
        return {}

    try:
        with ssh_connection(source, timeout=SSH_CONNECT_TIMEOUT_SECONDS) as client:
            discovery = client.execute_command(
                _DISCOVER_ORACLE_PATHS,
                timeout=SSH_COMMAND_TIMEOUT_SECONDS,
            )
            if not _command_succeeded(discovery):
                return {}
            homes = _parse_oracle_homes(discovery.get("stdout"), source)
            if not homes:
                return {}

            resolved: dict[str, dict] = {}
            for alias in selected_aliases:
                observations: list[dict[str, Any]] = []
                observation_keys: set[tuple[Any, ...]] = set()
                for home in homes:
                    result = client.execute_command(
                        _tnsping_command(home, alias),
                        timeout=SSH_COMMAND_TIMEOUT_SECONDS,
                    )
                    output = result.get("stdout") if isinstance(result, dict) else ""
                    if not _command_succeeded(result) or not _OK_MARKER.search(str(output or "")):
                        continue
                    descriptor = _balanced_descriptor(output)
                    if not descriptor:
                        continue
                    metadata = _parse_oracle_connect_identifier(descriptor)
                    if not metadata.get("host") and not metadata.get("endpoint_candidates"):
                        continue
                    metadata["connect_identifier"] = alias
                    metadata["resolution_source"] = "oracle_tnsping"
                    key = _metadata_key(metadata)
                    if key not in observation_keys:
                        observation_keys.add(key)
                        observations.append(metadata)
                if len(observations) == 1:
                    resolved[alias] = observations[0]
                elif len(observations) > 1:
                    resolved[alias] = _ambiguous_metadata(alias, observations)
            return resolved
    except Exception:
        return {}
