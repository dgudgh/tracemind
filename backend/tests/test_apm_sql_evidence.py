import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class ApmSqlEvidenceTest(unittest.TestCase):
    def test_trace_identifier_is_encoded_as_one_url_path_segment(self):
        from app.services.observability.apm import fetch_trace_payload

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": []}'
        source = SimpleNamespace(
            source_type="jaeger",
            api_url="https://jaeger.example.test",
            token_encrypted=None,
            timeout_seconds=10,
        )

        with patch("app.services.observability.apm.urllib.request.urlopen", return_value=response) as urlopen:
            fetch_trace_payload(source, "trace/../admin?raw=1")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://jaeger.example.test/api/traces/trace%2F..%2Fadmin%3Fraw%3D1",
        )

    def test_normalizes_skywalking_jaeger_zipkin_and_otel_without_credentials(self):
        from app.services.observability.apm_adapters import normalize_trace

        skywalking = normalize_trace(
            "skywalking",
            {"data": {"trace": {"traceId": "sky-1", "spans": [{"spanId": 1, "parentSpanId": -1, "serviceCode": "orders", "endpointName": "/checkout", "startTime": 1784541600000, "endTime": 1784541600100, "isError": False, "tags": [{"key": "db.statement", "value": "SELECT * FROM orders WHERE id = 42"}, {"key": "db.password", "value": "secret"}] }]}}},
        )
        jaeger = normalize_trace(
            "jaeger",
            {"data": [{"traceID": "jaeger-1", "spans": [{"spanID": "s1", "operationName": "SELECT", "startTime": 1784541600000000, "duration": 1000, "process": {"serviceName": "payments"}, "tags": [{"key": "peer.address", "value": "10.0.0.8"}, {"key": "db.statement", "value": "SELECT 1"}]}]}]},
        )
        zipkin = normalize_trace(
            "zipkin",
            [{"traceId": "zip-1", "id": "z1", "name": "GET /orders", "timestamp": 1784541600000000, "duration": 3000, "localEndpoint": {"serviceName": "orders"}, "tags": {"http.status_code": "200"}}],
        )
        otel = normalize_trace(
            "otel",
            {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "inventory"}}]}, "scopeSpans": [{"spans": [{"traceId": "otel-1", "spanId": "o1", "name": "inventory.query", "startTimeUnixNano": "1784541600000000000", "endTimeUnixNano": "1784541600002000000", "attributes": [{"key": "db.statement", "value": {"stringValue": "SELECT * FROM stock"}}]}]}]}]},
        )

        self.assertEqual(skywalking["trace_id"], "sky-1")
        self.assertEqual(skywalking["spans"][0]["duration_ms"], 100)
        self.assertNotIn("db.password", skywalking["spans"][0]["tags"])
        self.assertEqual(jaeger["spans"][0]["service"], "payments")
        self.assertEqual(zipkin["spans"][0]["endpoint"], "GET /orders")
        self.assertEqual(otel["spans"][0]["service"], "inventory")

    def test_malformed_spans_are_skipped_and_external_fields_are_bounded(self):
        from app.services.observability.apm_adapters import MAX_TAG_VALUE_LENGTH, normalize_trace

        trace = normalize_trace(
            "jaeger",
            {"data": [{"traceID": "trace", "spans": [
                {"spanID": "bad", "startTime": "invalid", "duration": 1000, "tags": []},
                {"spanID": "good", "startTime": 1784541600000000, "duration": 1000, "tags": [{"key": "db.statement", "value": "x" * (MAX_TAG_VALUE_LENGTH + 100)}]},
            ]}]},
        )

        self.assertEqual([span["span_id"] for span in trace["spans"]], ["good"])
        self.assertEqual(len(trace["spans"][0]["tags"]["db.statement"]), MAX_TAG_VALUE_LENGTH)

    def test_sql_linking_uses_fingerprint_and_does_not_fabricate_low_confidence_match(self):
        from app.services.observability.apm import link_sql

        trace = {
            "trace_id": "trace-1",
            "spans": [
                {"span_id": "matched", "event_time": "2026-07-20T01:00:00+00:00", "tags": {"db.statement": "SELECT * FROM orders WHERE id = 42 AND customer = 'alice'", "peer.address": "10.0.0.8", "peer.port": "3306"}},
                {"span_id": "unmatched", "event_time": "2026-07-20T01:01:00+00:00", "tags": {"db.statement": "SELECT * FROM orders", "peer.address": "10.0.0.99"}},
            ],
        }
        evidence = link_sql(trace, [{"id": 8, "host": "10.0.0.8", "port": 3306}, {"id": 9, "host": "10.0.0.9", "port": 3306}])

        self.assertEqual(evidence[0]["instance_id"], 8)
        self.assertGreaterEqual(evidence[0]["match_confidence"], 60)
        self.assertEqual(evidence[0]["sql_fingerprint"], "SELECT * FROM orders WHERE id = ? AND customer = ?")
        self.assertIsNone(evidence[1]["instance_id"])
        self.assertLess(evidence[1]["match_confidence"], 60)
        self.assertTrue(evidence[1]["unmatched_reason"])

    def test_build_apm_evidence_keeps_trace_and_span_identifiers(self):
        from app.services.observability.apm import build_apm_evidence

        evidence = build_apm_evidence(
            8,
            ("2026-07-20T00:00:00Z", "2026-07-20T02:00:00Z"),
            [{"trace_id": "trace-1", "spans": [{"span_id": "span-1", "event_time": "2026-07-20T01:00:00Z", "tags": {"db.statement": "SELECT 1", "peer.address": "db"}}]}],
            [{"id": 8, "host": "db", "port": 3306}],
        )

        self.assertEqual(evidence[0]["source"], "apm")
        self.assertEqual(evidence[0]["trace_id"], "trace-1")
        self.assertEqual(evidence[0]["span_id"], "span-1")
        self.assertEqual(evidence[0]["instance_id"], 8)
