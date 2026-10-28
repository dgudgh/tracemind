"""Run notification/report regression without loading workspace secrets or databases."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
TESTS = [
    "backend/tests/test_notify_delivery.py",
    "backend/tests/test_report_center.py",
    "backend/tests/test_report_subscription_delivery.py",
    "backend/tests/test_notification_scheduler.py",
    "backend/tests/test_master_scheduler_ha.py",
]


def single_round():
    import pytest

    sys.path.insert(0, str(ROOT / "backend"))
    with tempfile.TemporaryDirectory(prefix="notify-isolated-") as root:
        isolated = {
            "PATH": os.environ.get("PATH", ""),
            "DATABASE_URL": "sqlite://",
            "DB_TYPE": "sqlite",
            "APP_MODE": "test",
            "SECRET_KEY": "isolated-notification-test-key",
            "JWT_PRIVATE_KEY_PATH": str(Path(root) / "test-signing.pem"),
        }
        with patch.dict(os.environ, isolated, clear=True):
            # Settings computes default secrets before environment overrides, so
            # its initial runtime root and .env location must also be isolated.
            with patch.object(sys, "frozen", True, create=True), patch(
                "app.runtime.paths.ensure_frozen_runtime_root", return_value=Path(root),
            ):
                from app.core.config import settings

            assert settings.DATABASE_URL == "sqlite://"
            return pytest.main(["-q", *TESTS])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--single-round", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.single_round:
        return single_round()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    for number in range(1, args.rounds + 1):
        print(f"Notification regression round {number}/{args.rounds}", flush=True)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--single-round"], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
