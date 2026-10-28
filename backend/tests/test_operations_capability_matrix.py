import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import generic_operations, operations
from app.cmdb.models import AssetInstance
from app.core.database import Base, get_db
from app.models.access_control import AccessRole, RoleActionPermission, UserAccessRole, UserAssetScope
from app.models.db_instance import DBInstance
from app.services import operations_capabilities


class OperationsCapabilityMatrixTest(unittest.TestCase):
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

    @staticmethod
    def _instance(db_type: str, name: str, host: str, *, enabled: int = 1, credential: bool = True) -> DBInstance:
        return DBInstance(
            instance_name=name,
            db_type=db_type,
            host=host,
            port={"oracle": 1521, "mysql": 3306, "postgresql": 5432, "sqlserver": 1433}[db_type],
            username="monitor",
            password_cipher="env://TEST_DB_PASSWORD" if credential else "",
            enabled=enabled,
        )

    @staticmethod
    def _asset(instance: DBInstance, *, connection_status: str) -> AssetInstance:
        return AssetInstance(
            stable_key=f"native:{instance.id}",
            name=instance.instance_name,
            db_type=instance.db_type,
            ip=instance.host,
            port=instance.port,
            role="primary",
            host_id=instance.id,
            cluster_id=instance.id,
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            native_instance_id=instance.id,
            status="healthy" if connection_status == "success" else "warning",
            attributes={"connection_test_status": connection_status, "last_connection_test_at": "2026-08-23T10:00:00"},
        )

    def test_catalog_covers_all_four_engines_and_keeps_high_risk_actions_guarded(self):
        with self.Session() as db:
            result = operations_capabilities.build_capability_matrix(db)

        self.assertEqual([item["key"] for item in result["engines"]], list(operations_capabilities.ENGINE_ORDER))
        self.assertGreaterEqual(result["summary"]["capability_count"], 15)
        switchover = next(item for item in result["items"] if item["key"] == "ha.switchover")
        self.assertTrue(all(cell["mode"] == "plan_only" for cell in switchover["engines"].values()))
        restore = next(item for item in result["items"] if item["key"] == "protection.restore")
        self.assertTrue(all(cell["mode"] == "approval_required" for cell in restore["engines"].values()))

    def test_runtime_status_distinguishes_verified_failed_and_missing_credentials(self):
        with self.Session() as db:
            verified = self._instance(db_type="oracle", name="verified", host="10.0.0.1")
            failed = self._instance(db_type="mysql", name="failed", host="10.0.0.2")
            missing = self._instance(db_type="postgresql", name="missing", host="10.0.0.3", credential=False)
            db.add_all([verified, failed, missing])
            db.flush()
            db.add_all([
                self._asset(verified, connection_status="success"),
                self._asset(failed, connection_status="failed"),
                self._asset(missing, connection_status="unknown"),
            ])
            db.commit()
            verified_result = operations_capabilities.build_capability_matrix(db, selected_instance_id=verified.id)
            failed_result = operations_capabilities.build_capability_matrix(db, selected_instance_id=failed.id)
            missing_result = operations_capabilities.build_capability_matrix(db, selected_instance_id=missing.id)

        runtime_key = "runtime.overview"
        verified_cell = next(item for item in verified_result["items"] if item["key"] == runtime_key)["engines"]["oracle"]
        failed_cell = next(item for item in failed_result["items"] if item["key"] == runtime_key)["engines"]["mysql"]
        missing_cell = next(item for item in missing_result["items"] if item["key"] == runtime_key)["engines"]["postgresql"]
        self.assertEqual(verified_cell["availability"], "available")
        self.assertEqual(failed_cell["availability"], "blocked")
        self.assertEqual(missing_cell["availability"], "blocked")

    def test_static_matrix_route_is_not_captured_by_generic_engine_route(self):
        app = FastAPI()
        app.include_router(operations.router, prefix="/api/v1")
        app.include_router(generic_operations.router, prefix="/api/v1")

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        response = TestClient(app).get("/api/v1/operations/capability-matrix")
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.json())

    def test_api_applies_instance_scope_and_execute_permission(self):
        with self.Session() as db:
            allowed = self._instance(db_type="oracle", name="allowed", host="10.0.0.1")
            hidden = self._instance(db_type="mysql", name="hidden", host="10.0.0.2")
            db.add_all([allowed, hidden])
            db.flush()
            allowed_asset = self._asset(allowed, connection_status="success")
            hidden_asset = self._asset(hidden, connection_status="success")
            db.add_all([allowed_asset, hidden_asset])
            db.flush()
            role = AccessRole(role_key="matrix-viewer", name="能力矩阵只读", status="active")
            db.add(role)
            db.flush()
            db.add_all([
                RoleActionPermission(role_id=role.id, action_key="operations.read"),
                UserAccessRole(user_id=42, role_id=role.id),
                UserAssetScope(user_id=42, scope_type="instance", scope_id=allowed_asset.id, access_level="read"),
            ])
            db.commit()
            allowed_id = allowed.id
            hidden_id = hidden.id

        app = FastAPI()

        @app.middleware("http")
        async def attach_auth(request: Request, call_next):
            request.state.auth = {"source": "db", "user_id": 42, "role": "user"}
            return await call_next(request)

        app.include_router(operations.router, prefix="/api/v1")

        def override_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)
        response = client.get("/api/v1/operations/capability-matrix")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["instances"]], [allowed_id])
        self.assertFalse(payload["summary"]["execution_permission"])
        hidden_response = client.get(f"/api/v1/operations/capability-matrix?instance_id={hidden_id}")
        self.assertEqual(hidden_response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
