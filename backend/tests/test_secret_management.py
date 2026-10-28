from __future__ import annotations

import base64
import hashlib
import os
import unittest
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.crypto import decrypt_password, encrypt_password
from app.core.secrets import (
    SecretResolutionError,
    local_secret_requires_rotation,
    resolve_secret,
    rewrap_secret,
    store_secret,
)
from app.models.db_instance import DBInstance, DBInstanceCredentialAudit
from app.models.system_setting import DBSystemSetting
from app.schemas.db_instance import (
    DBInstanceCreate,
    DBInstanceCredentialRotate,
    DBInstanceResponse,
)
from app.services import instances as instance_service


class SecretEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.original_key = settings.SECRET_KEY
        self.original_previous = settings.SECRET_KEY_PREVIOUS
        self.original_vault_addr = settings.SECRETS_VAULT_ADDR
        self.original_vault_namespace = settings.SECRETS_VAULT_NAMESPACE

    def tearDown(self):
        settings.SECRET_KEY = self.original_key
        settings.SECRET_KEY_PREVIOUS = self.original_previous
        settings.SECRETS_VAULT_ADDR = self.original_vault_addr
        settings.SECRETS_VAULT_NAMESPACE = self.original_vault_namespace

    def test_new_ciphertext_is_versioned_and_round_trips(self):
        settings.SECRET_KEY = "primary-key"
        settings.SECRET_KEY_PREVIOUS = ""

        ciphertext = encrypt_password("database-secret")

        self.assertTrue(ciphertext.startswith("tmsec:v2:"))
        self.assertEqual(decrypt_password(ciphertext), "database-secret")
        self.assertNotIn("database-secret", ciphertext)

    def test_legacy_bare_fernet_ciphertext_remains_readable(self):
        settings.SECRET_KEY = "legacy-key"
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        legacy = Fernet(base64.urlsafe_b64encode(digest)).encrypt(b"legacy-secret").decode()

        self.assertEqual(decrypt_password(legacy), "legacy-secret")
        self.assertTrue(local_secret_requires_rotation(legacy))

    def test_previous_key_can_be_read_and_rewrapped_with_primary_key(self):
        settings.SECRET_KEY = "old-key"
        old_ciphertext = encrypt_password("rotate-me")
        settings.SECRET_KEY = "new-key"
        settings.SECRET_KEY_PREVIOUS = "old-key"

        self.assertEqual(decrypt_password(old_ciphertext), "rotate-me")
        self.assertTrue(local_secret_requires_rotation(old_ciphertext))

        rotated = rewrap_secret(old_ciphertext)
        self.assertEqual(decrypt_password(rotated), "rotate-me")
        self.assertFalse(local_secret_requires_rotation(rotated))
        self.assertNotEqual(rotated, old_ciphertext)

    def test_environment_reference_is_resolved_without_persisting_value(self):
        reference = "env://TRACEMIND_TEST_DATABASE_PASSWORD"
        with patch.dict(os.environ, {"TRACEMIND_TEST_DATABASE_PASSWORD": "runtime-secret"}):
            self.assertEqual(resolve_secret(reference), "runtime-secret")
        self.assertEqual(store_secret(reference=reference), reference)

    def test_vault_kv_v2_reference_uses_server_side_endpoint_and_token(self):
        settings.SECRETS_VAULT_ADDR = "https://vault.example.test"
        settings.SECRETS_VAULT_NAMESPACE = "factory-a"
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"data": {"password": "vault-secret"}}}
        with (
            patch.dict(os.environ, {settings.SECRETS_VAULT_TOKEN_ENV: "vault-token"}),
            patch("requests.get", return_value=response) as request,
        ):
            value = resolve_secret("vault://database/prod/oracle#password")

        self.assertEqual(value, "vault-secret")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://vault.example.test/v1/database/data/prod/oracle")
        self.assertEqual(kwargs["headers"]["X-Vault-Token"], "vault-token")
        self.assertEqual(kwargs["headers"]["X-Vault-Namespace"], "factory-a")

    def test_non_loopback_vault_rejects_plain_http(self):
        settings.SECRETS_VAULT_ADDR = "http://vault.example.test"
        with patch.dict(os.environ, {settings.SECRETS_VAULT_TOKEN_ENV: "vault-token"}):
            with self.assertRaisesRegex(SecretResolutionError, "HTTPS"):
                resolve_secret("vault://database/prod/oracle#password")

    def test_plaintext_and_reference_are_mutually_exclusive(self):
        with self.assertRaisesRegex(SecretResolutionError, "不能同时"):
            store_secret(plaintext="secret", reference="env://DATABASE_PASSWORD")


class InstanceCredentialLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        DBInstance.__table__.create(self.engine)
        DBInstanceCredentialAudit.__table__.create(self.engine)
        DBSystemSetting.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_external_reference_is_redacted_from_response_and_rotation_is_audited(self):
        created = instance_service.create(
            self.db,
            DBInstanceCreate(
                instance_name="postgres-prod",
                db_type="postgresql",
                host="db.example.test",
                port=5432,
                database_name="postgres",
                username="monitor",
                password_secret_ref="env://DATABASE_PROD_PASSWORD",
            ),
            actor="security-admin",
        )

        response = DBInstanceResponse.model_validate(created).model_dump()
        serialized = str(response)
        self.assertEqual(response["credential_source"], "environment")
        self.assertTrue(response["has_password"])
        self.assertNotIn("DATABASE_PROD_PASSWORD", serialized)
        self.assertNotIn("password_cipher", response)

        instance_service.rotate_credential(
            self.db,
            created,
            DBInstanceCredentialRotate(
                credential_kind="database",
                password="new-local-secret",
                reason="季度数据库凭据轮换",
            ),
            actor="security-admin",
        )

        self.assertEqual(decrypt_password(created.password_cipher), "new-local-secret")
        audits = instance_service.credential_audits(self.db, created.id)
        self.assertEqual([row.action for row in audits], ["rotated", "created"])
        self.assertEqual(audits[0].source_before, "environment")
        self.assertEqual(audits[0].source_after, "local_encrypted")
        self.assertNotIn("new-local-secret", str(audits[0].__dict__))
        self.assertNotIn("DATABASE_PROD_PASSWORD", str(audits[0].__dict__))


if __name__ == "__main__":
    unittest.main()
