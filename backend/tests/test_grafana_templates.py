import unittest


class GrafanaTemplatesTest(unittest.TestCase):
    def test_every_template_builds_a_safe_prometheus_dashboard(self):
        from app.services.observability.grafana import TEMPLATE_KEYS, build_dashboard

        self.assertEqual(
            set(TEMPLATE_KEYS),
            {"overview", "oracle_tablespace", "mysql_replication", "archive_jobs", "zabbix_push_failures"},
        )
        for key in TEMPLATE_KEYS:
            with self.subTest(key=key):
                dashboard = build_dashboard(key, "prom-main", "30s")
                self.assertTrue(dashboard["uid"])
                self.assertTrue(dashboard["title"])
                self.assertGreater(dashboard["schemaVersion"], 0)
                self.assertTrue(dashboard["panels"])
                self.assertEqual(dashboard["refresh"], "30s")
                self.assertTrue(all(panel["datasource"]["uid"] == "prom-main" for panel in dashboard["panels"]))
                rendered = str(dashboard).lower()
                self.assertNotIn("password", rendered)
                self.assertNotIn("token", rendered)
                self.assertNotIn("select ", rendered)

    def test_refresh_is_bounded_and_template_metadata_is_export_safe(self):
        from app.services.observability.grafana import build_dashboard, list_templates

        with self.assertRaisesRegex(ValueError, "refresh"):
            build_dashboard("overview", "prom-main", "1ms")
        with self.assertRaisesRegex(ValueError, "template"):
            build_dashboard("missing", "prom-main", "30s")

        templates = list_templates()
        self.assertEqual({item["key"] for item in templates}, {"overview", "oracle_tablespace", "mysql_replication", "archive_jobs", "zabbix_push_failures"})
        self.assertTrue(all(item["required_metrics"] for item in templates))
        self.assertTrue(all(item["version"] == 1 for item in templates))
