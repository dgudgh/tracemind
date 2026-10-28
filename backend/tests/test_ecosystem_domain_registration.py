import unittest

from app import models
from app.api.v1.diagnosis_center import ROUTE_TARGETS
from app.api.v1.module_config import DEFAULT_MODULES
from app.api.v1.registry import routers
from app.core.database import Base
from app.main import ADMIN_ONLY_PREFIXES


class EcosystemDomainRegistrationTest(unittest.TestCase):
    def test_new_domain_routers_are_mounted(self):
        prefixes = {router.prefix for router in routers}
        self.assertTrue({"/cmdb", "/automation", "/identity"}.issubset(prefixes))
        self.assertIn("/ecosystem", prefixes)

    def test_new_domain_models_are_registered_before_create_all(self):
        expected_tables = {
            "cmdb_source",
            "cmdb_asset",
            "cmdb_relation",
            "cmdb_sync_job",
            "cmdb_binding",
            "backup_platform_config",
            "backup_external_job",
            "backup_external_run",
            "backup_restore_drill",
            "archive_storage_config",
            "archive_artifact",
            "archive_transfer",
            "archive_lifecycle_request",
            "automation_plan",
            "automation_approval_request",
            "automation_execution_run",
            "automation_execution_output",
            "automation_rollback_plan",
            "identity_provider",
            "identity_group_mapping",
            "external_identity",
            "identity_login_audit",
            "observability_query_audit",
        }
        self.assertTrue(expected_tables.issubset(Base.metadata.tables))
        for export_name in [
            "CmdbSource",
            "AutomationPlan",
            "IdentityProvider",
            "BackupPlatformConfig",
            "ArchiveStorageConfig",
            "ObservabilityQueryAudit",
        ]:
            self.assertTrue(hasattr(models, export_name), export_name)

    def test_new_domain_permissions_and_admin_boundaries_are_registered(self):
        module_keys = {item["module_key"] for item in DEFAULT_MODULES}
        self.assertTrue(
            {
                "asset.center",
                "monitor.observability.logs",
                "monitor.observability.grafana",
                "monitor.observability.apm",
                "backup.platform",
                "ops.governance.objectStorage",
                "protection.automation",
                "system.identity",
            }.issubset(module_keys)
        )
        self.assertNotIn("integration.ecosystem", module_keys)
        route_keys = {item["key"] for item in ROUTE_TARGETS}
        self.assertNotIn("integration.ecosystem", route_keys)
        self.assertTrue(
            {
                "asset.center",
                "integration.notify",
                "integration.itsm",
                "monitor.observability.logs",
                "monitor.observability.grafana",
                "monitor.observability.apm",
                "backup.platform",
                "ops.governance.objectStorage",
                "protection.automation",
                "system.identity",
            }.issubset(route_keys)
        )
        self.assertTrue(
            {"/api/v1/cmdb", "/api/v1/automation", "/api/v1/identity"}.issubset(ADMIN_ONLY_PREFIXES)
        )


if __name__ == "__main__":
    unittest.main()
