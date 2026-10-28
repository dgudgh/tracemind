"""Upgrades must bring new settings in without overwriting the ones an operator set (ISS-42).

`_bootstrap_shared_config` copied the example file once and returned forever after. A release
that added a key shipped it into nothing: the setting stayed absent and the symptom appeared much
later as "the option does nothing". Overwriting instead would be worse - it would silently revert
a password or a port someone changed by hand.

The merge is three-way over the last written template, and local edits win every conflict.
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.runtime import config_merge
from app.runtime.release_manager import ReleaseManager


BASE = "PORT=8801\nLOG_LEVEL=info\n"
OURS = "PORT=8801\nLOG_LEVEL=debug\nLOCAL_ONLY=1\n"
THEIRS = "PORT=8801\nLOG_LEVEL=info\nNEW_SETTING=on\n"


class MergeEnvTest(unittest.TestCase):
    def test_new_release_key_is_added(self):
        merged, report = config_merge.merge_env(BASE, OURS, THEIRS)
        self.assertEqual(report.strategy, "three_way")
        self.assertIn("NEW_SETTING", report.added)
        self.assertIn("NEW_SETTING=on", merged)

    def test_local_edit_survives(self):
        merged, _report = config_merge.merge_env(BASE, OURS, THEIRS)
        self.assertIn("LOG_LEVEL=debug", merged)

    def test_local_only_key_survives(self):
        merged, _report = config_merge.merge_env(BASE, OURS, THEIRS)
        self.assertIn("LOCAL_ONLY=1", merged)

    def test_unedited_key_follows_the_release(self):
        _merged, report = config_merge.merge_env(BASE, OURS, "PORT=9902\nLOG_LEVEL=debug\n")
        self.assertIn("PORT", report.updated)

    def test_both_sides_changed_is_a_conflict_that_keeps_local(self):
        ours = "PORT=8801\n"
        base = "PORT=8801\n"
        merged, report = config_merge.merge_env(base, "PORT=9000\n", "PORT=9100\n")
        self.assertIn("PORT", report.conflicts)
        self.assertIn("PORT=9000", merged)

    def test_release_removal_drops_an_untouched_key(self):
        merged, report = config_merge.merge_env(BASE, BASE, "PORT=8801\n")
        self.assertIn("LOG_LEVEL", report.removed)
        self.assertNotIn("LOG_LEVEL", merged)

    def test_release_removal_keeps_a_locally_edited_key(self):
        merged, report = config_merge.merge_env(BASE, OURS, "PORT=8801\n")
        self.assertIn("LOG_LEVEL", report.kept_local)
        self.assertIn("LOG_LEVEL=debug", merged)

    def test_missing_base_degrades_to_two_way(self):
        merged, report = config_merge.merge_env(None, OURS, THEIRS)
        self.assertEqual(report.strategy, "two_way")
        self.assertIn("NEW_SETTING", report.added)
        self.assertIn("LOG_LEVEL=debug", merged)


class ReleaseManagerConfigMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = ReleaseManager(self.root)
        self.manager._ensure_layout()
        self.release = self.root / "releases" / "rel-2"
        (self.release / "config").mkdir(parents=True)
        (self.release / "config" / "app.env.example").write_text(BASE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self) -> str:
        return (self.manager.shared_dir / "config" / "app.env").read_text(encoding="utf-8")

    def test_first_bootstrap_copies_and_records_the_base(self):
        self.manager._bootstrap_shared_config(self.release)
        self.assertIn("PORT=8801", self._config())
        self.assertTrue((self.manager.shared_dir / "config" / "app.env.base").is_file())

    def test_upgrade_adds_new_keys_and_keeps_local_edits(self):
        self.manager._bootstrap_shared_config(self.release)
        config = self.manager.shared_dir / "config" / "app.env"
        config.write_text("PORT=8801\nLOG_LEVEL=debug\n", encoding="utf-8")

        (self.release / "config" / "app.env.example").write_text(THEIRS, encoding="utf-8")
        report = self.manager._bootstrap_shared_config(self.release)

        self.assertEqual(report["strategy"], "three_way")
        self.assertIn("NEW_SETTING", report["added"])
        merged = self._config()
        self.assertIn("NEW_SETTING=on", merged)
        # The whole defect is this line: a later release must not revert a local decision.
        self.assertIn("LOG_LEVEL=debug", merged)

    def test_merge_backs_up_the_previous_file(self):
        self.manager._bootstrap_shared_config(self.release)
        (self.release / "config" / "app.env.example").write_text(THEIRS, encoding="utf-8")
        report = self.manager._bootstrap_shared_config(self.release)
        self.assertIsNotNone(report.get("backup_path"))
        self.assertTrue(Path(report["backup_path"]).is_file())

    def test_unchanged_config_is_not_rewritten(self):
        self.manager._bootstrap_shared_config(self.release)
        before = self._config()
        report = self.manager._bootstrap_shared_config(self.release)
        self.assertEqual(report.get("added"), [])
        self.assertEqual(self._config(), before)


if __name__ == "__main__":
    unittest.main()
