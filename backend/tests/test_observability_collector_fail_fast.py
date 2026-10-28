from __future__ import annotations

import urllib.error
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.observability import prometheus_standard_service, zabbix_collector


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, item):
        self.added.append(item)

    def commit(self):
        self.commits += 1


class ObservabilityCollectorFailFastTest(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(
            id=4,
            api_url="http://monitoring.invalid",
            timeout_seconds=15,
        )

    @patch.object(prometheus_standard_service, "_query")
    def test_prometheus_stops_after_first_transport_failure(self, query):
        query.side_effect = urllib.error.URLError("timed out")

        result = prometheus_standard_service.sync_standard_metrics(
            FakeSession(),
            self.source,
        )

        self.assertEqual(query.call_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            len(result["failed"]),
            len(prometheus_standard_service.PROMETHEUS_STANDARD_QUERIES),
        )

    @patch.object(zabbix_collector, "api_call")
    def test_zabbix_stops_after_first_transport_failure(self, api_call):
        api_call.side_effect = urllib.error.URLError("timed out")

        with self.assertRaises(urllib.error.URLError):
            zabbix_collector._standard_items(self.source)

        self.assertEqual(api_call.call_count, 1)

    @patch.object(zabbix_collector, "api_call")
    def test_zabbix_continues_after_metric_specific_failure(self, api_call):
        api_call.side_effect = RuntimeError("unsupported item key")

        self.assertEqual(zabbix_collector._standard_items(self.source), [])
        self.assertEqual(api_call.call_count, len(zabbix_collector.ZABBIX_STANDARD_KEY_SEARCHES))


if __name__ == "__main__":
    unittest.main()
