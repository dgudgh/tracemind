from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
BASELINE_ROOT = ROOT / "contracts" / "baselines"
OPENAPI_BASELINE = BASELINE_ROOT / "openapi-v1.json"
SCHEMA_BASELINE = BASELINE_ROOT / "database-schema-v1.json"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _load_application():
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.core.database import Base
    from app.main import app

    return app, Base


def collect_openapi_contract() -> dict[str, Any]:
    app, _ = _load_application()
    return app.openapi()


def collect_database_contract() -> dict[str, Any]:
    _, base = _load_application()
    tables: dict[str, Any] = {}
    for table_name, table in sorted(base.metadata.tables.items()):
        columns = []
        for column in table.columns:
            columns.append(
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                    "primary_key": bool(column.primary_key),
                    "unique": bool(column.unique),
                    "foreign_keys": sorted(key.target_fullname for key in column.foreign_keys),
                }
            )
        indexes = [
            {
                "name": index.name,
                "columns": [column.name for column in index.columns],
                "unique": bool(index.unique),
            }
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        ]
        tables[table_name] = {"columns": columns, "indexes": indexes}
    return {"format_version": 1, "tables": tables}


def _operations(document: dict[str, Any]) -> dict[str, Any]:
    operations: dict[str, Any] = {}
    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS:
                operations[f"{method.upper()} {path}"] = operation
    return operations


def summarize_openapi_diff(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    baseline_operations = _operations(baseline)
    current_operations = _operations(current)
    baseline_schemas = baseline.get("components", {}).get("schemas", {})
    current_schemas = current.get("components", {}).get("schemas", {})
    shared_operations = baseline_operations.keys() & current_operations.keys()
    shared_schemas = baseline_schemas.keys() & current_schemas.keys()
    return {
        "added_operations": sorted(current_operations.keys() - baseline_operations.keys()),
        "removed_operations": sorted(baseline_operations.keys() - current_operations.keys()),
        "changed_operations": sorted(
            key for key in shared_operations if baseline_operations[key] != current_operations[key]
        ),
        "added_schemas": sorted(current_schemas.keys() - baseline_schemas.keys()),
        "removed_schemas": sorted(baseline_schemas.keys() - current_schemas.keys()),
        "changed_schemas": sorted(key for key in shared_schemas if baseline_schemas[key] != current_schemas[key]),
        "document_changed": baseline != current,
    }


def summarize_database_diff(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    baseline_tables = baseline.get("tables", {})
    current_tables = current.get("tables", {})
    shared_tables = baseline_tables.keys() & current_tables.keys()
    return {
        "added_tables": sorted(current_tables.keys() - baseline_tables.keys()),
        "removed_tables": sorted(baseline_tables.keys() - current_tables.keys()),
        "changed_tables": sorted(key for key in shared_tables if baseline_tables[key] != current_tables[key]),
        "document_changed": baseline != current,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _trim(summary: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    return {
        key: value[:limit] if isinstance(value, list) and len(value) > limit else value
        for key, value in summary.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the frozen OpenAPI and SQLAlchemy schema baselines.")
    parser.add_argument("--update", action="store_true", help="Replace baselines with the current contracts.")
    args = parser.parse_args()

    openapi = collect_openapi_contract()
    database = collect_database_contract()
    if args.update:
        _write_json(OPENAPI_BASELINE, openapi)
        _write_json(SCHEMA_BASELINE, database)
        print(
            json.dumps(
                {
                    "updated": True,
                    "openapi_operations": len(_operations(openapi)),
                    "openapi_schemas": len(openapi.get("components", {}).get("schemas", {})),
                    "database_tables": len(database["tables"]),
                },
                ensure_ascii=False,
            )
        )
        return 0

    missing = [str(path.relative_to(ROOT)) for path in (OPENAPI_BASELINE, SCHEMA_BASELINE) if not path.exists()]
    if missing:
        print(json.dumps({"ok": False, "missing_baselines": missing}, ensure_ascii=False, indent=2))
        print("Run scripts/check_backend_contract_baseline.py --update after reviewing intentional changes.")
        return 1

    openapi_diff = summarize_openapi_diff(_read_json(OPENAPI_BASELINE), openapi)
    database_diff = summarize_database_diff(_read_json(SCHEMA_BASELINE), database)
    ok = not openapi_diff["document_changed"] and not database_diff["document_changed"]
    print(
        json.dumps(
            {
                "ok": ok,
                "openapi": _trim(openapi_diff),
                "database": _trim(database_diff),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not ok:
        print("Contract drift detected. Review it, then use --update for an intentional contract change.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
