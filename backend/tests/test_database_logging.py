from __future__ import annotations

import unittest

from app.core.config import Settings
from app.core.database_options import (
    database_engine_options,
    database_target_summary,
    redact_database_error,
)


class DatabaseLoggingConfigTest(unittest.TestCase):
    def test_sql_echo_is_disabled_independently_from_debug_mode(self):
        settings = Settings(DEBUG=True, _env_file=None)

        self.assertTrue(settings.DEBUG)
        self.assertFalse(settings.SQL_ECHO)

    def test_sqlite_engine_options_keep_thread_and_lock_safety(self):
        options = database_engine_options("sqlite:////tmp/tracemind-test.db", sql_echo=False)

        self.assertEqual(options["connect_args"], {"check_same_thread": False, "timeout": 30})
        self.assertNotIn("pool_timeout", options)

    def test_mysql_engine_options_bound_connect_and_io_waits(self):
        options = database_engine_options(
            "mysql+pymysql://app:secret@db.example:3306/tracemind",  # secret-scan: allow -- engine option fixture
            sql_echo=False,
        )

        self.assertEqual(options["connect_args"]["connect_timeout"], 8)
        self.assertEqual(options["connect_args"]["read_timeout"], 15)
        self.assertEqual(options["connect_args"]["write_timeout"], 15)
        self.assertEqual(options["pool_timeout"], 10)
        self.assertTrue(options["pool_pre_ping"])

    def test_database_target_and_error_output_do_not_expose_credentials(self):
        url = "mysql+pymysql://platform-user:top-secret@db.example:3306/tracemind"  # secret-scan: allow -- redaction fixture
        target = database_target_summary(url)
        error = redact_database_error(f"failed for {url}; password=top-secret")

        self.assertEqual(target["driver"], "mysql+pymysql")
        self.assertEqual(target["host"], "db.example")
        self.assertEqual(target["port"], 3306)
        self.assertEqual(target["database"], "tracemind")
        self.assertNotIn("platform-user", str(target))
        self.assertNotIn("top-secret", str(target))
        self.assertNotIn("top-secret", error)
        self.assertIn("***:***@db.example", error)


if __name__ == "__main__":
    unittest.main()
