import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from app.core import database


class IntegrationRuntimeSchemaTest(unittest.TestCase):
    def test_existing_notification_and_itsm_tables_receive_idempotency_columns(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE db_instance (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE notify_event (id INTEGER PRIMARY KEY)"))
            conn.execute(text(
                "CREATE TABLE notify_log (id INTEGER PRIMARY KEY, retry_count INTEGER DEFAULT 0, "
                "next_retry_time DATETIME)"
            ))
            conn.execute(text("INSERT INTO notify_log (id, retry_count) VALUES (1, 2)"))
            conn.execute(text("CREATE TABLE itsm_ticket (id INTEGER PRIMARY KEY)"))

        with (
            patch.object(database, "engine", engine),
            patch.object(database, "settings", SimpleNamespace(is_sqlite=True)),
        ):
            database.ensure_runtime_schema(allow_upgrade=True)
            database.ensure_runtime_schema(allow_upgrade=False)

        inspector = inspect(engine)
        notify_event_columns = {column["name"] for column in inspector.get_columns("notify_event")}
        notify_log_columns = {column["name"] for column in inspector.get_columns("notify_log")}
        itsm_columns = {column["name"] for column in inspector.get_columns("itsm_ticket")}
        indexes = {index["name"] for index in inspector.get_indexes("notify_event")}

        self.assertIn("event_key", notify_event_columns)
        self.assertIn("attempt_count", notify_log_columns)
        self.assertIn("idempotency_key", itsm_columns)
        self.assertIn("uq_notify_event_event_key", indexes)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT attempt_count FROM notify_log WHERE id = 1")).scalar_one(), 3)


if __name__ == "__main__":
    unittest.main()
