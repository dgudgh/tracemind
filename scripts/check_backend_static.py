from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_APP = ROOT / "backend" / "app"
API_V1_DIR = BACKEND_APP / "api" / "v1"
API_REGISTRY = API_V1_DIR / "registry.py"
API_MODULE_ALLOWLIST = {"__init__", "registry"}
CAPACITY_API = API_V1_DIR / "capacity.py"
SCHEDULER_API = API_V1_DIR / "scheduler.py"
GENERIC_DB_OPS = BACKEND_APP / "services" / "generic_db_ops.py"
MODELS_DIR = BACKEND_APP / "models"
MODELS_INIT = MODELS_DIR / "__init__.py"
SQLITE_INIT_SERVICE = BACKEND_APP / "services" / "sqlite_init_service.py"
LEGACY_SCHEDULER_FILE = BACKEND_APP / "tasks" / "perf_metric_scheduler.py"
STANDALONE_SCHEDULER_CONTROLS = {
    BACKEND_APP / "tasks" / "capacity_scheduler.py": {
        "start_capacity_scheduler",
        "stop_capacity_scheduler",
        "get_scheduler_status",
    },
    BACKEND_APP / "tasks" / "awr_collect_scheduler.py": {
        "start_awr_collect_scheduler",
        "stop_awr_collect_scheduler",
    },
}
ALLOWED_MAIN_GUARDS = {
    Path("backend/app/runtime/exe_launcher.py"),
}
SUSPICIOUS_TEXT_PATTERNS = [
    ("mojibake", re.compile("\u951f")),
    (
        "common-mojibake",
        re.compile(r"[\u9427\u93ba\u9422\u7487\u95bf\u935a\u7035\u6ae5\u7d8d\u7e43\u93c8\u71c2\u7d1d\u5678\u93c2\u626e\u8930\u7e3a\u5a32]"),
    ),
    ("question-placeholder", re.compile(r"\?{4,}")),
]


def iter_python_files() -> list[Path]:
    return [
        path
        for path in BACKEND_APP.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def is_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left = test.left
    right = test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name)
        and right.id == "__name__"
        and isinstance(left, ast.Constant)
        and left.value == "__main__"
    )


def check_file(path: Path) -> list[dict[str, object]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    findings: list[dict[str, object]] = []

    for node in tree.body:
        relative_path = path.relative_to(ROOT)
        if (
            isinstance(node, ast.If)
            and is_main_guard(node.test)
            and relative_path not in ALLOWED_MAIN_GUARDS
        ):
            findings.append(
                {
                    "kind": "main-guard",
                    "line": node.lineno,
                    "message": "backend/app modules should not contain ad-hoc __main__ test blocks",
                }
            )
    for line_no, line in enumerate(source.splitlines(), 1):
        if "\ufffd" in line:
            findings.append(
                {
                    "kind": "replacement-character",
                    "line": line_no,
                    "message": "backend/app source should not contain Unicode replacement characters",
                }
            )
        for kind, pattern in SUSPICIOUS_TEXT_PATTERNS:
            if pattern.search(line):
                findings.append(
                    {
                        "kind": kind,
                        "line": line_no,
                        "message": "backend/app source should not contain suspicious mojibake or unresolved placeholders",
                    }
                )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            findings.append(
                {
                    "kind": "print-call",
                    "line": node.lineno,
                    "message": "backend/app source should use structured logging instead of print()",
                }
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "breakpoint"
        ):
            findings.append(
                {
                    "kind": "breakpoint",
                    "line": node.lineno,
                    "message": "backend/app source should not contain active breakpoint() calls",
                }
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_trace"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"pdb", "ipdb"}
        ):
            findings.append(
                {
                    "kind": "debugger",
                    "line": node.lineno,
                    "message": "backend/app source should not contain active debugger calls",
                }
            )

    return findings


def get_registered_api_modules() -> set[str]:
    tree = ast.parse(API_REGISTRY.read_text(encoding="utf-8"), filename=str(API_REGISTRY))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.api.v1":
            for alias in node.names:
                modules.add(alias.name)
    return modules


def check_orphan_api_modules() -> list[dict[str, object]]:
    registered = get_registered_api_modules()
    reports = []
    for path in sorted(API_V1_DIR.glob("*.py")):
        module_name = path.stem
        if module_name in API_MODULE_ALLOWLIST:
            continue
        if module_name not in registered:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": [
                        {
                            "kind": "orphan-api-module",
                            "line": 1,
                            "message": "api/v1 module is not registered in backend/app/api/v1/registry.py",
                        }
                    ],
                }
            )
    return reports


def has_sqlalchemy_model(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Base":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "Base":
                return True
    return False


def iter_model_classes(path: Path) -> list[tuple[str, ast.ClassDef]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.name, node)
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id == "Base")
            or (isinstance(base, ast.Attribute) and base.attr == "Base")
            for base in node.bases
        )
    ]


def get_tablename(model_class: ast.ClassDef) -> tuple[str, int] | None:
    for node in model_class.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "__tablename__"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return node.value.value, node.lineno
    return None


def get_registered_model_modules() -> set[str]:
    tree = ast.parse(MODELS_INIT.read_text(encoding="utf-8"), filename=str(MODELS_INIT))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.models."):
            modules.add(node.module.rsplit(".", 1)[-1])
    return modules


def check_unregistered_model_modules() -> list[dict[str, object]]:
    registered = get_registered_model_modules()
    reports = []
    for path in sorted(MODELS_DIR.glob("*.py")):
        if path.name == "__init__.py" or not has_sqlalchemy_model(path):
            continue
        if path.stem not in registered:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": [
                        {
                            "kind": "unregistered-model-module",
                            "line": 1,
                            "message": "SQLAlchemy model module is not imported in backend/app/models/__init__.py",
                        }
                    ],
                }
            )
    return reports


def check_duplicate_models() -> list[dict[str, object]]:
    reports = []
    class_locations: dict[str, list[tuple[Path, int]]] = {}
    table_locations: dict[str, list[tuple[Path, str, int]]] = {}

    for path in sorted(MODELS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for class_name, model_class in iter_model_classes(path):
            class_locations.setdefault(class_name, []).append((path, model_class.lineno))
            tablename = get_tablename(model_class)
            if tablename:
                table_name, line = tablename
                table_locations.setdefault(table_name, []).append((path, class_name, line))

    for class_name, locations in sorted(class_locations.items()):
        if len(locations) <= 1:
            continue
        for path, line in locations:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": [
                        {
                            "kind": "duplicate-model-class",
                            "line": line,
                            "message": f"SQLAlchemy model class name is duplicated: {class_name}",
                        }
                    ],
                }
            )

    for table_name, locations in sorted(table_locations.items()):
        if len(locations) <= 1:
            continue
        for path, class_name, line in locations:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": [
                        {
                            "kind": "duplicate-table-name",
                            "line": line,
                            "message": f"SQLAlchemy table name is duplicated: {table_name} ({class_name})",
                        }
                    ],
                }
            )

    return reports


def is_create_all_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_all"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "metadata"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "Base"
    )


def is_import_runtime_schema_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "app.core.database"
        and any(alias.name == "initialize_schema" for alias in node.names)
    )


def is_runtime_schema_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "initialize_schema"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "engine"
        and any(
            keyword.arg == "allow_upgrade"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    )


def check_sqlite_init_schema_guards() -> list[dict[str, object]]:
    tree = ast.parse(SQLITE_INIT_SERVICE.read_text(encoding="utf-8"), filename=str(SQLITE_INIT_SERVICE))
    findings = []
    init_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "init_sqlite_database"
        ),
        None,
    )
    if init_fn is None:
        return [
            {
                "file": str(SQLITE_INIT_SERVICE.relative_to(ROOT)).replace("\\", "/"),
                "findings": [
                    {
                        "kind": "sqlite-init-missing",
                        "line": 1,
                        "message": "sqlite init service should expose init_sqlite_database",
                    }
                ],
            }
        ]

    runtime_import_line = next((node.lineno for node in init_fn.body if is_import_runtime_schema_guard(node)), None)
    create_all_line = next(
        (
            node.lineno
            for stmt in init_fn.body
            for node in ast.walk(stmt)
            if is_create_all_call(node)
        ),
        None,
    )
    runtime_call_line = next(
        (
            node.lineno
            for stmt in init_fn.body
            for node in ast.walk(stmt)
            if is_runtime_schema_call(node)
        ),
        None,
    )

    if create_all_line is not None:
        findings.append(
            {
                "kind": "sqlite-init-unmanaged-create-all",
                "line": create_all_line,
                "message": "sqlite init must not call create_all outside the managed schema initializer",
            }
        )
    if (
        runtime_import_line is None
        or runtime_call_line is None
        or runtime_import_line > runtime_call_line
    ):
        findings.append(
            {
                "kind": "sqlite-init-runtime-schema",
                "line": init_fn.lineno,
                "message": "sqlite init must import and call initialize_schema(engine, allow_upgrade=True)",
            }
        )

    if not findings:
        return []
    return [
        {
            "file": str(SQLITE_INIT_SERVICE.relative_to(ROOT)).replace("\\", "/"),
            "findings": findings,
        }
    ]


def check_capacity_scheduler_status_binding() -> list[dict[str, object]]:
    tree = ast.parse(CAPACITY_API.read_text(encoding="utf-8"), filename=str(CAPACITY_API))
    findings = []
    has_master_status_import = False

    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        imported_names = {alias.name for alias in node.names}
        if node.module == "app.tasks.master_scheduler" and "get_scheduler_status" in imported_names:
            has_master_status_import = True
        if node.module == "app.tasks.capacity_scheduler" and "get_scheduler_status" in imported_names:
            findings.append(
                {
                    "kind": "capacity-obsolete-scheduler-status",
                    "line": node.lineno,
                    "message": "capacity scheduler status should reflect the unified master scheduler",
                }
            )

    if not has_master_status_import:
        findings.append(
            {
                "kind": "capacity-master-scheduler-status-missing",
                "line": 1,
                "message": "capacity scheduler status should import get_scheduler_status from master_scheduler",
            }
        )

    if not findings:
        return []
    return [
        {
            "file": str(CAPACITY_API.relative_to(ROOT)).replace("\\", "/"),
            "findings": findings,
        }
    ]


def check_legacy_scheduler_dead_code() -> list[dict[str, object]]:
    reports = []
    if LEGACY_SCHEDULER_FILE.exists():
        reports.append(
            {
                "file": str(LEGACY_SCHEDULER_FILE.relative_to(ROOT)).replace("\\", "/"),
                "findings": [
                    {
                        "kind": "legacy-perf-scheduler-module",
                        "line": 1,
                        "message": "perf metric collection is handled by master_scheduler; standalone scheduler module should not return",
                    }
                ],
            }
        )

    for path, banned_names in STANDALONE_SCHEDULER_CONTROLS.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings = [
            {
                "kind": "legacy-standalone-scheduler-control",
                "line": node.lineno,
                "message": f"{node.name} is obsolete; use master_scheduler for lifecycle/status",
            }
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in banned_names
        ]
        if findings:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": findings,
                }
            )
    return reports


def check_scheduler_run_now_status_binding() -> list[dict[str, object]]:
    tree = ast.parse(SCHEDULER_API.read_text(encoding="utf-8"), filename=str(SCHEDULER_API))
    findings = []
    direct_collect_imports = {
        ("app.services.capacity_monitor", "collect_snapshot"),
        ("app.services.awr_collect", "run_collect"),
        ("app.services.perf_baseline", "collect_perf_metrics"),
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (node.module, alias.name) in direct_collect_imports:
                    findings.append(
                        {
                            "kind": "scheduler-run-now-bypasses-policy-status",
                            "line": node.lineno,
                            "message": "scheduler run-now should use run_collect_by_policy so last_status/last_message stay current",
                        }
                    )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "round"
        ):
            findings.append(
                {
                    "kind": "scheduler-interval-rounding",
                    "line": node.lineno,
                    "message": "scheduler interval conversion should not use banker's rounding",
                }
            )
    if not findings:
        return []
    return [
        {
            "file": str(SCHEDULER_API.relative_to(ROOT)).replace("\\", "/"),
            "findings": findings,
        }
    ]


def check_generic_ops_backup_menu_split() -> list[dict[str, object]]:
    tree = ast.parse(GENERIC_DB_OPS.read_text(encoding="utf-8"), filename=str(GENERIC_DB_OPS))
    findings = []
    modules_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MODULES" for target in node.targets)
    ]
    for assignment in modules_assignments:
        for node in ast.walk(assignment.value):
            if not isinstance(node, ast.Dict):
                continue
            values = node.values
            for key_node, value_node in zip(node.keys, values):
                if (
                    isinstance(key_node, ast.Constant)
                    and key_node.value == "key"
                    and isinstance(value_node, ast.Constant)
                    and value_node.value == "backupRecovery"
                ):
                    findings.append(
                        {
                            "kind": "backup-recovery-inside-generic-ops-menu",
                            "line": value_node.lineno,
                            "message": "backup/recovery should stay in the independent backup center, not PG/SQL Server ops module lists",
                        }
                    )

    if not findings:
        return []
    return [
        {
            "file": str(GENERIC_DB_OPS.relative_to(ROOT)).replace("\\", "/"),
            "findings": findings,
        }
    ]


def check_generic_ops_backup_degraded_plan() -> list[dict[str, object]]:
    tree = ast.parse(GENERIC_DB_OPS.read_text(encoding="utf-8"), filename=str(GENERIC_DB_OPS))
    safe_collect_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_safe_collect"
        ),
        None,
    )
    if safe_collect_fn is None:
        return [
            {
                "file": str(GENERIC_DB_OPS.relative_to(ROOT)).replace("\\", "/"),
                "findings": [
                    {
                        "kind": "generic-ops-safe-collect-missing",
                        "line": 1,
                        "message": "generic DB ops should keep the guarded collector wrapper",
                    }
                ],
            }
        ]

    for handler in [node for node in ast.walk(safe_collect_fn) if isinstance(node, ast.ExceptHandler)]:
        has_backup_section_guard = any(
            isinstance(node, ast.Constant) and node.value == "backupRecovery"
            for node in ast.walk(handler)
        )
        has_backup_plan = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_plan"
            for node in ast.walk(handler)
        )
        if has_backup_section_guard and has_backup_plan:
            return []

    return [
        {
            "file": str(GENERIC_DB_OPS.relative_to(ROOT)).replace("\\", "/"),
            "findings": [
                {
                    "kind": "generic-ops-backup-degraded-plan-missing",
                    "line": safe_collect_fn.lineno,
                    "message": "legacy backupRecovery collection should still return a plan_only backup plan when live collection is degraded",
                }
            ],
        }
    ]


def main() -> int:
    reports = []
    for path in iter_python_files():
        findings = check_file(path)
        if findings:
            reports.append(
                {
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "findings": findings,
                }
            )
    reports.extend(check_orphan_api_modules())
    reports.extend(check_unregistered_model_modules())
    reports.extend(check_duplicate_models())
    reports.extend(check_sqlite_init_schema_guards())
    reports.extend(check_capacity_scheduler_status_binding())
    reports.extend(check_legacy_scheduler_dead_code())
    reports.extend(check_scheduler_run_now_status_binding())
    reports.extend(check_generic_ops_backup_menu_split())
    reports.extend(check_generic_ops_backup_degraded_plan())

    summary = {
        "checkedFileCount": len(iter_python_files()),
        "findingFileCount": len(reports),
        "findingCount": sum(len(report["findings"]) for report in reports),
        "findings": reports,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if reports else 0


if __name__ == "__main__":
    raise SystemExit(main())
