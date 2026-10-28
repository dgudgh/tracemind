import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.oracle.oracle_connection import _oracle_connection_key, oracle_connection
from app.cmdb.models import AssetInstance
from app.core.crypto import decrypt_password, encrypt_password
from app.models.db_instance import DBInstance, DBInstanceCredentialAudit
from app.schemas.db_instance import DBInstanceConnectionTest
from app.services import instances as instance_service


class InstanceConnectionConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        DBInstance.__table__.create(self.engine)
        DBInstanceCredentialAudit.__table__.create(self.engine)
        AssetInstance.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def payload(self, **overrides):
        values = {
            "instance_name": "postgres-test",
            "db_type": "postgresql",
            "host": "198.51.100.151",
            "port": 5432,
            "database_name": "postgres",
            "username": "postgres",
            "password": "new-secret",
        }
        values.update(overrides)
        return DBInstanceConnectionTest(**values)

    @patch("app.services.instances.test_connection")
    def test_new_configuration_is_tested_without_being_persisted(self, test_connection):
        test_connection.return_value = {"version": "16.7"}

        result = instance_service.test_connection_configuration(self.db, self.payload())

        self.assertEqual(result["version"], "16.7")
        self.assertEqual(self.db.query(DBInstance).count(), 0)
        candidate = test_connection.call_args.args[0]
        self.assertEqual(candidate.host, "198.51.100.151")
        self.assertEqual(decrypt_password(candidate.password_cipher), "new-secret")

    @patch("app.services.instances.test_connection")
    def test_edit_configuration_reuses_saved_password_without_updating_row(self, test_connection):
        stored = DBInstance(
            instance_name="postgres-test",
            db_type="postgresql",
            host="198.51.100.151",
            port=5432,
            database_name="postgres",
            username="postgres",
            password_cipher=encrypt_password("saved-secret"),
            enabled=1,
        )
        self.db.add(stored)
        self.db.commit()
        self.db.refresh(stored)
        original_host = stored.host
        test_connection.return_value = {"version": "16.7"}

        instance_service.test_connection_configuration(
            self.db,
            self.payload(instance_id=stored.id, host="198.51.100.152", password=None),
        )

        candidate = test_connection.call_args.args[0]
        self.assertEqual(candidate.host, "198.51.100.152")
        self.assertEqual(decrypt_password(candidate.password_cipher), "saved-secret")
        self.db.refresh(stored)
        self.assertEqual(stored.host, original_host)

    @patch("app.services.instances.test_connection")
    def test_saved_connection_test_records_recovery_without_changing_topology(self, test_connection):
        stored = DBInstance(
            instance_name="oracle-standby",
            db_type="oracle",
            host="198.51.100.211",
            port=1521,
            service_name="reportdb",
            username="system",
            password_cipher=encrypt_password("saved-secret"),
            enabled=1,
        )
        self.db.add(stored)
        self.db.commit()
        self.db.refresh(stored)
        asset = AssetInstance(
            stable_key=f"native-instance:{stored.id}",
            name="reportdb",
            db_type="Oracle",
            ip=stored.host,
            port=stored.port,
            role="adg_standby",
            host_id=1,
            cluster_id=1,
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            status="healthy",
            native_instance_id=stored.id,
            discovery_source="automatic",
            attributes={
                "architecture_type": "data_guard",
                "discovery_status": "failed",
                "discovery_error": "ORA-01033",
            },
        )
        self.db.add(asset)
        self.db.commit()
        test_connection.return_value = {
            "version": "19.0.0.0.0",
            "architecture": {
                "architecture_type": "data_guard",
                "role_type": "standby",
            },
        }

        result = instance_service.test_connection_and_record(self.db, stored)

        self.db.refresh(asset)
        self.assertEqual(result["version"], "19.0.0.0.0")
        self.assertEqual(asset.role, "adg_standby")
        self.assertEqual(asset.attributes["discovery_status"], "failed")
        self.assertEqual(asset.attributes["connection_test_status"], "success")
        self.assertEqual(asset.attributes["connection_test_architecture"], "data_guard")
        self.assertNotIn("connection_test_error", asset.attributes)
        self.assertTrue(test_connection.call_args.kwargs["force_live"])

    @patch("app.services.instances.test_connection")
    def test_saved_connection_failure_is_recorded_and_forces_live_probe(self, test_connection):
        stored = DBInstance(
            instance_name="oracle-primary",
            db_type="oracle",
            host="198.51.100.200",
            port=1521,
            service_name="mesdb",
            username="system",
            password_cipher=encrypt_password("saved-secret"),
            enabled=1,
        )
        self.db.add(stored)
        self.db.commit()
        self.db.refresh(stored)
        asset = AssetInstance(
            stable_key=f"native-instance:{stored.id}",
            name="mesdb",
            db_type="Oracle",
            ip=stored.host,
            port=stored.port,
            role="adg_primary",
            host_id=1,
            cluster_id=1,
            region_id=1,
            site_id=1,
            zone_id=1,
            env_id=1,
            status="healthy",
            native_instance_id=stored.id,
            discovery_source="automatic",
            attributes={
                "architecture_type": "data_guard",
                "discovery_status": "success",
                "connection_test_status": "success",
                "connection_test_version": "19.0.0.0.0",
                "connection_test_architecture": "data_guard",
                "connection_test_role": "standby",
            },
        )
        self.db.add(asset)
        self.db.commit()
        test_connection.side_effect = RuntimeError("ORA-01033: ORACLE 正在初始化或关闭")

        with self.assertRaisesRegex(RuntimeError, "ORA-01033"):
            instance_service.test_connection_and_record(self.db, stored)

        self.db.refresh(asset)
        self.assertEqual(asset.attributes["discovery_status"], "success")
        self.assertEqual(asset.attributes["connection_test_status"], "failed")
        self.assertIn("ORA-01033", asset.attributes["connection_test_error"])
        self.assertNotIn("connection_test_version", asset.attributes)
        self.assertNotIn("connection_test_architecture", asset.attributes)
        self.assertNotIn("connection_test_role", asset.attributes)
        self.assertTrue(test_connection.call_args.kwargs["force_live"])

    def test_force_live_oracle_connection_ignores_recent_failure_cache(self):
        stored = DBInstance(
            instance_name="oracle-primary",
            db_type="oracle",
            host="198.51.100.200",
            port=1521,
            service_name="mesdb",
            username="system",
            password_cipher=encrypt_password("saved-secret"),
            enabled=1,
        )
        connection = MagicMock()
        fake_oracledb = SimpleNamespace(
            connect=MagicMock(return_value=connection),
            SYSDBA=object(),
        )

        with (
            patch.dict("sys.modules", {"oracledb": fake_oracledb}),
            patch("app.collectors.oracle.oracle_connection.build_oracle_dsn", return_value="dsn"),
            patch(
                "app.collectors.oracle.oracle_connection._recent_failure_message",
                return_value="cached failure",
            ) as recent_failure,
        ):
            with oracle_connection(stored, force_live=True):
                pass

        recent_failure.assert_not_called()
        fake_oracledb.connect.assert_called_once()
        connection.close.assert_called_once()

    def test_oracle_failure_cache_key_changes_when_password_changes(self):
        first = DBInstance(
            instance_name="oracle-primary",
            db_type="oracle",
            host="198.51.100.200",
            port=1521,
            service_name="mesdb",
            username="system",
            password_cipher=encrypt_password("old-secret"),
        )
        second = DBInstance(
            instance_name=first.instance_name,
            db_type=first.db_type,
            host=first.host,
            port=first.port,
            service_name=first.service_name,
            username=first.username,
            password_cipher=encrypt_password("new-secret"),
        )

        self.assertNotEqual(_oracle_connection_key(first), _oracle_connection_key(second))


if __name__ == "__main__":
    unittest.main()
