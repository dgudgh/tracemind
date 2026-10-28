from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_repository_secrets", ROOT / "scripts" / "check_repository_secrets.py"
)
assert SPEC and SPEC.loader
SECRETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SECRETS)


class RepositorySecretScanTest(unittest.TestCase):
    def test_allows_explicit_documentation_and_template_placeholders(self):
        self.assertTrue(
            SECRETS.is_safe_placeholder(
                "credential in URL", "DATABASE_URL=mysql://user:replace-with-password@127.0.0.1/db"
            )
        )
        self.assertTrue(
            SECRETS.is_safe_placeholder(
                "credential in URL", "postgresql://<USER>:<PASSWORD>@<HOST>/<DATABASE>"
            )
        )
        self.assertTrue(
            SECRETS.is_safe_placeholder(
                "credential in URL", "f'mysql://{username}:{password}@{host}/{database}'"
            )
        )

    def test_rejects_a_concrete_embedded_password(self):
        line = "DATABASE_URL=mysql://admin:ActualPassword123@db.example/production"  # secret-scan: allow -- scanner fixture

        self.assertRegex(line, SECRETS.PATTERNS["credential in URL"])
        self.assertFalse(SECRETS.is_safe_placeholder("credential in URL", line))


if __name__ == "__main__":
    unittest.main()
