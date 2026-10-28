#!/usr/bin/env python3
"""Targeted resilience checks for inspection persistence edge cases."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.inspection.runner import DB_INT_MAX, _bounded_db_int  # noqa: E402


def main() -> int:
    assert _bounded_db_int(None) is None
    assert _bounded_db_int("") is None
    assert _bounded_db_int("123") == 123
    assert _bounded_db_int(2_582_272_774_455) == DB_INT_MAX
    assert _bounded_db_int(-9_999_999_999) == -DB_INT_MAX - 1
    assert _bounded_db_int("not-a-number") is None
    print("inspection resilience checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
