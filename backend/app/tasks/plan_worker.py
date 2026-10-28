"""Importable capability worker; CLI lives in scripts/capability_worker.py."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import uuid

from sqlalchemy.exc import SQLAlchemyError

from app.services.plan_queue import QueueError, process_one

logger = logging.getLogger(__name__)

_embedded_lock = threading.Lock()
_embedded_thread: threading.Thread | None = None
_embedded_stop: threading.Event | None = None


def run_worker(session_factory, *, worker_id, once=False, stop=None,
               idle_seconds=0.5, max_backoff=15.0, worker_identity=None):
    """SIGTERM stops new claims; in-flight work finishes with its heartbeat still alive."""
    if not 0 < idle_seconds <= max_backoff <= 300:
        raise ValueError("invalid_worker_backoff")
    stop = stop if stop is not None else threading.Event()
    delay = idle_seconds
    while not stop.is_set():
        try:
            if worker_identity is None:
                result = process_one(session_factory, worker_id=worker_id)
            else:
                # The queue repository must filter before its database claim.
                # An older repository that lacks this contract raises TypeError
                # before it can claim a plan; never fall back to unfiltered work.
                result = process_one(session_factory, worker_id=worker_id,
                                     worker_identity=worker_identity)
        except SQLAlchemyError:
            logger.warning("capability_worker_storage_unavailable")
            if once:
                return 1
            stop.wait(delay)
            delay = min(delay * 2, max_backoff)
            continue
        except QueueError as exc:
            logger.error("capability_worker_refused code=%s", exc.code)
            return 2
        except TypeError:
            if worker_identity is None:
                raise
            logger.exception("managed_capability_worker_identity_contract_unavailable")
            return 2
        if result is not None:
            logger.info("capability_plan_processed", extra={"execution": result})
        if once:
            return 0
        if result is None:
            stop.wait(delay)
            delay = min(delay * 2, max_backoff)
        else:
            delay = idle_seconds
    return 0


def start_embedded_worker(
    session_factory=None,
    *,
    enabled: bool | None = None,
    idle_seconds: float = 0.5,
    max_backoff: float = 15.0,
):
    """Start the process-local durable-plan consumer once.

    The queue's database claim is the cross-process concurrency boundary. This
    guard only prevents duplicate consumer threads inside one API process.
    ``session_factory`` is injectable so lifecycle tests never touch runtime
    metadata storage.
    """
    global _embedded_thread, _embedded_stop

    if enabled is None:
        from app.core.config import settings

        enabled = settings.CAPABILITY_WORKER_ENABLED
    if not enabled:
        logger.info("embedded_capability_worker_disabled")
        return None
    if not 0 < idle_seconds <= max_backoff <= 300:
        raise ValueError("invalid_worker_backoff")

    with _embedded_lock:
        if _embedded_thread is not None and _embedded_thread.is_alive():
            return _embedded_thread
        if session_factory is None:
            # Resolve lazily so application tests can replace SessionLocal before
            # lifespan starts and never connect to the configured runtime store.
            from app.core import database

            session_factory = database.SessionLocal
        stop = threading.Event()
        worker_id = f"embedded:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"

        def consume():
            try:
                exit_code = run_worker(
                    session_factory,
                    worker_id=worker_id,
                    stop=stop,
                    idle_seconds=idle_seconds,
                    max_backoff=max_backoff,
                )
            except Exception:
                logger.exception("embedded_capability_worker_crashed worker_id=%s", worker_id)
                return
            if exit_code:
                logger.error("embedded_capability_worker_stopped code=%s", exit_code)

        thread = threading.Thread(
            target=consume,
            name="capability-plan-worker",
            daemon=False,
        )
        _embedded_stop = stop
        _embedded_thread = thread
        try:
            thread.start()
        except Exception:
            _embedded_thread = None
            _embedded_stop = None
            logger.exception("embedded_capability_worker_start_failed")
            raise
        logger.info("embedded_capability_worker_started worker_id=%s", worker_id)
        return thread


def stop_embedded_worker():
    """Stop new claims and wait for any in-flight plan to finish."""
    global _embedded_thread, _embedded_stop

    with _embedded_lock:
        thread, stop = _embedded_thread, _embedded_stop
        if thread is None:
            return
        if stop is not None:
            stop.set()
        # run_worker observes the event during idle/backoff and after its current
        # process_one call. Waiting without a timeout preserves its no-false-stop
        # contract for an in-flight external operation.
        if thread is not threading.current_thread():
            thread.join()
        if _embedded_thread is thread:
            _embedded_thread = None
            _embedded_stop = None
        logger.info("embedded_capability_worker_stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consume durable capability plans")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}")
    parser.add_argument("--idle-seconds", type=float, default=0.5)
    parser.add_argument("--max-backoff", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not 0 < args.idle_seconds <= args.max_backoff <= 300:
        parser.error("require 0 < idle-seconds <= max-backoff <= 300")
    # Importing the session factory does not initialize/migrate schema or start the platform.
    from app.core.database import SessionLocal

    stop = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        return run_worker(SessionLocal, worker_id=args.worker_id, once=args.once, stop=stop,
                          idle_seconds=args.idle_seconds, max_backoff=args.max_backoff)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
