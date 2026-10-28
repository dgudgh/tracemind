from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.connection_certificate import ConnectionCertificateAudit, ConnectionCertificateProfile
from app.models.db_instance import DBInstance
from app.schemas.connection_security import CertificateProfileCreate
from app.services import connection_security


def certificate_pair(common_name: str = "db.example.test") -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


class ConnectionCertificateServiceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        ConnectionCertificateProfile.__table__.create(self.engine)
        ConnectionCertificateAudit.__table__.create(self.engine)
        DBInstance.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_pem_profile_is_validated_redacted_and_materialized_temporarily(self):
        cert_pem, key_pem = certificate_pair()
        profile = connection_security.create_profile(
            self.db,
            CertificateProfileCreate(
                name="production-postgresql-mtls",
                ca_certificate_pem=cert_pem,
                client_certificate_pem=cert_pem,
                private_key=key_pem,
            ),
            actor="security-admin",
        )
        response = connection_security.serialize_profile(profile)
        serialized = str(response)
        self.assertEqual(response["lifecycle_status"], "valid")
        self.assertTrue(response["has_private_key"])
        self.assertNotIn("BEGIN CERTIFICATE", serialized)
        self.assertNotIn("BEGIN PRIVATE KEY", serialized)

        instance = DBInstance(
            instance_name="postgres-prod",
            db_type="postgresql",
            host="db.example.test",
            port=5432,
            username="monitor",
            password_cipher="unused",
            certificate_profile_id=profile.id,
            tls_verify=1,
        )
        instance.certificate_profile = profile
        with connection_security.database_tls_material(instance) as material:
            for key in ("ca", "cert", "key"):
                self.assertTrue(Path(str(material[key])).is_file())
                self.assertEqual(os.stat(str(material[key])).st_mode & 0o777, 0o600)
            private_key_path = Path(str(material["key"]))
        self.assertFalse(private_key_path.exists())

        result = connection_security.test_profile(profile, self.db, "security-admin")
        self.assertTrue(result["ok"])
        self.assertEqual(
            [row.action for row in connection_security.audits(self.db, profile.id)],
            ["tested", "created"],
        )

    def test_mismatched_client_certificate_and_private_key_is_rejected(self):
        cert_pem, _ = certificate_pair("db-a.example.test")
        _, other_key = certificate_pair("db-b.example.test")
        with self.assertRaisesRegex(ValueError, "不匹配"):
            connection_security.create_profile(
                self.db,
                CertificateProfileCreate(
                    name="bad-pair",
                    client_certificate_pem=cert_pem,
                    private_key=other_key,
                ),
                actor="security-admin",
            )

    def test_external_private_key_reference_is_resolved_only_when_used(self):
        cert_pem, key_pem = certificate_pair()
        profile = connection_security.create_profile(
            self.db,
            CertificateProfileCreate(
                name="vault-ready",
                client_certificate_pem=cert_pem,
                private_key_secret_ref="env://TEST_CLIENT_PRIVATE_KEY",
            ),
            actor="security-admin",
        )
        instance = DBInstance(
            instance_name="mysql-prod",
            db_type="mysql",
            host="db.example.test",
            port=3306,
            username="monitor",
            password_cipher="unused",
            certificate_profile_id=profile.id,
        )
        instance.certificate_profile = profile
        with patch.dict(os.environ, {"TEST_CLIENT_PRIVATE_KEY": key_pem}):
            with connection_security.database_tls_material(instance) as material:
                self.assertTrue(Path(str(material["key"])).is_file())

    def test_referenced_profile_cannot_be_deleted(self):
        cert_pem, _ = certificate_pair()
        profile = connection_security.create_profile(
            self.db,
            CertificateProfileCreate(name="shared-ca", ca_certificate_pem=cert_pem),
            actor="security-admin",
        )
        self.db.add(DBInstance(
            instance_name="postgres-prod",
            db_type="postgresql",
            host="db.example.test",
            port=5432,
            username="monitor",
            password_cipher="unused",
            certificate_profile_id=profile.id,
        ))
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "不能删除"):
            connection_security.delete_profile(self.db, profile, "security-admin")

    def test_catalog_covers_all_four_database_engines(self):
        expected = {"oracle", "mysql", "postgresql", "sqlserver"}
        self.assertEqual({row["db_type"] for row in connection_security.minimum_privilege_catalog()}, expected)
        self.assertEqual({row["db_type"] for row in connection_security.compatibility_matrix()}, expected)
        for row in connection_security.minimum_privilege_catalog():
            self.assertTrue(row["create_sql"])
            self.assertTrue(row["revoke_sql"])

    def test_oracle_wallet_requires_absolute_path_and_resolves_password(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = connection_security.create_profile(
                self.db,
                CertificateProfileCreate(
                    name="oracle-wallet",
                    profile_type="oracle_wallet",
                    wallet_directory=directory,
                    wallet_password_secret_ref="env://TEST_WALLET_PASSWORD",
                ),
                actor="security-admin",
            )
            instance = DBInstance(
                instance_name="oracle-prod",
                db_type="oracle",
                host="db.example.test",
                port=1521,
                username="monitor",
                password_cipher="unused",
                certificate_profile_id=profile.id,
            )
            instance.certificate_profile = profile
            with patch.dict(os.environ, {"TEST_WALLET_PASSWORD": "wallet-secret"}):
                self.assertEqual(
                    connection_security.oracle_wallet_settings(instance),
                    {"wallet_location": directory, "wallet_password": "wallet-secret"},
                )


if __name__ == "__main__":
    unittest.main()
