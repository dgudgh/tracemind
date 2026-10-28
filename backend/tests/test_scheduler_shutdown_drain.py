import threading
from unittest.mock import MagicMock, patch

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_SUBMITTED

from app.services import report_center
from app.tasks import master_scheduler


def test_shutdown_does_not_release_lease_while_job_still_runs():
    entered = threading.Event()
    finish = threading.Event()
    done = threading.Event()
    shutdown_started = threading.Event()
    submitted = threading.Event()
    released = threading.Event()
    premature = []

    def job():
        entered.set()
        finish.wait(5)
        done.set()

    def release(*args):
        premature.append(not done.is_set())
        released.set()

    scheduler = BackgroundScheduler()
    scheduler.add_listener(lambda event: submitted.set(), EVENT_JOB_SUBMITTED)
    scheduler.add_job(job, "date")
    scheduler.start()
    db = MagicMock()
    shutdown = scheduler.shutdown

    def observed_shutdown(*args, **kwargs):
        shutdown_started.set()
        return shutdown(*args, **kwargs)

    with (patch.object(master_scheduler, "_scheduler", scheduler),
          patch.object(master_scheduler, "SessionLocal", return_value=db),
          patch.object(scheduler, "shutdown", side_effect=observed_shutdown),
          patch.object(report_center, "release_scheduler_lease", side_effect=release)):
        stopper = threading.Thread(target=master_scheduler.stop_master_scheduler)
        try:
            assert entered.wait(2), "scheduled test job did not start"
            assert submitted.wait(2), "scheduler did not finish dispatching the job"
            stopper.start()
            assert shutdown_started.wait(2)
            released_early = released.wait(0.15)
        finally:
            finish.set()
            if stopper.ident is not None:
                stopper.join(3)
            elif scheduler.running:
                shutdown(wait=True)
        assert not stopper.is_alive()
        assert not released_early
        assert premature == [False]
        assert done.is_set() and released.is_set()
        db.close.assert_called_once()
