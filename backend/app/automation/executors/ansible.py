import os
import tempfile

from app.automation.executors.process import run_cancellable_process


class AnsibleExecutor:
    executor_type = "ansible"

    def execute(self, plan, cancel_check=None) -> dict:
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".sh", delete=False) as handle:
                handle.write(plan.script)
                path = handle.name
            os.chmod(path, 0o700)
            return run_cancellable_process(
                ["ansible", plan.target_host, "-m", "script", "-a", path],
                timeout=int(plan.timeout_seconds or 300),
                cancel_check=cancel_check,
            )
        finally:
            if path:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def probe_identity(self, plan) -> str:
        result = run_cancellable_process(
            ["ansible", plan.target_host, "-m", "command", "-a", "hostname", "-o"],
            timeout=min(30, int(plan.timeout_seconds or 300)),
        )
        if result["exit_code"] != 0:
            raise ValueError(result["stderr"] or "target identity probe failed")
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        return lines[-1] if lines else ""
