from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    logging.disable(logging.CRITICAL)

    from app.main import app

    seen: dict[tuple[str, str], list[str]] = defaultdict(list)
    operation_ids: dict[str, list[str]] = defaultdict(list)
    missing_operation_ids: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or []
        name = getattr(route, "name", "")
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            seen[(method, path)].append(name)
            if getattr(route, "include_in_schema", False):
                unique_id = getattr(route, "unique_id", None)
                route_label = f"{method} {path}"
                if unique_id:
                    operation_ids[unique_id].append(route_label)
                else:
                    missing_operation_ids.append(route_label)

    duplicates = {
        f"{method} {path}": names
        for (method, path), names in sorted(seen.items())
        if len(names) > 1
    }
    duplicate_operation_ids = {
        operation_id: routes
        for operation_id, routes in sorted(operation_ids.items())
        if len(routes) > 1
    }
    print(
        json.dumps(
            {
                "route_count": len(seen),
                "duplicate_count": len(duplicates),
                "operation_id_count": len(operation_ids),
                "duplicate_operation_id_count": len(duplicate_operation_ids),
                "missing_operation_id_count": len(missing_operation_ids),
            },
            ensure_ascii=False,
        )
    )
    if duplicates or duplicate_operation_ids or missing_operation_ids:
        if duplicates:
            print(json.dumps({"duplicate_routes": duplicates}, ensure_ascii=False, indent=2))
        if duplicate_operation_ids:
            print(json.dumps({"duplicate_operation_ids": duplicate_operation_ids}, ensure_ascii=False, indent=2))
        if missing_operation_ids:
            print(json.dumps({"missing_operation_ids": missing_operation_ids}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
