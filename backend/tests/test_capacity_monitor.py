import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.alert_event import DBAlertEvent
from app.models.capacity_policy import DBCapacityAlert, DBCapacityPolicy
from app.models.db_instance import DBInstance
from app.models.object_size import DBObjectSizeDaily
from app.services import capacity_monitor


class CapacityMonitorTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _instance(self, db_type="postgresql", name="db-1"):
        row = DBInstance(
            instance_name=name,
            db_type=db_type,
            host="10.0.0.10",
            port=5432 if db_type == "postgresql" else 1433,
            username="monitor",
            password_cipher="encrypted",
            enabled=1,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_postgresql_and_sqlserver_are_supported_without_fake_usage_percent(self):
        pg = self._instance("postgresql", "pg-1")
        with patch.object(capacity_monitor, "_postgresql_capacity_rows", return_value=[{
            "object_name": "mes",
            "object_type": "database",
            "total_mb": 2048,
            "used_mb": 2048,
            "free_mb": None,
            "used_pct": None,
            "capacity_limit_known": False,
        }]):
            result = capacity_monitor.collect_current_capacity(pg)
        self.assertEqual(result["collection_status"], "success")
        self.assertFalse(result["capacity_limit_known"])
        self.assertIsNone(result["used_pct"])
        self.assertIsNone(result["free_mb"])

        sqlserver = self._instance("sqlserver", "mssql-1")
        with patch.object(capacity_monitor, "_sqlserver_capacity_rows", return_value=[{
            "object_name": "mes",
            "object_type": "database",
            "total_mb": 4096,
            "used_mb": 3072,
            "free_mb": 1024,
            "used_pct": 75,
            "capacity_limit_known": True,
        }]):
            result = capacity_monitor.collect_current_capacity(sqlserver)
        self.assertEqual(result["used_pct"], 75)
        self.assertEqual(result["free_mb"], 1024)

    def test_empty_result_is_no_data_and_never_reported_as_zero_percent(self):
        instance = self._instance()
        with patch.object(capacity_monitor, "_postgresql_capacity_rows", return_value=[]):
            result = capacity_monitor.collect_current_capacity(instance)
        self.assertEqual(result["collection_status"], "no_data")
        self.assertIsNone(result["used_pct"])
        self.assertIsNone(result["total_mb"])

    def test_daily_snapshot_is_upserted_and_logical_capacity_has_no_false_forecast(self):
        instance = self._instance()
        current = {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "total_mb": 100,
            "used_mb": 100,
            "free_mb": None,
            "used_pct": None,
            "collection_status": "success",
            "capacity_limit_known": False,
            "objects": [{
                "object_name": "mes",
                "object_type": "database",
                "total_mb": 100,
                "used_mb": 100,
                "capacity_limit_known": False,
            }],
        }
        with patch.object(capacity_monitor, "collect_current_capacity", return_value=current):
            first = capacity_monitor.collect_snapshot(self.db, instance)
            current["objects"][0]["total_mb"] = 120
            current["objects"][0]["used_mb"] = 120
            second = capacity_monitor.collect_snapshot(self.db, instance)

        self.assertEqual(self.db.query(DBObjectSizeDaily).count(), 1)
        snapshot = self.db.query(DBObjectSizeDaily).one()
        self.assertEqual(float(snapshot.total_size_mb), 120)
        self.assertEqual(snapshot.capacity_limit_known, 0)
        self.assertEqual(first["policy"]["data_freshness_status"], "fresh")
        self.assertEqual(second["analysis"]["items"][0]["capacity_limit_known"], False)
        self.assertIsNone(second["analysis"]["items"][0]["forecast_days"])

    def test_no_data_and_connection_failures_create_deduplicated_events_then_recover(self):
        instance = self._instance()
        no_data = {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "total_mb": None,
            "used_mb": None,
            "free_mb": None,
            "used_pct": None,
            "objects": [],
            "warnings": ["empty"],
            "collection_status": "no_data",
            "capacity_limit_known": False,
        }
        with patch.object(capacity_monitor, "collect_current_capacity", return_value=no_data):
            capacity_monitor.collect_snapshot(self.db, instance)
            capacity_monitor.collect_snapshot(self.db, instance)
        missing = self.db.query(DBCapacityAlert).filter_by(alert_type="data_missing").one()
        self.assertEqual(missing.status, "open")
        self.assertEqual(missing.occurrence_count, 2)
        self.assertEqual(self.db.query(DBAlertEvent).count(), 1)

        with patch.object(capacity_monitor, "collect_current_capacity", side_effect=RuntimeError("connection refused")):
            with self.assertRaisesRegex(RuntimeError, "connection refused"):
                capacity_monitor.collect_snapshot(self.db, instance)
        failed = self.db.query(DBCapacityAlert).filter_by(alert_type="collection_failed").one()
        policy = self.db.query(DBCapacityPolicy).filter_by(instance_id=instance.id).one()
        self.assertEqual(failed.level, "critical")
        self.assertEqual(policy.last_status, "error")
        self.assertEqual(policy.data_freshness_status, "error")

        success = {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "total_mb": 10,
            "used_mb": 10,
            "free_mb": None,
            "used_pct": None,
            "collection_status": "success",
            "capacity_limit_known": False,
            "objects": [{
                "object_name": "mes",
                "object_type": "database",
                "total_mb": 10,
                "used_mb": 10,
                "capacity_limit_known": False,
            }],
        }
        with patch.object(capacity_monitor, "collect_current_capacity", return_value=success):
            capacity_monitor.collect_snapshot(self.db, instance)
        self.assertEqual(missing.status, "closed")
        self.assertEqual(failed.status, "closed")
        self.assertTrue(all(event.status == "resolved" for event in self.db.query(DBAlertEvent).all()))

    def test_freshness_evaluation_creates_and_recovers_stale_alert(self):
        instance = self._instance()
        policy = capacity_monitor.get_or_create_policy(self.db, instance.id)
        now = datetime(2026, 8, 23, 12, 0, 0)
        policy.frequency_minutes = 30
        policy.last_run_time = now - timedelta(hours=3)
        policy.last_success_time = now - timedelta(hours=3)
        self.db.commit()

        result = capacity_monitor.evaluate_data_freshness(self.db, now)
        self.assertEqual(result["stale"], 1)
        stale = self.db.query(DBCapacityAlert).filter_by(alert_type="data_stale").one()
        self.assertEqual(stale.status, "open")

        policy.last_success_time = now
        self.db.commit()
        result = capacity_monitor.evaluate_data_freshness(self.db, now + timedelta(minutes=10))
        self.assertEqual(result["fresh"], 1)
        self.assertEqual(stale.status, "closed")

    def _selects_while_collecting(self, object_count: int, repeats: int = 1) -> int:
        """Measure collection against a fresh instance, optionally repeating keys."""
        instance = self._instance(name=f"db-{object_count}")
        current = {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "collection_status": "success",
            "capacity_limit_known": True,
            "objects": [
                {
                    "object_name": f"obj_{index}",
                    "object_type": "tablespace",
                    "total_mb": 100 + index,
                    "used_mb": 50 + index,
                    "index_mb": 1,
                    "capacity_limit_known": True,
                }
                for index in range(object_count)
            ],
        }
        current["objects"] *= repeats
        statements: list[str] = []

        def _record(_conn, _cursor, statement, _params, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", _record)
        try:
            with patch.object(capacity_monitor, "collect_current_capacity", return_value=current):
                capacity_monitor.collect_snapshot(self.db, instance)
        finally:
            event.remove(self.engine, "before_cursor_execute", _record)
        return len(statements)

    def test_snapshot_collection_uses_one_database_match_per_distinct_key(self):
        """No unique constraint exists: resolve each key with DB comparison first."""
        small = self._selects_while_collecting(8)
        large = self._selects_while_collecting(64)
        self.assertEqual(large - small, 56)

    def test_snapshot_collection_caches_repeated_exact_keys_within_batch(self):
        small = self._selects_while_collecting(8)
        repeated = self._selects_while_collecting(8, repeats=8)
        self.assertEqual(small, repeated)

    def test_repeated_object_rows_update_a_single_snapshot_row(self):
        """A batch listing the same object twice must not insert a duplicate.

        The old per-object SELECT found the freshly-added pending row through autoflush;
        the prefetched index has to reproduce that, otherwise one collection would create
        two rows for the same object and corrupt the day's history.
        """
        instance = self._instance()
        current = {
            "instance_id": instance.id,
            "db_type": instance.db_type,
            "collection_status": "success",
            "capacity_limit_known": True,
            "objects": [
                {"object_name": "MES_DATA", "object_type": "tablespace",
                 "total_mb": 100, "used_mb": 40, "index_mb": 1, "capacity_limit_known": True},
                {"object_name": "MES_DATA", "object_type": "tablespace",
                 "total_mb": 200, "used_mb": 90, "index_mb": 2, "capacity_limit_known": True},
            ],
        }
        with patch.object(capacity_monitor, "collect_current_capacity", return_value=current):
            capacity_monitor.collect_snapshot(self.db, instance)

        rows = self.db.query(DBObjectSizeDaily).all()
        self.assertEqual(len(rows), 1)
        # Last write for the key wins, in batch order.
        self.assertEqual(float(rows[0].total_size_mb), 200)
        self.assertEqual(float(rows[0].data_size_mb), 90)


class CapacityPolicyListingTest(unittest.TestCase):
    """`app.tasks.capacity_scheduler.get_all_policies_status` backs the scheduler listing.

    It resolved each policy's instance with its own SELECT, so the endpoint cost grew with
    the number of policies while returning the same rows. Same batched-lookup shape as
    ``capacity_monitor.evaluate_data_freshness``.
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _policies(self, count: int) -> None:
        # Reset first: this class measures the same listing at two sizes against one engine,
        # so a second call must not see the first call's rows.
        self.db.query(DBCapacityPolicy).delete()
        self.db.query(DBInstance).delete()
        self.db.commit()
        for index in range(count):
            instance = DBInstance(
                instance_name=f"db-{index}",
                db_type="oracle",
                host=f"10.0.0.{index}",
                port=1521,
                username="monitor",
                password_cipher="encrypted",
                enabled=1,
            )
            self.db.add(instance)
            self.db.flush()
            self.db.add(DBCapacityPolicy(instance_id=instance.id, enabled=1))
        self.db.commit()

    def _selects_for_listing(self, policy_count: int) -> tuple[int, list[dict]]:
        self._policies(policy_count)
        statements: list[str] = []

        def _record(_conn, _cursor, statement, _params, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", _record)
        try:
            from app.tasks.capacity_scheduler import get_all_policies_status

            rows = get_all_policies_status(self.db)
        finally:
            event.remove(self.engine, "before_cursor_execute", _record)
        return len(statements), rows

    def test_policy_listing_does_not_issue_one_select_per_policy(self):
        small, small_rows = self._selects_for_listing(3)
        large, large_rows = self._selects_for_listing(30)
        self.assertEqual(
            small,
            large,
            f"SELECT count grew with policy count ({small} for 3 policies, {large} for 30)",
        )
        self.assertEqual(len(small_rows), 3)
        self.assertEqual(len(large_rows), 30)

    def test_policy_listing_still_resolves_instance_name_and_host(self):
        _count, rows = self._selects_for_listing(2)
        by_id = {row["instance_name"] for row in rows}
        self.assertEqual(by_id, {"db-0", "db-1"})
        for row in rows:
            self.assertTrue(row["host"].startswith("10.0.0."))

    def test_policy_pointing_at_a_missing_instance_still_reads_as_unknown(self):
        """The batched lookup must degrade the same way the per-row one did."""
        self._policies(1)
        orphan = DBCapacityPolicy(instance_id=999999, enabled=1)
        self.db.add(orphan)
        self.db.commit()

        from app.tasks.capacity_scheduler import get_all_policies_status

        rows = {row["instance_id"]: row for row in get_all_policies_status(self.db)}
        self.assertEqual(rows[999999]["instance_name"], "未知")
        self.assertEqual(rows[999999]["host"], "")


if __name__ == "__main__":
    unittest.main()
