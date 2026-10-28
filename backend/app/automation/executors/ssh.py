from app.automation.executors.process import run_cancellable_process


class SSHExecutor:
    executor_type = "ssh"

    def execute(self, plan, cancel_check=None) -> dict:
        return run_cancellable_process(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", plan.target_host, "sh -s"],
            input_text=plan.script,
            timeout=int(plan.timeout_seconds or 300),
            cancel_check=cancel_check,
        )

    def probe_identity(self, plan) -> str:
        result = run_cancellable_process(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", plan.target_host, "hostname -f 2>/dev/null || hostname"],
            timeout=min(30, int(plan.timeout_seconds or 300)),
        )
        if result["exit_code"] != 0:
            raise ValueError(result["stderr"] or "target identity probe failed")
        return (result["stdout"].strip().splitlines() or [""])[-1].strip()
