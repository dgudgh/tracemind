import unittest
from pathlib import Path


class GenLicenseDefaultsTest(unittest.TestCase):
    def test_default_features_cover_current_database_platforms(self):
        try:
            from gen_license import DEFAULT_FEATURES
        except ModuleNotFoundError:
            # backend/gen_license.py is the license-signing tool and is
            # deliberately withheld from the repository by .gitignore:45
            # ("License generators are retained only in the secure local
            # distribution"). A clean checkout therefore cannot import it,
            # and that is intentional -- the missing piece is a non-public
            # tool, not a code defect. Skipping keeps this test honest
            # instead of either failing forever in CI or forcing a private
            # file into the public tree with `git add -f`.
            self.skipTest(
                "backend/gen_license.py is a private tool excluded by "
                ".gitignore; not importable from a clean checkout."
            )

        self.assertTrue({
            "oracle_check",
            "mysql_check",
            "postgresql_check",
            "sqlserver_check",
            "report_export",
            "batch_operation",
            "ai_diagnosis",
        }.issubset(set(DEFAULT_FEATURES)))


class OneFilePackageSpecTest(unittest.TestCase):
    def test_onefile_spec_collects_cryptography_runtime_files(self):
        root = Path(__file__).resolve().parents[2]
        spec = (root / "scripts" / "TraceMindAI-sqlite-onefile.spec").read_text(encoding="utf-8")

        self.assertIn('collect_all("cryptography")', spec)
        self.assertIn("binaries=crypto_binaries", spec)
        self.assertIn("datas=datas + crypto_datas", spec)
        self.assertIn("+ crypto_hiddenimports", spec)
