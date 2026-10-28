"""Explicit C08 migration: index completed inspection jobs that predate automatic indexing.

    python scripts/backfill_inspection_reports.py --dry-run
    python scripts/backfill_inspection_reports.py --limit 200

This never deletes results and never rewrites a job; it only adds the missing report artifact.
A failure is printed and the process exits non-zero, leaving that job unindexed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal
from app.services.inspection import report_backfill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        result = report_backfill.backfill(db, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
