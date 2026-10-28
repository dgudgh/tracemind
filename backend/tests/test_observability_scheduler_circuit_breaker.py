from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.observability import ObservabilityCollectPolicy, ObservabilitySource
from app.tasks import observability_scheduler


class FakeQuery:
    def __init__(self, items):
        self.items = items

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.items


class FakeSession:
    def __init__(self, policies, sources):
        self.policies = policies
        self.sources = sources
        self.rollback_count = 0
        self.closed = False

    def query(self, model):
        if model is ObservabilityCollectPolicy:
            return FakeQuery(self.policies)
        if model is ObservabilitySource:
            return FakeQuery(self.sources)
        raise AssertionError(f"Unexpected model: {model}")

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


class ObservabilitySchedulerCircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self.policies = [
            SimpleNamespace(
                id=1,
                source_type="zabbix",
                collect_type="zabbix_hosts",
                policy_name="hosts",
            ),
            SimpleNamespace(
                id=2,
                source_type="zabbix",
                collect_type="zabbix_metrics",
                policy_name="metrics",
            ),
        ]
        self.source = SimpleNamespace(id=3, source_name="offline-zabbix")

    @patch.object(observability_scheduler, "_is_due", return_value=True)
    @patch.object(observability_scheduler, "run_collect_task")
    def test_transport_failure_skips_remaining_policies_for_source(self, run_task, _is_due):
        run_task.return_value = {
            "task_status": "failed",
            "error_message": "<urlopen error timed out>",
        }
        db = FakeSession(self.policies, [self.source])

        with patch.object(observability_scheduler, "SessionLocal", return_value=db):
            observability_scheduler.run_due_observability_jobs()

        self.assertEqual(run_task.call_count, 1)
        self.assertTrue(db.closed)

    @patch.object(observability_scheduler, "_is_due", return_value=True)
    @patch.object(observability_scheduler, "run_collect_task")
    def test_metric_specific_failure_does_not_trip_source_breaker(self, run_task, _is_due):
        run_task.return_value = {
            "task_status": "failed",
            "error_message": "unsupported item key",
        }
        db = FakeSession(self.policies, [self.source])

        with patch.object(observability_scheduler, "SessionLocal", return_value=db):
            observability_scheduler.run_due_observability_jobs()

        self.assertEqual(run_task.call_count, 2)
        self.assertTrue(db.closed)

    def test_transport_failure_classifier_is_specific_to_network_errors(self):
        self.assertTrue(observability_scheduler._is_transport_failure("Connection refused"))
        self.assertTrue(observability_scheduler._is_transport_failure("<urlopen error timed out>"))
        self.assertFalse(observability_scheduler._is_transport_failure("unsupported item key"))


if __name__ == "__main__":
    unittest.main()
