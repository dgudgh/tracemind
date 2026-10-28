import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

from app.runtime.paths import ensure_frozen_runtime_root

logger = logging.getLogger("exe_launcher")

FRONTEND_PORT = 3300
BACKEND_PORT = 8800


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        port = int(value)
    except ValueError:
        logger.warning("Invalid %s=%r, fallback to %d", name, value, default)
        return default
    if port < 1 or port > 65535:
        logger.warning("Invalid %s=%r, fallback to %d", name, value, default)
        return default
    return port


def load_env_file(env_path: str):
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value:
                os.environ[key] = value
    logger.info("Loaded env from %s", env_path)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, timeout: int = 30):
    start = time.time()
    while time.time() - start < timeout:
        if port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def service_is_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def show_error(message: str) -> None:
    logger.error(message)
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        try:
            import subprocess

            escaped = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'display alert "TraceMind AI 启动失败" message "{escaped}" as critical',
                ],
                check=False,
                timeout=10,
            )
        except Exception:
            pass


def start_backend_in_thread() -> Tuple[threading.Thread, threading.Event, threading.Event, Dict[str, Optional[str]]]:
    startup_ready = threading.Event()
    startup_failed = threading.Event()
    startup_error: Dict[str, Optional[str]] = {"message": None}

    def run():
        try:
            import uvicorn
            from app.main import app
        except ModuleNotFoundError as exc:
            startup_error["message"] = (
                f"启动后端失败：缺少 Python 包 `{exc.name}`。请确认打包时已将后端依赖打包。"
            )
            startup_failed.set()
            return
        except Exception as exc:
            startup_error["message"] = f"启动后端失败：{exc}"
            startup_failed.set()
            return

        startup_ready.set()
        try:
            uvicorn.run(app, host="127.0.0.1", port=BACKEND_PORT, log_level="info")
        except Exception as exc:
            startup_error["message"] = f"后端服务异常退出：{exc}"
            startup_failed.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info("Backend thread started on port %d", BACKEND_PORT)
    return t, startup_ready, startup_failed, startup_error


def start_frontend_static(static_dir: str, backend_port: int):
    if not os.path.isdir(static_dir):
        logger.error("Static dir not found: %s", static_dir)
        return None

    try:
        from app.runtime.frontend_server import create_frontend_app
        import uvicorn
    except ImportError:
        logger.error("frontend_server module not available")
        return None

    def run_server():
        app = create_frontend_app(static_dir, backend_port)
        uvicorn.run(app, host="127.0.0.1", port=FRONTEND_PORT, log_level="warning")

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    logger.info("Frontend static server started on port %d", FRONTEND_PORT)
    return t


def main():
    global BACKEND_PORT, FRONTEND_PORT

    if getattr(sys, "frozen", False):
        base_dir = str(ensure_frozen_runtime_root())
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    Path(base_dir).mkdir(parents=True, exist_ok=True)
    os.chdir(base_dir)

    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(log_dir, "init.log"), encoding="utf-8"),
        ],
    )

    env_candidates = (
        os.path.join(base_dir, "config", "app.env"),
        os.path.join(base_dir, ".env"),
    )
    loaded_env_path = next((path for path in env_candidates if os.path.isfile(path)), "")
    if loaded_env_path:
        load_env_file(loaded_env_path)
        logger.info("Loaded config from %s", loaded_env_path)
    else:
        logger.info("No external config file found; using embedded SQLite defaults")

    db_url = os.environ.get("DATABASE_URL", "")
    db_scheme = db_url.partition("://")[0] if "://" in db_url else "unknown"
    logger.info("DATABASE_URL configured=%s | scheme=%s", bool(db_url), db_scheme)

    BACKEND_PORT = _env_int("BACKEND_PORT", BACKEND_PORT)
    FRONTEND_PORT = _env_int("FRONTEND_PORT", FRONTEND_PORT)
    logger.info("Configured ports: backend=%d, frontend=%d", BACKEND_PORT, FRONTEND_PORT)

    serve_static = False
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", "")
        bundle_static = os.path.join(bundle_root, "static") if bundle_root else ""
        external_static = os.path.join(base_dir, "static")
        static_dir = bundle_static if os.path.isdir(bundle_static) else external_static
        if os.path.isdir(static_dir):
            os.environ["SERVE_STATIC"] = "true"
            os.environ["STATIC_DIR"] = static_dir
            logger.info("SERVE_STATIC from %s", static_dir)
            serve_static = True
        else:
            logger.warning(
                "No static directory found at bundle=%s or external=%s",
                bundle_static,
                external_static,
            )

    if port_in_use(BACKEND_PORT):
        if service_is_healthy(BACKEND_PORT):
            access_port = FRONTEND_PORT if port_in_use(FRONTEND_PORT) else BACKEND_PORT
            logger.info("TraceMind AI is already running; opening existing service")
            webbrowser.open(f"http://localhost:{access_port}")
            return
        show_error(f"端口 {BACKEND_PORT} 已被其他程序占用，请关闭占用程序后重试。")
        return

    backend_thread, backend_ready_event, backend_failed_event, backend_error = start_backend_in_thread()
    backend_ready_event.wait(1.0)
    logger.info("Waiting for backend to be ready...")
    if backend_failed_event.is_set():
        show_error(backend_error["message"] or "后端启动失败")
        return
    if not wait_for_port(BACKEND_PORT, timeout=30):
        if backend_failed_event.is_set():
            show_error(backend_error["message"] or "后端启动失败")
            return
        show_error("后端服务在 30 秒内未能启动，请查看运行日志。")
        return

    access_port = BACKEND_PORT

    if not serve_static:
        static_dir = os.path.join(base_dir, "static")
        frontend_thread = start_frontend_static(static_dir, BACKEND_PORT)
        if frontend_thread:
            logger.info("Waiting for frontend to be ready...")
            if wait_for_port(FRONTEND_PORT, timeout=15):
                access_port = FRONTEND_PORT
            else:
                logger.warning("Frontend failed to start on port %d", FRONTEND_PORT)
        else:
            logger.warning("Frontend static server not started, using backend port")

    logger.info("Opening browser...")
    webbrowser.open(f"http://localhost:{access_port}")

    logger.info("TraceMindAI is running on port %d. Press Ctrl+C to stop.", access_port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
