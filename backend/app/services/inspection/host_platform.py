"""Read-only OS selection before choosing a native host inspection template."""
import base64

from app.services.ssh_executor import ssh_connection
from .windows_host_checks import _run_command


def detect_host_platform(instance):
    try:
        with ssh_connection(instance, timeout=10) as client:
            result = _run_command(client, "uname -s", timeout=10, max_bytes=4096)
            if any(result.get(key) for key in ("timed_out", "transport_error", "error", "output_limited")):
                raise ValueError("主机平台探测失败，未执行巡检")
            if result.get("exit_code") == 0 and result.get("stdout", "").strip() in {"Linux", "AIX", "HP-UX"}:
                return "unix"
            script = "[Console]::Write([Environment]::OSVersion.Platform.ToString())"
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            result = _run_command(client, "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded, timeout=10, max_bytes=4096)
            if any(result.get(key) for key in ("timed_out", "transport_error", "error", "output_limited")):
                raise ValueError("主机平台探测失败，未执行巡检")
            if result.get("exit_code") == 0 and not result.get("stderr") and result.get("stdout", "").strip() == "Win32NT":
                return "windows"
    except ValueError:
        raise
    except Exception:
        raise ValueError("无法连接主机进行平台识别，请检查 SSH 配置；未执行巡检") from None
    raise ValueError("未能识别受支持的主机平台；未执行巡检")
