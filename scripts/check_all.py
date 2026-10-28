from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PYCACHE_PREFIX = ROOT / "build" / "pycache-check"


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def package_manager_command() -> str:
    candidates = ["npm.cmd", "pnpm.cmd"] if sys.platform.startswith("win") else ["npm", "pnpm"]
    return next((candidate for candidate in candidates if command_exists(candidate)), candidates[0])


def is_git_line_ending_warning(line: str) -> bool:
    return (
        line.startswith("warning: in the working copy of ")
        and "LF will be replaced by CRLF" in line
    )


def run_step(name: str, cmd: list[str], cwd: Path = ROOT, filter_git_line_endings: bool = False) -> int:
    print(f"\n=== {name} ===", flush=True)
    print(" ".join(cmd), flush=True)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=filter_git_line_endings,
            text=filter_git_line_endings,
        )
    except FileNotFoundError as exc:
        print(f"FAILED: {name} command not found: {exc.filename}", flush=True)
        return 127
    if filter_git_line_endings:
        output = "\n".join(
            line
            for line in (result.stdout + result.stderr).splitlines()
            if not is_git_line_ending_warning(line)
        )
        if output:
            print(output, flush=True)
    if result.returncode:
        print(f"FAILED: {name} exit={result.returncode}", flush=True)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TraceMind AI 智能运维平台 local validation checks.")
    parser.add_argument("--fast", action="store_true", help="Skip frontend production build.")
    parser.add_argument("--api-smoke", action="store_true", help="Probe no-parameter GET API routes on a running backend.")
    parser.add_argument("--frontend-smoke", action="store_true", help="Probe a running frontend static server and its proxied /health route.")
    parser.add_argument("--diagnosis-smoke", action="store_true", help="Create one local end-to-end diagnosis incident on a running backend.")
    args = parser.parse_args()

    node = "node"
    package_manager = package_manager_command()
    python = sys.executable
    compile_python = [python, "-X", f"pycache_prefix={PYCACHE_PREFIX}"]

    steps: list[tuple[str, list[str], Path, bool]] = [
        ("encoding", [python, "scripts/check_encoding.py"], ROOT, False),
        ("repository secrets", [python, "scripts/check_repository_secrets.py"], ROOT, False),
        ("docs links", [node, "scripts/check_docs_links.cjs"], ROOT, False),
        ("docs path citations", [python, "scripts/check_doc_paths.py"], ROOT, False),
        ("docs bracket balance", [python, "scripts/find_unbalanced_brackets.py"], ROOT, False),
        ("docs api routes", [python, "scripts/check_doc_routes.py"], ROOT, False),
        ("capability consistency", [python, "scripts/check_capability_consistency.py"], ROOT, False),
        ("backend routes", [python, "scripts/check_backend_routes.py"], ROOT, False),
        ("backend contract baseline", [python, "scripts/check_backend_contract_baseline.py"], ROOT, False),
        ("backend static", [python, "scripts/check_backend_static.py"], ROOT, False),
        ("runtime schema", [python, "scripts/check_runtime_schema.py"], ROOT, False),
        ("discovery delivery backend", [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "backend/tests/test_discovery_delivery_matrix.py",
            "backend/tests/test_cmdb_multiengine_credential_reuse.py",
            "backend/tests/test_oracle_dg_topology.py",
            "backend/tests/test_asset_topology_evidence.py",
            "backend/tests/test_oracle_discovery_peer_metadata.py",
            "backend/tests/test_pg_discovery_peer_metadata.py",
            "backend/tests/test_sqlserver_discovery_peer_metadata.py",
            "backend/tests/test_ssh_discovery_host_trust.py",
            "backend/tests/test_discovery_scope_integrity.py",
            "backend/tests/test_ha_diagnostics.py",
            "backend/tests/test_ha_topology_completeness.py",
            "backend/tests/test_cmdb_location_defaults.py"], ROOT, False),
        ("discovery delivery frontend", [node, "--test", "tests/asset-topology-layout.test.mjs",
            "tests/asset-discovery-failure.test.mjs"], FRONTEND, False),
        ("python scripts compileall", [*compile_python, "-m", "compileall", "-q", "scripts"], ROOT, False),
        ("backend compileall", [*compile_python, "-m", "compileall", "-q", "backend/app"], ROOT, False),
        ("entrypoint py_compile", [*compile_python, "-m", "py_compile", "run.py", "backend/run.py"], ROOT, False),
        ("frontend static", [package_manager, "run", "check:static"], FRONTEND, False),
        ("frontend menu contract", [node, "scripts/check_menu_contract.cjs"], ROOT, False),
        ("frontend server syntax", [node, "--check", "frontend/serve-3000.cjs"], ROOT, False),
        ("node script syntax", [node, "--check", "scripts/check_frontend_static.cjs"], ROOT, False),
        ("frontend smoke script syntax", [node, "--check", "scripts/check_frontend_smoke.cjs"], ROOT, False),
        ("docs links script syntax", [node, "--check", "scripts/check_docs_links.cjs"], ROOT, False),
        ("git diff whitespace", ["git", "diff", "--check"], ROOT, True),
    ]

    if not args.fast:
        steps.insert(5, ("frontend build", [package_manager, "run", "build"], FRONTEND, False))
    smoke_insert_index = 2
    if args.api_smoke:
        steps.insert(
            smoke_insert_index,
            ("api smoke", [python, "scripts/check_api_smoke.py", "--strict", "--timeout", "10", "--retries", "2"], ROOT, False),
        )
        smoke_insert_index += 1
    if args.frontend_smoke:
        steps.insert(smoke_insert_index, ("frontend smoke", [node, "scripts/check_frontend_smoke.cjs", "--check-health"], ROOT, False))
        smoke_insert_index += 1
    if args.diagnosis_smoke:
        steps.insert(
            smoke_insert_index,
            ("diagnosis smoke", [python, "scripts/check_diagnosis_smoke.py"], ROOT, False),
        )

    failed = []
    for name, cmd, cwd, filter_git_line_endings in steps:
        code = run_step(name, cmd, cwd, filter_git_line_endings)
        if code:
            failed.append((name, code))

    if failed:
        print("\nValidation failed:", flush=True)
        for name, code in failed:
            print(f"- {name}: exit={code}", flush=True)
        return 1

    print("\nAll validation checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
