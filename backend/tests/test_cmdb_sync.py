import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cmdb import service
from app.cmdb.api import router
from app.cmdb.models import (
    AssetCluster,
    AssetCorrectionAudit,
    AssetDiscoveryBatch,
    AssetEnvironment,
    AssetHost,
    AssetInstance,
    AssetRegion,
    AssetRelation,
    AssetSite,
    AssetZone,
    CmdbAsset,
    CmdbAssetOverride,
    CmdbBinding,
    CmdbChangeEvent,
    CmdbRelation,
    CmdbSource,
    CmdbSyncJob,
)
from app.cmdb.schemas import CmdbSyncRequest
from app.core.database import get_db


class CmdbSyncTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (
            CmdbSource, CmdbAsset, CmdbRelation, CmdbSyncJob, CmdbBinding, CmdbAssetOverride, CmdbChangeEvent,
            AssetRegion, AssetSite, AssetZone, AssetEnvironment, AssetHost, AssetCluster, AssetInstance,
            AssetRelation, AssetDiscoveryBatch, AssetCorrectionAudit,
        ):
            model.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.source = CmdbSource(source_name="primary", source_type="cmdb")
        self.db.add(self.source)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_sync_upserts_by_source_external_id(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
        )

        result = service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES Core"}],
        )

        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(
            self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="MES-01").one().name,
            "MES Core",
        )

    def test_sync_marks_missing_assets_pending_confirmation_without_deleting_them(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [
                {"external_id": "MES-01", "asset_type": "application", "name": "MES"},
                {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
            ],
        )

        result = service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
            authoritative_snapshot=True,
        )

        missing = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="DB-01").one()
        self.assertEqual(result.pending_confirmation_count, 1)
        self.assertEqual(missing.status, "pending_confirmation")
        self.assertIsNotNone(missing.id)

    def test_sync_request_defaults_to_non_authoritative(self):
        request = CmdbSyncRequest(source_id=self.source.id, items=[])

        self.assertFalse(request.authoritative_snapshot)

    def test_sync_missing_source_rolls_back_and_persists_failed_job(self):
        with patch.object(self.db, "rollback", wraps=self.db.rollback) as rollback:
            with self.assertRaisesRegex(ValueError, "source not found"):
                service.upsert_assets(
                    self.db,
                    999,
                    [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
                )

        self.assertGreaterEqual(rollback.call_count, 1)
        failed_job = self.db.query(CmdbSyncJob).one()
        self.assertEqual(failed_job.source_id, 999)
        self.assertEqual(failed_job.status, "failed")

    def test_non_authoritative_page_does_not_mark_missing_assets(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [
                {"external_id": "MES-01", "asset_type": "application", "name": "MES"},
                {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
            ],
        )

        result = service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
        )

        missing = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="DB-01").one()
        self.assertEqual(result.pending_confirmation_count, 0)
        self.assertEqual(missing.status, "active")

    def test_empty_authoritative_snapshot_does_not_mark_assets_missing(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
        )

        result = service.upsert_assets(
            self.db,
            self.source.id,
            [],
            authoritative_snapshot=True,
        )

        asset = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="MES-01").one()
        self.assertEqual(result.pending_confirmation_count, 0)
        self.assertEqual(asset.status, "active")

    def test_sync_retries_once_after_unique_conflict_and_commits_one_result(self):
        original_commit = self.db.commit
        commit_count = 0

        def commit_with_one_conflict():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 1:
                raise IntegrityError("INSERT cmdb_asset", {}, RuntimeError("unique conflict"))
            return original_commit()

        with (
            patch.object(self.db, "commit", side_effect=commit_with_one_conflict),
            patch.object(self.db, "rollback", wraps=self.db.rollback) as rollback,
        ):
            result = service.upsert_assets(
                self.db,
                self.source.id,
                [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
            )

        self.assertEqual(commit_count, 2)
        self.assertGreaterEqual(rollback.call_count, 1)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(self.db.query(CmdbAsset).filter_by(external_id="MES-01").count(), 1)
        self.assertEqual(self.db.query(CmdbSyncJob).filter_by(status="success").count(), 1)

    def test_sync_stops_retrying_unique_conflicts_and_persists_failed_job(self):
        original_commit = self.db.commit
        commit_count = 0

        def commit_with_repeated_conflicts():
            nonlocal commit_count
            commit_count += 1
            if commit_count <= 3:
                raise IntegrityError("INSERT cmdb_asset", {}, RuntimeError("unique conflict"))
            return original_commit()

        with patch.object(self.db, "commit", side_effect=commit_with_repeated_conflicts):
            with self.assertRaises(IntegrityError):
                service.upsert_assets(
                    self.db,
                    self.source.id,
                    [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
                )

        self.assertEqual(commit_count, 4)
        self.assertEqual(self.db.query(CmdbAsset).count(), 0)
        failed_job = self.db.query(CmdbSyncJob).one()
        self.assertEqual(failed_job.status, "failed")
        self.assertIn("unique conflict", failed_job.error_message)

    def test_sync_rolls_back_all_asset_changes_and_persists_failed_job(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
        )
        original_commit = self.db.commit
        commit_count = 0

        def commit_with_transaction_failure():
            nonlocal commit_count
            commit_count += 1
            if commit_count == 1:
                raise RuntimeError("database unavailable")
            return original_commit()

        with patch.object(self.db, "commit", side_effect=commit_with_transaction_failure):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                service.upsert_assets(
                    self.db,
                    self.source.id,
                    [
                        {"external_id": "MES-01", "asset_type": "application", "name": "Changed"},
                        {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
                    ],
                )

        assets = self.db.query(CmdbAsset).order_by(CmdbAsset.external_id).all()
        self.assertEqual([(asset.external_id, asset.name) for asset in assets], [("MES-01", "MES")])
        failed_job = self.db.query(CmdbSyncJob).filter_by(status="failed").one()
        self.assertIn("database unavailable", failed_job.error_message)

    def test_sync_masks_sensitive_values_before_persisting_raw_payload(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{
                "external_id": "MES-01",
                "asset_type": "application",
                "name": "MES",
                "password": "do-not-store",
                "nested": {"apiToken": "also-secret", "public": "visible"},
            }],
        )

        asset = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="MES-01").one()
        self.assertEqual(asset.raw_payload["password"], "***")
        self.assertEqual(asset.raw_payload["nested"]["apiToken"], "***")
        self.assertEqual(asset.raw_payload["nested"]["public"], "visible")

    def test_sync_masks_sensitive_values_in_exposed_asset_attributes(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{
                "external_id": "MES-01",
                "asset_type": "application",
                "name": "MES",
                "attributes": {"access_key": "do-not-return", "owner": "ops"},
            }],
        )

        asset = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="MES-01").one()
        self.assertEqual(asset.attributes["access_key"], "***")
        self.assertEqual(asset.attributes["owner"], "ops")

    def test_resolve_instance_context_returns_active_binding_and_asset(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "DB-01", "asset_type": "database", "name": "MES DB"}],
        )
        asset = self.db.query(CmdbAsset).filter_by(source_id=self.source.id, external_id="DB-01").one()
        self.db.add(CmdbBinding(instance_id=42, asset_id=asset.id, binding_status="active"))
        self.db.commit()

        context = service.resolve_instance_context(self.db, 42)

        self.assertEqual(context["instance_id"], 42)
        self.assertEqual(context["binding_status"], "active")
        self.assertEqual(context["asset"]["external_id"], "DB-01")

    def test_resolve_instance_context_prefers_an_active_binding(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [
                {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
                {"external_id": "DB-02", "asset_type": "database", "name": "Retired DB"},
            ],
        )
        assets = {
            asset.external_id: asset
            for asset in self.db.query(CmdbAsset).filter_by(source_id=self.source.id).all()
        }
        self.db.add_all([
            CmdbBinding(instance_id=42, asset_id=assets["DB-02"].id, binding_status="inactive", confidence=100),
            CmdbBinding(instance_id=42, asset_id=assets["DB-01"].id, binding_status="active", confidence=1),
        ])
        self.db.commit()

        context = service.resolve_instance_context(self.db, 42)

        self.assertEqual(context["binding_status"], "active")
        self.assertEqual(context["asset"]["external_id"], "DB-01")

    def test_relation_rejects_dangling_asset_endpoint(self):
        with self.assertRaisesRegex(ValueError, "asset not found"):
            service.upsert_relation(self.db, {
                "source_id": self.source.id,
                "source_asset_id": 999,
                "target_asset_id": 1000,
                "relation_type": "depends_on",
            })

        self.assertEqual(self.db.query(CmdbRelation).count(), 0)

    def test_relation_rejects_missing_source(self):
        with self.assertRaisesRegex(ValueError, "source not found"):
            service.upsert_relation(self.db, {
                "source_id": 999,
                "source_asset_id": 1,
                "target_asset_id": 2,
                "relation_type": "depends_on",
            })

        self.assertEqual(self.db.query(CmdbRelation).count(), 0)

    def test_relation_rejects_disabled_source(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [
                {"external_id": "MES-01", "asset_type": "application", "name": "MES"},
                {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
            ],
        )
        assets = self.db.query(CmdbAsset).order_by(CmdbAsset.external_id).all()
        self.source.enabled = 0
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "enabled"):
            service.upsert_relation(self.db, {
                "source_id": self.source.id,
                "source_asset_id": assets[0].id,
                "target_asset_id": assets[1].id,
                "relation_type": "depends_on",
            })

        self.assertEqual(self.db.query(CmdbRelation).count(), 0)

    def test_asset_sync_rejects_disabled_source_and_records_failure(self):
        self.source.enabled = 0
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "enabled"):
            service.upsert_assets(
                self.db,
                self.source.id,
                [{"external_id": "DB-01", "asset_type": "database", "name": "MES DB"}],
            )

        self.assertEqual(self.db.query(CmdbAsset).count(), 0)
        job = self.db.query(CmdbSyncJob).one()
        self.assertEqual(job.status, "failed")

    def test_relation_rejects_assets_from_another_source(self):
        other_source = CmdbSource(source_name="secondary", source_type="cmdb")
        self.db.add(other_source)
        self.db.commit()
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "MES-01", "asset_type": "application", "name": "MES"}],
        )
        service.upsert_assets(
            self.db,
            other_source.id,
            [{"external_id": "DB-01", "asset_type": "database", "name": "MES DB"}],
        )
        source_asset = self.db.query(CmdbAsset).filter_by(source_id=self.source.id).one()
        target_asset = self.db.query(CmdbAsset).filter_by(source_id=other_source.id).one()

        with self.assertRaisesRegex(ValueError, "same CMDB source"):
            service.upsert_relation(self.db, {
                "source_id": self.source.id,
                "source_asset_id": source_asset.id,
                "target_asset_id": target_asset.id,
                "relation_type": "depends_on",
            })

        self.assertEqual(self.db.query(CmdbRelation).count(), 0)

    def test_relation_rejects_inactive_asset_endpoint(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [
                {"external_id": "MES-01", "asset_type": "application", "name": "MES"},
                {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
            ],
        )
        assets = self.db.query(CmdbAsset).order_by(CmdbAsset.external_id).all()
        assets[0].status = "pending_confirmation"
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "active"):
            service.upsert_relation(self.db, {
                "source_id": self.source.id,
                "source_asset_id": assets[0].id,
                "target_asset_id": assets[1].id,
                "relation_type": "depends_on",
            })

    def test_binding_rejects_missing_asset(self):
        with self.assertRaisesRegex(ValueError, "asset not found"):
            service.upsert_binding(self.db, {"instance_id": 42, "asset_id": 999})

        self.assertEqual(self.db.query(CmdbBinding).count(), 0)

    def test_binding_rejects_inactive_asset(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "DB-01", "asset_type": "database", "name": "MES DB"}],
        )
        asset = self.db.query(CmdbAsset).one()
        asset.status = "pending_confirmation"
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "active"):
            service.upsert_binding(self.db, {"instance_id": 42, "asset_id": asset.id})

        self.assertEqual(self.db.query(CmdbBinding).count(), 0)

    def test_instance_context_ignores_dangling_binding_without_asset_null(self):
        self.db.add(CmdbBinding(instance_id=42, asset_id=999, binding_status="active"))
        self.db.commit()

        context = service.resolve_instance_context(self.db, 42)

        self.assertEqual(context["binding_status"], "unbound")
        self.assertNotIn("asset", context)

    def test_instance_context_ignores_binding_to_inactive_asset(self):
        service.upsert_assets(
            self.db,
            self.source.id,
            [{"external_id": "DB-01", "asset_type": "database", "name": "MES DB"}],
        )
        asset = self.db.query(CmdbAsset).one()
        self.db.add(CmdbBinding(instance_id=42, asset_id=asset.id, binding_status="active"))
        asset.status = "pending_confirmation"
        self.db.commit()

        context = service.resolve_instance_context(self.db, 42)

        self.assertEqual(context["binding_status"], "unbound")
        self.assertNotIn("asset", context)


class CmdbApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        for model in (
            CmdbSource, CmdbAsset, CmdbRelation, CmdbSyncJob, CmdbBinding, CmdbAssetOverride, CmdbChangeEvent,
            AssetRegion, AssetSite, AssetZone, AssetEnvironment, AssetHost, AssetCluster, AssetInstance,
            AssetRelation, AssetDiscoveryBatch, AssetCorrectionAudit,
        ):
            model.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_api_supports_source_sync_relation_topology_binding_and_instance_context(self):
        source = self.client.post(
            "/api/v1/cmdb/sources",
            json={"source_name": "primary", "source_type": "cmdb"},
        )
        self.assertEqual(source.status_code, 200)
        source_id = source.json()["id"]

        synced = self.client.post(
            "/api/v1/cmdb/sync",
            json={
                "source_id": source_id,
                "items": [
                    {"external_id": "MES-01", "asset_type": "application", "name": "MES"},
                    {"external_id": "DB-01", "asset_type": "database", "name": "MES DB"},
                ],
            },
        )
        self.assertEqual(synced.status_code, 200)
        self.assertEqual(synced.json()["created_count"], 2)

        assets = self.client.get("/api/v1/cmdb/assets").json()["items"]
        asset_ids = {item["external_id"]: item["id"] for item in assets}
        relation = self.client.post(
            "/api/v1/cmdb/relations",
            json={
                "source_id": source_id,
                "source_asset_id": asset_ids["MES-01"],
                "target_asset_id": asset_ids["DB-01"],
                "relation_type": "depends_on",
            },
        )
        self.assertEqual(relation.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/cmdb/topology").json()["relation_count"], 1)

        binding = self.client.post(
            "/api/v1/cmdb/bindings",
            json={"instance_id": 42, "asset_id": asset_ids["DB-01"]},
        )
        self.assertEqual(binding.status_code, 200)
        context = self.client.get("/api/v1/cmdb/instance-context/42")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["asset"]["external_id"], "DB-01")

    def test_api_rejects_relation_with_missing_assets(self):
        source = self.client.post(
            "/api/v1/cmdb/sources",
            json={"source_name": "primary", "source_type": "cmdb"},
        )

        response = self.client.post(
            "/api/v1/cmdb/relations",
            json={
                "source_id": source.json()["id"],
                "source_asset_id": 999,
                "target_asset_id": 1000,
                "relation_type": "depends_on",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("asset not found", response.json()["detail"])

    def test_api_rejects_binding_with_missing_asset(self):
        response = self.client.post(
            "/api/v1/cmdb/bindings",
            json={"instance_id": 42, "asset_id": 999},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("asset not found", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
