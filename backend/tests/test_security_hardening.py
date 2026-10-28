import hashlib
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class PasswordSecurityTest(unittest.TestCase):
    def test_password_hash_round_trip_and_rejects_wrong_password(self):
        from app.core.security import hash_password, verify_password

        encoded = hash_password("StrongPass1")

        self.assertNotEqual(encoded, hashlib.sha256(b"StrongPass1").hexdigest())
        self.assertTrue(verify_password("StrongPass1", encoded))
        self.assertFalse(verify_password("WrongPass1", encoded))

    def test_legacy_sha256_hash_is_recognized_for_one_time_migration(self):
        from app.core.security import verify_password

        legacy_hash = hashlib.sha256(b"LegacyPass1").hexdigest()

        self.assertTrue(verify_password("LegacyPass1", legacy_hash))

    def test_password_complexity_requires_mixed_case_and_number(self):
        from app.core.security import password_complexity_issues

        self.assertEqual(password_complexity_issues("StrongPass1"), [])
        self.assertTrue(password_complexity_issues("alllowercase"))


class AuthenticationHardeningTest(unittest.TestCase):
    def test_mes_login_rejects_unconfigured_or_incorrect_token(self):
        from app.core.auth import authenticate_by_mes

        with patch.dict(os.environ, {"MES_AUTH_TOKEN": "configured-token"}, clear=False):
            self.assertIsNone(authenticate_by_mes("any-eight-character-token"))

    def test_mes_login_issues_random_session_only_for_configured_token(self):
        from app.core.auth import authenticate_by_mes

        with patch.dict(os.environ, {"MES_AUTH_TOKEN": "configured-token"}, clear=False):
            first = authenticate_by_mes("configured-token")
            second = authenticate_by_mes("configured-token")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["token"], second["token"])


class SecurityRegressionGuardTest(unittest.TestCase):
    def test_default_config_does_not_embed_database_credentials(self):
        source = (ROOT / "backend/app/core/config.py").read_text(encoding="utf-8")
        init_source = (ROOT / "backend/app/services/sqlite_init_service.py").read_text(encoding="utf-8")

        self.assertNotIn("root:" + "123456@", source)
        self.assertNotIn('SECRET_KEY: str = "change-me-in-production"', source)
        self.assertNotIn("admin" + "123", init_source)

    def test_license_errors_fail_closed(self):
        source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")

        self.assertIn('return JSONResponse(status_code=503', source)

    def test_high_risk_routers_require_administrator(self):
        source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")

        self.assertIn("ADMIN_ONLY_PREFIXES", source)
        for prefix in ("/api/v1/users", "/api/v1/ssh", "/api/v1/recovery"):
            self.assertIn(prefix, source)

    def test_authentication_does_not_accept_query_string_tokens(self):
        source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")

        self.assertNotIn('request.query_params.get("token"', source)

    def test_sqlite_does_not_use_memory_journal(self):
        source = (ROOT / "backend/app/core/database.py").read_text(encoding="utf-8")

        self.assertNotIn('PRAGMA journal_mode=MEMORY', source)
        self.assertIn('PRAGMA journal_mode=WAL', source)

    def test_sensitive_runtime_artifacts_are_not_tracked(self):
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True
        ).splitlines()
        sensitive = [
            path for path in tracked
            if path.endswith((".pem", "license.json", ".db", ".sqlite", ".sqlite3"))
        ]

        self.assertEqual(sensitive, [])

    def test_runtime_data_directory_is_ignored(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("data/", gitignore)

    def test_release_snapshot_excludes_private_build_and_license_artifacts(self):
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True
        ).splitlines()
        forbidden_prefixes = (
            "backend/reports/",
            "build/",
            "dist/",
            "exe-test/",
            "frontend/dist/",
            "license_tool/",
        )
        forbidden_names = {"backend/gen_license.py", "backend/license_tool.py"}
        leaked = [
            path for path in tracked
            if path.startswith(forbidden_prefixes) or path in forbidden_names
        ]

        self.assertEqual(leaked, [])

    def test_release_sources_do_not_embed_lab_network_addresses(self):
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, text=True
        ).split("\0")
        text_suffixes = {
            ".bat", ".cjs", ".cmd", ".html", ".js", ".jsx", ".md",
            ".py", ".sh", ".sql", ".txt", ".yaml", ".yml",
        }
        lab_prefix = "192.168." + "102."
        n2n_prefix = "10.194." + "144."
        leaked = []
        for relative_path in tracked:
            if not relative_path:
                continue
            path = ROOT / relative_path
            if path.suffix.lower() not in text_suffixes or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            if lab_prefix in content or n2n_prefix in content:
                leaked.append(relative_path)

        self.assertEqual(leaked, [])

    def test_docs_do_not_publish_license_generator_instructions(self):
        docs = ROOT / "docs"
        leaked = []
        for path in docs.rglob("*.md"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            if "license_tool/gen_license" in content:
                leaked.append(str(path.relative_to(ROOT)))

        self.assertEqual(leaked, [])

    def test_release_sources_do_not_embed_known_default_credentials(self):
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, text=True
        ).split("\0")
        text_suffixes = {
            ".bat", ".cjs", ".cmd", ".html", ".js", ".jsx", ".md",
            ".py", ".sh", ".sql", ".txt", ".yaml", ".yml",
        }
        forbidden = ("admin" + "123", "root / " + "123456")
        leaked = []
        for relative_path in tracked:
            if not relative_path:
                continue
            path = ROOT / relative_path
            if path.suffix.lower() not in text_suffixes or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            if any(value in content for value in forbidden):
                leaked.append(relative_path)

        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
