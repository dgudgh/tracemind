import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.backup_operation import BackupOperation
from app.models.db_instance import DBInstance
from app.models.perf_metric import DBPerfMetric
from app.models.report_center import ReportArtifact, ReportScheduleRun, SchedulerLease
from app.services import report_center


class ReportCenterTest(unittest.TestCase):
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

    def _items(self):
        return [
            {
                "id": "inspection:100",
                "source_id": "100",
                "report_type": "inspection",
                "title": "Oracle 巡检报告",
                "instance_id": 7,
                "instance_name": "mesdb",
                "db_type": "oracle",
                "status": "generated",
                "risk_level": "warning",
                "score": 88,
                "summary": "发现 1 个需要关注的风险",
                "create_time": "2026-08-23 08:00:00",
            }
        ]

    def _schedule(self, **values):
        payload = {
            "name": "工作日报",
            "report_type": "all",
            "output_formats": ["markdown", "html", "json", "word"],
            "schedule_type": "workday",
            "run_time": "08:30",
            "weekdays": [1, 2, 3, 4, 5],
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once",
            "misfire_grace_seconds": 300,
            "retention_days": 365,
            "enabled": 1,
        }
        payload.update(values)
        return report_center.create_schedule(self.db, payload, actor="tester")

    def test_artifact_signature_covers_all_representations(self):
        artifact = report_center.create_snapshot_artifact(
            self.db,
            self._items(),
            source_key="manual:test-signature",
            title="统一快照",
            watermark="内部使用",
        )
        verification = report_center.verify_artifact(artifact)
        self.assertTrue(verification["valid"])
        self.assertEqual(set(verification["representation_hashes"]), {"markdown", "html", "json"})

        artifact.content_html += "<p>tampered</p>"
        self.db.commit()
        verification = report_center.verify_artifact(artifact)
        self.assertFalse(verification["valid"])
        self.assertTrue(verification["hash_valid"])
        self.assertFalse(verification["representations_valid"])

    def test_snapshot_normalizes_decimal_and_datetime_payloads(self):
        items = self._items()
        items[0]["score"] = Decimal("88.50")
        items[0]["create_time"] = datetime(2026, 8, 23, 8, 0, 0)
        artifact = report_center.create_snapshot_artifact(
            self.db,
            items,
            source_key="manual:json-safe",
            title="可序列化快照",
        )
        self.assertEqual(artifact.content_json["items"][0]["score"], "88.50")
        self.assertTrue(report_center.verify_artifact(artifact)["valid"])

    def test_multi_instance_snapshot_keeps_explicit_data_scope(self):
        items = self._items()
        second = dict(items[0], id="inspection:101", source_id="101", instance_id=8)
        artifact = report_center.create_snapshot_artifact(
            self.db,
            items + [second],
            source_key="manual:scope",
            title="多实例快照",
        )

        self.assertEqual(report_center.artifact_instance_scope(artifact), {7, 8})
        self.assertEqual(report_center.list_artifacts(self.db, allowed_instance_ids={7, 8}), [artifact])
        self.assertEqual(report_center.list_artifacts(self.db, allowed_instance_ids={7}), [])

    def test_source_sync_is_idempotent_and_resigns_updated_source(self):
        first = report_center.sync_source_artifacts(self.db, self._items(), actor="tester")[0]
        first_signature = first.signature
        updated_items = self._items()
        updated_items[0]["summary"] = "风险已复核"
        second = report_center.sync_source_artifacts(self.db, updated_items, actor="tester")[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ReportArtifact).count(), 1)
        self.assertNotEqual(first_signature, second.signature)
        self.assertTrue(report_center.verify_artifact(second)["valid"])

    def test_retention_archives_expired_artifacts_but_respects_legal_hold(self):
        now = datetime(2026, 8, 23, 10, 0, 0)
        expired = report_center.create_snapshot_artifact(
            self.db, self._items(), source_key="manual:expired", title="过期报告", generated_at=now - timedelta(days=10), retention_days=1
        )
        held = report_center.create_snapshot_artifact(
            self.db, self._items(), source_key="manual:held", title="保留报告", generated_at=now - timedelta(days=10), retention_days=1
        )
        report_center.set_legal_hold(self.db, held, enabled=True, actor="auditor", reason="调查中")

        count = report_center.apply_retention(self.db, now=now)

        self.assertEqual(count, 1)
        self.assertEqual(expired.archive_state, "archived")
        self.assertIsNotNone(expired.archive_uri)
        self.assertEqual(held.archive_state, "active")

    def test_workday_schedule_uses_timezone_and_skips_weekend(self):
        schedule = self._schedule()
        # Friday 2026-08-21 09:00 in Shanghai is 01:00 UTC; next run is Monday.
        next_run = report_center.calculate_next_run(schedule, after=datetime(2026, 8, 21, 1, 0, 0))
        self.assertEqual(next_run, datetime(2026, 8, 24, 0, 30, 0))

    def test_quarterly_schedule_clamps_day_to_month_end(self):
        schedule = self._schedule(
            schedule_type="quarterly",
            day_of_month=31,
            run_time="09:00",
        )
        next_run = report_center.calculate_next_run(schedule, after=datetime(2026, 3, 31, 2, 0, 0))
        # Q2 run is April 30 09:00 Shanghai, represented in UTC.
        self.assertEqual(next_run, datetime(2026, 4, 30, 1, 0, 0))

    def test_schedule_execution_is_idempotent_and_creates_signed_artifact(self):
        schedule = self._schedule()
        planned = datetime(2026, 8, 24, 0, 30, 0)
        loader = lambda db, report_type, limit: self._items()

        first = report_center.execute_schedule(
            self.db,
            schedule,
            planned_at=planned,
            trigger_type="scheduled",
            worker_id="node-a",
            source_loader=loader,
        )
        second = report_center.execute_schedule(
            self.db,
            schedule,
            planned_at=planned,
            trigger_type="scheduled",
            worker_id="node-b",
            source_loader=loader,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ReportScheduleRun).count(), 1)
        self.assertEqual(first.status, "success")
        artifact = self.db.query(ReportArtifact).filter(ReportArtifact.id == first.artifact_id).one()
        self.assertTrue(report_center.verify_artifact(artifact)["valid"])

    def test_misfire_skip_records_history_without_generating_artifact(self):
        current = datetime(2026, 8, 24, 3, 0, 0)
        schedule = self._schedule(misfire_policy="skip", misfire_grace_seconds=60)
        schedule.next_run_at = current - timedelta(hours=2)
        self.db.commit()

        runs = report_center.run_due_report_schedules(
            self.db,
            now=current,
            worker_id="node-a",
            source_loader=lambda db, report_type, limit: self._items(),
        )

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "skipped")
        self.assertIsNone(runs[0].artifact_id)
        self.assertGreater(schedule.next_run_at, current)

    def test_catch_up_honors_maximum_runs_in_one_dispatch(self):
        current = datetime(2026, 8, 24, 6, 0, 0)
        schedule = self._schedule(
            schedule_type="interval",
            interval_minutes=30,
            misfire_policy="catch_up",
            max_catchup_runs=3,
        )
        schedule.next_run_at = current - timedelta(hours=2)
        self.db.commit()

        runs = report_center.run_due_report_schedules(
            self.db,
            now=current,
            worker_id="node-a",
            source_loader=lambda db, report_type, limit: self._items(),
        )

        self.assertEqual(len(runs), 3)
        self.assertTrue(all(run.status == "success" for run in runs))
        self.assertEqual(self.db.query(ReportScheduleRun).count(), 3)
        self.assertEqual(schedule.next_run_at, current - timedelta(minutes=30))

    def test_unified_sources_include_performance_and_backup(self):
        from app.api.v1.report import _collect_unified_reports

        instance = DBInstance(
            instance_name="mesdb",
            db_type="oracle",
            host="127.0.0.1",
            port=1521,
            username="monitor",
            password_cipher="env://TEST_PASSWORD",
        )
        self.db.add(instance)
        self.db.commit()
        self.db.refresh(instance)
        self.db.add(DBPerfMetric(
            instance_id=instance.id,
            db_type="oracle",
            metric_name="cpu_util",
            value=Decimal("91.2"),
            capture_time=datetime(2026, 8, 23, 8, 0, 0),
        ))
        self.db.add(BackupOperation(
            operation_no="BKP-001",
            instance_id=instance.id,
            engine="oracle",
            category="backup",
            operation_type="full",
            status="success",
        ))
        self.db.commit()

        performance = _collect_unified_reports(self.db, "performance", 20)
        backups = _collect_unified_reports(self.db, "backup", 20)

        self.assertEqual(len(performance), 1)
        self.assertEqual(performance[0]["risk_level"], "warning")
        self.assertEqual(performance[0]["instance_name"], "mesdb")
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["source_id"], "operation-1")

    def test_static_report_center_routes_precede_legacy_report_id_route(self):
        from app.main import app

        paths = [getattr(route, "path", "") for route in app.routes]
        self.assertLess(
            paths.index("/api/v1/reports/catalog"),
            paths.index("/api/v1/reports/{report_id}"),
        )

    def test_scheduler_lease_is_exclusive_and_can_fail_over_after_expiry(self):
        start = datetime(2026, 8, 23, 10, 0, 0)
        self.assertTrue(report_center.claim_scheduler_lease(self.db, "master", "node-a", now=start, ttl_seconds=60))
        self.assertFalse(report_center.claim_scheduler_lease(self.db, "master", "node-b", now=start + timedelta(seconds=30), ttl_seconds=60))
        self.assertTrue(report_center.claim_scheduler_lease(self.db, "master", "node-b", now=start + timedelta(seconds=61), ttl_seconds=60))
        row = self.db.query(SchedulerLease).filter(SchedulerLease.lease_key == "master").one()
        self.assertEqual(row.owner_id, "node-b")

    def test_scheduler_fencing_rejects_stale_epoch(self):
        start = report_center.utcnow()
        first_epoch = report_center.claim_scheduler_lease_token(
            self.db, "master-fenced", "node-a", now=start, ttl_seconds=60
        )
        self.assertIsNotNone(first_epoch)
        self.assertTrue(report_center.validate_scheduler_lease(
            self.db, "master-fenced", "node-a", first_epoch, now=start
        ))

        next_epoch = report_center.claim_scheduler_lease_token(
            self.db, "master-fenced", "node-a", now=start + timedelta(seconds=1), ttl_seconds=60
        )
        self.assertGreater(next_epoch, first_epoch)
        self.assertFalse(report_center.validate_scheduler_lease(
            self.db, "master-fenced", "node-a", first_epoch, now=start + timedelta(seconds=1)
        ))
        self.assertFalse(report_center.heartbeat_scheduler_lease(
            self.db,
            "master-fenced",
            "node-a",
            expected_epoch=first_epoch,
            ttl_seconds=60,
        ))
        self.assertFalse(report_center.release_scheduler_lease(
            self.db, "master-fenced", "node-a", expected_epoch=first_epoch
        ))
        self.assertTrue(report_center.release_scheduler_lease(
            self.db, "master-fenced", "node-a", expected_epoch=next_epoch
        ))

    def test_dispatch_persists_fencing_epoch(self):
        row = report_center.start_dispatch_run(
            self.db, "master", "node-a", fencing_epoch=7
        )
        payload = report_center.serialize_dispatch(row)
        self.assertEqual(payload["fencing_epoch"], 7)


if __name__ == "__main__":
    unittest.main()
