"""Bounded fixture call scheduler: queue, inflight limit, cancel and deadlines."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class SchedulerError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class ScheduledCall:
    fn: Callable[[], dict]
    deadline: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: Exception | None = None
    state: str = "queued"
    done: threading.Event = field(default_factory=threading.Event)


class BoundedScheduler:
    def __init__(self, *, max_queue: int = 256, max_inflight: int = 8):
        if max_queue < 1 or max_inflight < 1:
            raise SchedulerError("invalid_limit")
        self.max_queue = max_queue
        self.max_inflight = max_inflight
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._inflight = 0
        self._lock = threading.Lock()
        self._closed = False
        self._workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(max_inflight)]
        for worker in self._workers:
            worker.start()

    def submit(self, fn: Callable[[], dict], *, timeout: float) -> ScheduledCall:
        if self._closed:
            raise SchedulerError("scheduler_closed")
        if timeout <= 0:
            raise SchedulerError("invalid_deadline")
        call = ScheduledCall(fn=fn, deadline=time.monotonic() + timeout)
        try:
            self._queue.put_nowait(call)
        except queue.Full as exc:
            raise SchedulerError("queue_full") from exc
        return call

    def _worker(self):
        while True:
            call = self._queue.get()
            if call is None:
                return
            if call.cancel_event.is_set() or call.deadline <= time.monotonic():
                call.state = "cancelled" if call.cancel_event.is_set() else "timeout"
                call.done.set()
                continue
            call.state = "running"
            try:
                call.result = call.fn()
                call.state = "succeeded"
            except Exception as exc:  # noqa: BLE001
                call.error = exc
                call.state = "failed"
            finally:
                call.done.set()

    def cancel(self, call: ScheduledCall) -> bool:
        if call.state == "queued":
            call.cancel_event.set()
            call.state = "cancelled"
            call.done.set()
            return True
        if call.state == "running":
            call.cancel_event.set()
            return False
        return False

    def wait(self, call: ScheduledCall, *, timeout: float | None = None) -> dict:
        if not call.done.wait(timeout):
            raise SchedulerError("call_timeout")
        if call.error is not None:
            raise call.error
        if call.state != "succeeded":
            raise SchedulerError(call.state)
        return call.result or {}

    def close(self):
        self._closed = True
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=1)
