"""Three-way merge for the shared env file across releases.

The previous behaviour was "copy the template if the file is missing, otherwise return". An
upgrade therefore shipped with whatever keys the file happened to have: every key added by the
new release was silently absent, and the failure only surfaced later as a setting that "does
nothing" (ISS-42). A two-way merge is not enough either - it cannot tell "the operator changed
this" from "the release changed this", so it either clobbers local edits or refuses to bring in
new defaults.

Three-way needs a base: the template as of the version that last wrote the file. It is kept
beside the config as ``app.env.base``. Without it (fresh install, or a file predating this
change) the merge degrades to two-way and says so in the report instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ASSIGNMENT = "="


@dataclass
class MergeReport:
    strategy: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    backup_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "added": list(self.added),
            "updated": list(self.updated),
            "kept_local": list(self.kept_local),
            "removed": list(self.removed),
            "conflicts": list(self.conflicts),
            "backup_path": self.backup_path,
        }


def _parse(text: str) -> tuple[list[str], dict[str, str]]:
    """Split env text into (lines in order, key -> value). Comments and blanks stay in order."""
    lines = text.splitlines()
    values: dict[str, str] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ASSIGNMENT not in stripped:
            continue
        name, _, value = stripped.partition(ASSIGNMENT)
        values[name.strip()] = value
    return lines, values


def _render(base_lines: Iterable[str], values: dict[str, str], order: list[str]) -> str:
    rendered: list[str] = []
    seen: set[str] = set()
    for line in base_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ASSIGNMENT not in stripped:
            rendered.append(line)
            continue
        name, _, _value = stripped.partition(ASSIGNMENT)
        name = name.strip()
        if name in values:
            rendered.append(f"{name}{ASSIGNMENT}{values[name]}")
            seen.add(name)
        # a key that no longer exists is dropped, which is how removals are expressed
    for name in order:
        if name in seen or name not in values:
            continue  # absent on purpose: that is how a removal is expressed
        rendered.append(f"{name}{ASSIGNMENT}{values[name]}")
        seen.add(name)
    return "\n".join(rendered) + ("\n" if rendered else "")


def merge_env(base_text: str | None, ours_text: str, theirs_text: str) -> tuple[str, MergeReport]:
    """Merge ours (installed config) with theirs (new template) using base as the common ancestor.

    Local edits win on conflict: an upgrade must never silently rewrite a value an operator set
    by hand. Conflicts are reported so the operator is told, instead of discovering it at 3am.
    """
    ours_lines, ours = _parse(ours_text)
    theirs_lines, theirs = _parse(theirs_text)
    if base_text is None:
        base: dict[str, str] = {}
        report = MergeReport(strategy="two_way")
    else:
        _base_lines, base = _parse(base_text)
        report = MergeReport(strategy="three_way")

    merged: dict[str, str] = {}
    order: list[str] = list(ours.keys())
    for name in theirs:
        if name not in order:
            order.append(name)

    for name in order:
        in_ours = name in ours
        in_theirs = name in theirs
        in_base = name in base
        if in_theirs and not in_ours and not in_base:
            merged[name] = theirs[name]
            report.added.append(name)
            continue
        if in_theirs and not in_ours and in_base:
            # Removed locally while the release still ships it: respect the local removal.
            report.kept_local.append(name)
            continue
        if in_ours and not in_theirs:
            if in_base:
                # Dropped by the release. Keep it only if the operator touched it.
                if ours[name] != base[name]:
                    merged[name] = ours[name]
                    report.kept_local.append(name)
                else:
                    report.removed.append(name)
            else:
                merged[name] = ours[name]
                report.kept_local.append(name)
            continue
        if in_ours and in_theirs:
            if not in_base:
                if ours[name] == theirs[name]:
                    merged[name] = ours[name]
                else:
                    merged[name] = ours[name]
                    report.conflicts.append(name)
                    report.kept_local.append(name)
                continue
            if ours[name] == base[name]:
                merged[name] = theirs[name]
                if ours[name] != theirs[name]:
                    report.updated.append(name)
                continue
            if theirs[name] == base[name]:
                merged[name] = ours[name]
                report.kept_local.append(name)
                continue
            merged[name] = ours[name]
            report.conflicts.append(name)
            report.kept_local.append(name)
    return _render(ours_lines, merged, order), report


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
