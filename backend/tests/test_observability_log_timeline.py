import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException


class ObservabilityLogTimelineTest(unittest.TestCase):
    def test_disabled_log_source_cannot_be_queried(self):
        from app.api.v1.observability import query_observability_logs
        from app.schemas.observability import LogQueryRequest

        payload = LogQueryRequest(
            source_id=7,
            start_time="2026-07-20T00:00:00Z",
            end_time="2026-07-20T01:00:00Z",
        )
        source = SimpleNamespace(id=7, source_type="elasticsearch", enabled=0)
        with patch("app.api.v1.observability._get_source_or_404", return_value=source):
            with self.assertRaises(HTTPException) as raised:
                query_observability_logs(payload, db=object())

        self.assertEqual(raised.exception.status_code, 400)

    def test_adapters_normalize_elasticsearch_loki_and_graylog_events(self):
        from app.services.observability.log_adapters import normalize_log_events

        elastic = normalize_log_events(
            "elasticsearch",
            {"hits": {"hits": [{"_id": "es-1", "_source": {"@timestamp": "2026-07-20T10:00:00Z", "host": {"name": "db-1"}, "service": {"name": "orders"}, "log": {"level": "ERROR"}, "trace": {"id": "trace-es"}, "message": "database timeout"}}]}},
        )
        loki = normalize_log_events(
            "loki",
            {"data": {"result": [{"stream": {"host": "app-1", "app": "gateway", "level": "warn", "trace_id": "trace-loki"}, "values": [["1784541660000000000", "upstream slow"]]}]}},
        )
        graylog = normalize_log_events(
            "graylog",
            {"messages": [{"message": {"timestamp": "2026-07-20T10:02:00.000Z", "source": "api-1", "application_name": "billing", "level": 3, "trace_id": "trace-gray", "full_message": "payment retry"}}]},
        )

        self.assertEqual(elastic[0]["event_time"], "2026-07-20T10:00:00+00:00")
        self.assertEqual(elastic[0]["host"], "db-1")
        self.assertEqual(elastic[0]["severity"], "error")
        self.assertEqual(loki[0]["trace_id"], "trace-loki")
        self.assertEqual(graylog[0]["application"], "billing")
        self.assertEqual(graylog[0]["severity"], "error")
        self.assertEqual({item["raw_ref"] for item in elastic + loki + graylog}, {"es-1", "loki:0:0", "graylog:0"})

    def test_log_events_without_a_valid_timestamp_are_skipped(self):
        from app.services.observability.log_adapters import normalize_log_events

        events = normalize_log_events(
            "elasticsearch",
            {"hits": {"hits": [
                {"_id": "missing", "_source": {"message": "must not fabricate current time"}},
                {"_id": "invalid", "_source": {"@timestamp": "not-a-time", "message": "invalid"}},
                {"_id": "valid", "_source": {"@timestamp": "2026-07-20T10:00:00Z", "message": "valid"}},
            ]}},
        )

        self.assertEqual([item["raw_ref"] for item in events], ["valid"])

    def test_timeline_sorts_mixed_events_by_utc_time(self):
        from app.services.observability.log_query import build_fault_timeline

        events = [
            {"event_time": "2026-07-20T10:02:00+08:00", "source_type": "application_log", "message": "late"},
            {"event_time": "2026-07-20T01:00:00Z", "source_type": "alert", "message": "first"},
            {"event_time": "2026-07-20T01:30:00Z", "source_type": "apm", "message": "middle"},
        ]

        timeline = build_fault_timeline(7, "2026-07-20T00:00:00Z", "2026-07-20T03:00:00Z", events=events)

        self.assertEqual([item["message"] for item in timeline], ["first", "middle", "late"])
        self.assertEqual([item["source_label"] for item in timeline], ["Alert", "APM", "Application log"])

    def test_rejects_windows_over_twenty_four_hours_and_caps_results(self):
        from app.services.observability.log_adapters import normalize_log_events
        from app.services.observability.log_query import build_fault_timeline, validate_time_window

        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "24 hours"):
            validate_time_window(start, start + timedelta(hours=24, seconds=1))
        with self.assertRaisesRegex(ValueError, "24 hours"):
            build_fault_timeline(7, start, start + timedelta(hours=25), events=[])

        events = normalize_log_events(
            "graylog",
            {"messages": [{"message": {"timestamp": "2026-07-20T00:00:00Z", "message": "x" * 5000}} for _ in range(2001)]},
        )
        self.assertEqual(len(events), 2000)
        self.assertEqual(len(events[0]["message"]), 4000)
