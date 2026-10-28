"""Interactive inspections must use the same control plane as scheduled ones (ISS-21).

The scheduled path was moved onto the durable queue while the manual API, the per-engine pages
and the report centre kept calling `runner.run_unified_inspection` directly. Nothing looked
broken - both paths returned a report - so the half-wiring survived a full review round.

Two properties guard it now:
1. An interactive run really produces a job row (leases, cancel, retries apply to it).
2. No production module calls the direct runner any more - enforced by walking the AST, not by
   grepping, so a commented-out line or a name in a docstring cannot satisfy it.
"""
import ast
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.db_instance import DBInstance
from app.models.inspection_job import DBInspectionJob
from app.services.inspection import dispatch, jobs

BACKEND = Path(__file__).resolve().parents[1] / "app"
# Both direct entry points bypass the queue: `run_unified_inspection` returns a report,
# `run_inspection` returns rows. Implicit callers (diagnosis, rule engine, operation AI,
# legacy engine pages) used the latter, so guarding only the former left them uncovered.
DIRECT_ENTRYPOINTS = {"run_unified_inspection", "run_inspection"}


def _direct_runner_calls() -> list[str]:
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("services/inspection/runner.py"):
            continue  # the definition itself
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in DIRECT_ENTRYPOINTS:
                    offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in DIRECT_ENTRYPOINTS:
                    offenders.append(f"{relative}:{node.lineno}")
    return offenders


class ManualInspectionDispatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _instance(self) -> DBInstance:
        return DBInstance(
            instance_name="manual-target",
            db_type="mysql",
            host="127.0.0.1",
            port=3306,
            username="monitor",
            password_cipher="env://TEST_DB_PASSWORD",
            enabled=1,
        )

    def test_manual_run_leaves_a_job_row_instead_of_bypassing_the_queue(self):
        with self.Session() as db:
            db.add(self._instance())
            db.commit()
            instance = db.query(DBInstance).first()
            payload = dispatch.run_through_queue(db, instance, trigger_type="manual")

        self.assertTrue(payload["job"]["id"])
        self.assertEqual(payload["job"]["published"], payload["job"]["status"] in {"success", "partial"})
        with self.Session() as db:
            row = db.get(DBInspectionJob, payload["job"]["id"])
            self.assertIsNotNone(row)
            self.assertEqual(row.trigger_type, "manual")
            self.assertIn(row.status, jobs.TERMINAL)
            # The report is addressed by the job id, so the queue is the record of the run.
            self.assertEqual(payload["task"]["task_id"], row.id)

    def test_cancelled_submission_is_refused_instead_of_running_anyway(self):
        with self.Session() as db:
            db.add(self._instance())
            db.commit()
            instance = db.query(DBInstance).first()
            policy = jobs.default_policy_for(db, instance, instance.db_type)
            self.assertIsNotNone(policy)
            row = jobs.enqueue(db, instance, instance.db_type, policy,
                               idempotency_key="cancel-check", trigger_type="manual")
            jobs.cancel(db, row)
            with self.assertRaises(jobs.JobError):
                jobs.submit(db, instance, policy, idempotency_key="cancel-check")

    def test_implicit_run_is_recorded_as_a_job_too(self):
        with self.Session() as db:
            db.add(self._instance())
            db.commit()
            instance = db.query(DBInstance).first()
            task_id, _rows = dispatch.run_collecting_rows(db, instance, trigger_type="diagnosis")

        with self.Session() as db:
            row = db.get(DBInspectionJob, task_id)
            self.assertIsNotNone(row, "隐式巡检没有留下任务记录")
            self.assertEqual(row.trigger_type, "diagnosis")
            self.assertIn(row.status, jobs.TERMINAL)

    def test_no_production_module_calls_the_direct_runner(self):
        offenders = _direct_runner_calls()
        self.assertEqual(
            offenders, [],
            f"这些位置绕过了巡检队列（ISS-21 半吊子接线）: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
