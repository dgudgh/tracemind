import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable


def run_cancellable_process(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 300,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    started = time.monotonic()
    cancelled = False
    timed_out = False
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8"
    ) as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
        )
        if process.stdin is not None:
            process.stdin.write(input_text or "")
            process.stdin.close()

        while process.poll() is None:
            if cancel_check and cancel_check():
                cancelled = True
                _terminate_process_group(process)
                break
            if time.monotonic() - started >= timeout:
                timed_out = True
                _terminate_process_group(process)
                break
            time.sleep(0.2)

        if process.poll() is None:
            _terminate_process_group(process, force=True)
        return_code = process.wait()
        stdout_file.seek(0)
        stderr_file.seek(0)
        return {
            "exit_code": return_code,
            "stdout": stdout_file.read(),
            "stderr": stderr_file.read(),
            "cancelled": cancelled,
            "timed_out": timed_out,
        }


def _terminate_process_group(process: subprocess.Popen, force: bool = False) -> None:
    signal_value = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(process.pid, signal_value)
    except (ProcessLookupError, PermissionError):
        if force:
            process.kill()
        else:
            process.terminate()
    if not force:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, force=True)
