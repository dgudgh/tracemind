#!/usr/bin/env python3
"""Verify that file paths mentioned in prose actually exist.

The link gate only checks markdown links. Docs also cite paths in backticks and in plain prose
(``backend/app/services/report_center.py:869``), and those rot silently when files move.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".superpowers",
                  ".trae", ".venv", ".venv-mac", ".venv-mac-primary", ".deps-py312",
                  "dist", "build", "runtime-backups", "runtime-logs", "reports", "exports"}

# A repo-ish path: starts with a known top dir, contains a slash, ends with a known extension.
# The trailing lookahead stops "x.jsx" from matching inside "x.jsx.fragment".
PATH_RE = re.compile(
    r"(?<![\w/.-])"
    r"((?:backend|frontend|scripts|agent|docs|data|contracts|license_tool)/"
    r"(?:[\w./\u4e00-\u9fff-]+/)*[\w\u4e00-\u9fff.-]+"
    r"\.(?:py|jsx?|cjs|mjs|json|sql|md|sh|go|css|html|yml|yaml|toml|spec|txt|db|exe))"
    r"(?![.\w])"
)

# Names that are obviously illustrative rather than real citations.
PLACEHOLDER_STEMS = {"x", "foo", "bar", "baz", "example", "sample", "dummy", "placeholder",
                     "your_file", "some_file", "path", "file"}


def looks_like_placeholder(raw: str) -> bool:
    stem = Path(raw).stem.lower()
    return stem in PLACEHOLDER_STEMS


def resolves(doc: Path, raw: str) -> bool:
    """A citation may be relative to the file that cites it, or to the repository root."""
    return (doc.parent / raw).exists() or (REPO / raw).exists()


def main() -> int:
    files = []
    for p in sorted(REPO.rglob("*.md")):
        rel = p.relative_to(REPO)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        if str(rel).startswith("docs/archive/"):
            continue
        files.append(p)

    missing: list[tuple[str, str, int]] = []
    checked = 0
    for path in files:
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            for m in PATH_RE.finditer(line):
                raw = m.group(1)
                if looks_like_placeholder(raw):
                    continue
                checked += 1
                if not resolves(path, raw):
                    missing.append((rel, raw, n))

    print(f"live markdown files : {len(files)}")
    print(f"path citations      : {checked}")
    print(f"missing             : {len(missing)}")
    for rel, raw, n in missing:
        print(f"  {rel}:{n}\n      -> {raw}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
