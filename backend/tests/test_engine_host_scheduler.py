from __future__ import annotations
import threading
from app.runtime.engine_host.scheduler import BoundedScheduler, SchedulerError


def test_queue_full_and_queued_cancel():
    scheduler = BoundedScheduler(max_queue=1, max_inflight=1)
    gate = threading.Event()
    first = scheduler.submit(lambda: gate.wait(2) and {"ok": 1}, timeout=2)
    while first.state == "queued":
        pass
    second = scheduler.submit(lambda: {"ok": 2}, timeout=2)
    with __import__("pytest").raises(SchedulerError) as error:
        scheduler.submit(lambda: {"ok": 3}, timeout=2)
    assert error.value.code == "queue_full"
    assert scheduler.cancel(second) is True
    gate.set()
    assert scheduler.wait(first) == {"ok": 1}
    scheduler.close()


def test_running_cancel_is_not_reported_success():
    scheduler = BoundedScheduler(max_queue=4, max_inflight=1)
    gate = threading.Event()
    call = scheduler.submit(lambda: gate.wait(2) and {"ok": 1}, timeout=2)
    while call.state != "running":
        pass
    assert scheduler.cancel(call) is False
    gate.set()
    assert scheduler.wait(call) == {"ok": 1}
    scheduler.close()
