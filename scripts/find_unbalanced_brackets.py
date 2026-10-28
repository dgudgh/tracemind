#!/usr/bin/env python3
"""Find markdown bracket imbalance -- the signature left by links whose "](" and ")" were eaten.

A line containing ``[label](target)`` has equal numbers of ``[`` and ``]``. When a tool replaces
the whole ``](target)`` match with just the target, the ``]`` disappears, so the line ends up with
strictly more ``[`` than ``]``. That is a far more reliable signal than trying to re-split the
run, because it does not care whether the label contains spaces or Chinese punctuation.

Code fences are skipped: brackets in code are not links.

Usage
-----
    python3 scripts/find_unbalanced_brackets.py            # report
    python3 scripts/find_unbalanced_brackets.py --context  # show the offending lines
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

REPO = Path(__file__).resolve().parent.parent

SKIP_DIR_PARTS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".superpowers", ".trae",
    ".venv", ".venv-mac", ".venv-mac-primary", ".deps-py312", "dist", "build",
    "runtime-backups", "runtime-logs",
    # Generated artefacts: the front-end audit logs embed raw JSON inside backticks, so their
    # brackets are unbalanced by nature. They are output, not documentation.
    "reports", "exports",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    if args.files:
        files = [Path(p) for p in args.files]
    else:
        files = [p for p in sorted(REPO.rglob("*.md"))
                 if not any(part in SKIP_DIR_PARTS for part in p.relative_to(REPO).parts)]

    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(REPO).as_posix() if path.is_absolute() else str(path)
        in_fence = False
        bad: list[tuple[int, str]] = []
        for n, line in enumerate(text.split("\n"), 1):
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # Inline code is not a link. A document that *quotes* broken link syntax (for example
            # a report explaining "]( was eaten") would otherwise be flagged as broken itself.
            prose = INLINE_CODE_RE.sub("", line)
            if prose.count("[") > prose.count("]"):
                bad.append((n, line))
        if bad:
            total += len(bad)
            print(f"{rel}  ({len(bad)} line(s) with more '[' than ']')")
            if args.context:
                for n, line in bad[:6]:
                    print(f"    {n}: {line[:150]}")
    print()
    print(f"[DETECTED] unbalanced lines: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
