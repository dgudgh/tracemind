#!/usr/bin/env python
"""TraceMind AI quick launcher.

Default mode:
  - Backend API: uvicorn on http://127.0.0.1:8800
  - Frontend: Node static server on http://127.0.0.1:3300

Environment variables:
  BACKEND_PORT=8800
  FRONTEND_PORT=3300
  FRONTEND_DEV=true       Use Vite dev server instead of static build.
  UVICORN_RELOAD=true     Enable backend reload.
  BACKEND_PYTHON=...      Force backend Python interpreter.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"

BACKEND_PORT = os.getenv("BACKEND_PORT", "8800")
FRONTEND_PORT = os.getenv("FRONTEND_PORT", "3300")

ProcessEntry = tuple[str, subprocess.Popen]
procs: list[ProcessEntry] = []


def find_backend_python() -> str | None:
    """Return the first Python interpreter that can import backend dependencies."""
    forced = os.getenv("BACKEND_PYTHON")
    candidates = [
        forced,
        sys.executable,
        str(ROOT_DIR / ".venv-mac-primary" / "bin" / "python"),
        str(ROOT_DIR / ".venv-mac" / "bin" / "python"),
        str(ROOT_DIR / ".venv" / "Scripts" / "python.exe"),
        str(ROOT_DIR / ".venv" / "bin" / "python"),
        r"D:\python\python.exe",
        "python",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [candidate, "-c", "import fastapi, uvicorn"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, FileNotFoundError):
            continue
        if result.returncode == 0:
            return candidate
    return None


BACKEND_PYTHON = find_backend_python()


def bundled_dependency_dirs() -> list[Path]:
    """Return bundled runtime bin directories, if this Codex desktop runtime exists."""
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    candidates: list[Path] = []
    primary = runtime_root / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node"
    candidates.append(primary)
    if runtime_root.exists():
        candidates.extend(sorted(runtime_root.glob("*/dependencies/node/bin/node")))

    dirs: list[Path] = []
    seen: set[Path] = set()
    for node_path in candidates:
        if not node_path.is_file():
            continue
        node_bin = node_path.parent
        deps_dir = node_path.parents[2]
        for path in (
            node_bin,
            deps_dir / "bin" / "override",
            deps_dir / "bin" / "fallback",
            deps_dir / "bin",
        ):
            if path.is_dir() and path not in seen:
                seen.add(path)
                dirs.append(path)
    return dirs


def frontend_env() -> dict[str, str]:
    """Build a subprocess-only env with bundled Node/Pnpm prepended when available."""
    env = os.environ.copy()
    extra_dirs = bundled_dependency_dirs()
    if extra_dirs:
        current_path = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([str(path) for path in extra_dirs] + ([current_path] if current_path else []))
    return env


def resolve_frontend_command(name: str, env: dict[str, str]) -> str | None:
    command = name + ".cmd" if os.name == "nt" and name in {"npm", "pnpm"} else name
    return shutil.which(command, path=env.get("PATH"))


def popen(cmd: list[str], cwd: Path, env: dict[str, str] | None = None, name: str = "") -> subprocess.Popen:
    print(f"[启动] {name}: {' '.join(cmd)}  (cwd={cwd})", flush=True)
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env)
    procs.append((name, proc))
    return proc


def start_backend() -> subprocess.Popen:
    if not BACKEND_PYTHON:
        raise RuntimeError("未找到已安装 fastapi/uvicorn 的 Python 解释器")

    env = os.environ.copy()
    env["SERVE_STATIC"] = "false"
    reload_enabled = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    cmd = [
        BACKEND_PYTHON,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        BACKEND_PORT,
    ]
    if reload_enabled:
        cmd += ["--reload", "--reload-dir", str(BACKEND_DIR)]
    return popen(cmd, BACKEND_DIR, env=env, name="backend")


def start_frontend() -> subprocess.Popen:
    env = frontend_env()
    env.setdefault("FRONTEND_HOST", "0.0.0.0")
    env.setdefault("FRONTEND_PORT", FRONTEND_PORT)
    env.setdefault("BACKEND_HOST", "127.0.0.1")
    env.setdefault("BACKEND_PORT", BACKEND_PORT)

    dev_enabled = os.getenv("FRONTEND_DEV", "false").lower() == "true"
    if dev_enabled:
        package_runner = resolve_frontend_command("pnpm", env) or resolve_frontend_command("npm", env)
        if not package_runner:
            raise RuntimeError(
                "未找到 pnpm/npm，无法启动前端 dev server；请安装 Node.js，或确认 Codex bundled runtime 存在"
            )
        runner_name = Path(package_runner).name.lower()
        arg_separator = [] if runner_name.startswith("pnpm") else ["--"]
        cmd = [package_runner, "run", "dev"] + arg_separator + ["--host", "0.0.0.0", "--port", FRONTEND_PORT]
        return popen(cmd, FRONTEND_DIR, env=env, name="frontend(vite-dev)")

    node = resolve_frontend_command("node", env)
    if not node:
        raise RuntimeError(
            "未找到 node，无法启动前端静态服务；请安装 Node.js，或确认 Codex bundled runtime 存在"
        )
    cmd = [node, "serve-3000.cjs"]
    return popen(cmd, FRONTEND_DIR, env=env, name="frontend(static)")


def stop_processes() -> None:
    print("\n[停止] 正在关闭服务...", flush=True)
    for name, proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
        except Exception:
            print(f"[停止] {name} terminate 失败，尝试 kill", flush=True)
            proc.kill()

    deadline = time.time() + 8
    for _name, proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            proc.kill()
    print("[停止] 已退出。", flush=True)


def shutdown(*_: object) -> None:
    stop_processes()
    raise SystemExit(0)


def main() -> int:
    print("=" * 56)
    print("  TraceMind AI - 智能运维平台")
    print("=" * 56)
    print(f"  后端 API : http://127.0.0.1:{BACKEND_PORT}")
    print(f"  前端页面 : http://127.0.0.1:{FRONTEND_PORT}")
    print("  监听地址 : 0.0.0.0")
    print("  按 Ctrl+C 停止全部服务")
    print("=" * 56)

    if not BACKEND_PYTHON:
        print("[错误] 找不到已安装后端依赖 fastapi/uvicorn 的 Python 解释器。")
        print(r"       请执行: D:\python\python.exe -m pip install -r backend\requirements.txt")
        print("       或设置 BACKEND_PYTHON=<python路径> 后重新启动。")
        return 1

    print(f"  后端解释器: {BACKEND_PYTHON}")
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        start_backend()
        time.sleep(1.5)
        start_frontend()

        while True:
            for name, proc in procs:
                ret = proc.poll()
                if ret is not None:
                    print(f"[退出] 进程 {name} 已结束 (code={ret})，停止其它服务。", flush=True)
                    shutdown()
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()
    except Exception as exc:
        print(f"[错误] 启动失败: {exc}", flush=True)
        stop_processes()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
