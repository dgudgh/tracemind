import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.module_config import DEFAULT_MODULES, _ensure_defaults
from app.models.module_config import ModuleConfig
from app.models.user import UserModulePermission


class DiagnosisModuleMergeTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        ModuleConfig.__table__.create(self.engine)
        UserModulePermission.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_old_diagnosis_permissions_move_to_unified_workspace_without_duplicates(self):
        self.session.add(ModuleConfig(
            module_key="diagnose.ai",
            module_name="AI 诊断助手",
            category="ai",
            sort_order=29,
            enabled=1,
        ))
        self.session.add_all([
            UserModulePermission(user_id=7, module_key="diagnose.ai"),
            UserModulePermission(user_id=7, module_key="diagnose.rule"),
            UserModulePermission(user_id=8, module_key="diagnose.ai"),
            UserModulePermission(user_id=8, module_key="diagnose.smart"),
            UserModulePermission(user_id=9, module_key="diagnose.rule"),
        ])
        self.session.commit()

        _ensure_defaults(self.session)

        module_keys = {row.module_key for row in self.session.query(ModuleConfig).all()}
        self.assertIn("diagnose.smart", module_keys)
        self.assertNotIn("diagnose.ai", module_keys)
        self.assertNotIn("diagnose.rule", module_keys)

        user_7_keys = [
            row.module_key
            for row in self.session.query(UserModulePermission).filter_by(user_id=7).all()
        ]
        user_8_keys = [
            row.module_key
            for row in self.session.query(UserModulePermission).filter_by(user_id=8).all()
        ]
        user_9_keys = [
            row.module_key
            for row in self.session.query(UserModulePermission).filter_by(user_id=9).all()
        ]
        self.assertEqual(user_7_keys, ["diagnose.smart"])
        self.assertEqual(user_8_keys, ["diagnose.smart"])
        self.assertEqual(user_9_keys, ["diagnose.smart"])

    def test_incident_snapshot_pages_have_default_module_permissions(self):
        module_keys = {item["module_key"] for item in DEFAULT_MODULES}

        self.assertTrue({
            "diagnose.incidents",
            "diagnose.snapshot.agents",
            "diagnose.snapshot.policies",
        }.issubset(module_keys))


if __name__ == "__main__":
    unittest.main()
