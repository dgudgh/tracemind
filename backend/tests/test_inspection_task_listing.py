from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.models.check_result import DBCheckResult
from app.models.db_instance import DBInstance
from app.services.inspection.result_writer import list_tasks


class InspectionTaskListingTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        DBInstance.__table__.create(self.engine)
        DBCheckResult.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        instance = DBInstance(
            instance_name="mysql-primary",
            db_type="mysql",
            host="127.0.0.1",
            port=3306,
            username="monitor",
            password_cipher="encrypted",
            enabled=1,
        )
        self.session.add(instance)
        self.session.commit()
        self.instance_id = instance.id

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _result(self, task_id: str) -> DBCheckResult:
        return DBCheckResult(
            task_id=task_id,
            instance_id=self.instance_id,
            db_type="mysql",
            check_category="overview",
            check_item="status",
            check_status="normal",
            metric_name="status",
            metric_value="1",
            raw_data={},
        )

    def test_large_latest_task_does_not_hide_the_previous_task(self):
        self.session.add(self._result("older-task"))
        self.session.add_all(self._result("latest-task") for _ in range(401))
        self.session.commit()

        tasks = list_tasks(self.session, limit=2)

        self.assertEqual([item["task_id"] for item in tasks], ["latest-task", "older-task"])
        self.assertEqual(tasks[0]["total_count"], 401)
        self.assertEqual(tasks[1]["total_count"], 1)

    def test_collector_failure_is_not_reported_as_a_successful_health_check(self):
        row = self._result("failed-task")
        row.check_item = "Collector degraded"
        row.check_status = "warning"
        row.metric_name = "collector_status"
        row.finding = "cannot connect to database"
        self.session.add(row)
        self.session.commit()

        task = list_tasks(self.session, limit=1)[0]

        self.assertEqual(task["task_status"], "failed")
        self.assertEqual(task["failure_reason"], "cannot connect to database")
        self.assertIsNone(task["overall_score"])

    def test_collector_failure_after_valid_checks_is_partial(self):
        self.session.add(self._result("partial-task"))
        row = self._result("partial-task")
        row.check_item = "Replication collector"
        row.check_status = "warning"
        row.metric_name = "collector_status"
        row.finding = "replication query failed"
        self.session.add(row)
        self.session.commit()

        task = list_tasks(self.session, limit=1)[0]

        self.assertEqual(task["task_status"], "partial")
        self.assertEqual(task["failure_reason"], "replication query failed")
        self.assertIsNone(task["overall_score"])

    def test_collector_error_status_is_reported_as_failed(self):
        row = self._result("connection-error-task")
        row.check_item = "Database connection"
        row.check_status = "error"
        row.metric_name = "collector_status"
        row.metric_value = "connection_failed"
        row.finding = "password authentication failed"
        row.raw_data = {"error": "password authentication failed"}
        self.session.add(row)
        self.session.commit()

        task = list_tasks(self.session, limit=1)[0]

        self.assertEqual(task["task_status"], "failed")
        self.assertEqual(task["failure_reason"], "password authentication failed")
        self.assertIsNone(task["overall_score"])
        self.assertEqual(task["risk_level"], "warning")

    def test_scoped_lists_exclude_foreign_and_mixed_task_summaries(self):
        from app.api.v1 import inspection

        self.session.add(self._result("allowed-task"))
        for task_id in ("foreign-task", "mixed-task"):
            row = self._result(task_id)
            row.instance_id = self.instance_id + 100
            self.session.add(row)
        self.session.add(self._result("mixed-task"))
        self.session.commit()
        request = Request({"type": "http", "headers": []})
        request.state.auth = {"user_id": 7, "role": "user", "source": "db"}
        scope = SimpleNamespace(restricted=True, native_instance_ids={self.instance_id})
        with patch.object(inspection, "resolve_scope", return_value=scope):
            tasks = inspection.api_list_tasks(request, db=self.session, db_type=None)
            results = inspection.api_list_results(request, db=self.session, status=None, db_type=None)
        self.assertEqual([row["task_id"] for row in tasks["items"]], ["allowed-task"])
        self.assertEqual({row["instance_id"] for row in results["items"]}, {self.instance_id})
        self.assertEqual(results["total"], 2)
        self.assertEqual(list_tasks(self.session, allowed_instance_ids=set()), [])


if __name__ == "__main__":
    unittest.main()
