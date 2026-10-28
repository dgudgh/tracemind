#!/usr/bin/env python3
from __future__ import annotations

from decimal import Decimal

from _bootstrap import add_backend_to_path

add_backend_to_path()

from app.services.observability.metric_mapping_service import _to_decimal


def main() -> int:
    assert _to_decimal("12.34") == Decimal("12.34")
    assert _to_decimal("NaN") is None
    assert _to_decimal(float("inf")) is None
    assert _to_decimal("-Infinity") is None
    print("observability metric non-finite value handling passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
