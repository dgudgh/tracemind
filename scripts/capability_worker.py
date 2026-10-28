"""Run with python scripts/capability_worker.py --once."""
from __future__ import annotations

import json
import logging

from _bootstrap import add_backend_to_path

add_backend_to_path()

from app.tasks.plan_worker import main


class WorkerLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {"event": record.getMessage(), "level": record.levelname, "logger": record.name}
        execution = getattr(record, "execution", None)
        if execution is not None:
            payload["execution"] = execution
        return json.dumps(payload, sort_keys=True)


if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.setFormatter(WorkerLogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    raise SystemExit(main())
