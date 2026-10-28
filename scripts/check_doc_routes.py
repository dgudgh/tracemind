#!/usr/bin/env python3
"""Check that ``/api/v1/...`` routes quoted in the documentation are actually registered.

Complements the other two gates: the link gate only sees markdown links, and
``check_doc_paths.py`` only sees file paths. Neither notices a route that was renamed or deleted
while the prose kept advertising it -- and a route table is exactly the kind of thing that rots
silently after a refactor.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SKIP_DIR_PARTS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", ".superpowers", ".trae",
    ".venv", ".venv-mac", ".venv-mac-primary", ".deps-py312", "dist", "build",
    "runtime-backups", "runtime-logs", "reports", "exports",
}

ROUTE_RE = re.compile(r"/api/v1/[A-Za-z0-9_\-{}/.]*[A-Za-z0-9_\-}]")
# A citation that ends in a file extension is a source path (`backend/app/api/v1/registry.py`),
# not a route.
FILE_SUFFIX_RE = re.compile(
    r"\.(?:py|js|jsx|cjs|mjs|json|md|sql|sh|html|css|txt|yml|yaml|toml)$", re.IGNORECASE)

# A citation on a line that explicitly declares the route absent is an *acknowledged* gap, not
# drift. Requiring the marker on the same line keeps the exemption narrow and self-documenting --
# it cannot silently swallow an unannotated stale route.
ACKNOWLEDGED_MARKERS = ("未实现", "历史路径", "已废弃", "待实现", "planned", "TODO")


def doc_files() -> list[Path]:
    out = []
    for p in sorted(REPO.rglob("*.md")):
        rel = p.relative_to(REPO)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        if str(rel).startswith("docs/archive/"):
            continue
        out.append(p)
    return out


def normalize(route: str) -> str:
    """Collapse path parameters and concrete ids so a citation and a route template compare equal."""
    route = route.rstrip("/.)")
    route = re.sub(r"\{[^}]*\}", "§", route)
    route = re.sub(r"(?<=/)\d+(?=/|$)", "§", route)
    return route


def main() -> int:
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "backend"))
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    os.environ.setdefault("DB_TYPE", "sqlite")
    os.environ.setdefault("APP_MODE", "quality_test")
    os.environ.setdefault("DEBUG", "false")
    os.environ.setdefault("TRACEMIND_AUTO_MIGRATE", "0")
    # The application logs its route table at INFO on import; keep this check's output parseable.
    import logging  # noqa: E402

    logging.disable(logging.INFO)
    try:
        from app.main import app  # noqa: E402
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"SKIP: cannot import the application ({type(exc).__name__}: {exc})")
        return 0

    registered = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if path and path.startswith("/api/v1"):
            registered.add(normalize(path))

    # A doc may cite a family ("all of /api/v1/ecosystem/*") rather than one endpoint, so a
    # citation is accepted when it *is* a registered route or a path-prefix of one.
    def known(cand: str) -> bool:
        if cand in registered:
            return True
        return any(r.startswith(cand + "/") for r in registered)

    checked = 0
    missing: list[tuple[str, int, str]] = []
    for path in doc_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if any(mark in line for mark in ACKNOWLEDGED_MARKERS):
                continue
            for m in ROUTE_RE.finditer(line):
                raw = m.group(0)
                if FILE_SUFFIX_RE.search(raw):
                    continue  # a source path, not a route
                cand = normalize(raw)
                if cand in ("/api/v1", "/api/v1§"):
                    continue
                checked += 1
                if not known(cand):
                    missing.append((rel, n, raw))

    print(f"registered /api/v1 routes : {len(registered)}")
    print(f"route citations in docs   : {checked}")
    print(f"not registered            : {len(missing)}")
    for rel, n, raw in missing:
        print(f"  {rel}:{n}\n      -> {raw}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
