from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.inspection_policy import DBInspectionPolicy
from app.services.inspection import policy_service


class InspectionPolicyDefaultTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        DBInspectionPolicy.__table__.create(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _create(self, key: str, db_types: list[str], default: bool = False) -> dict:
        return policy_service.create_policy(
            self.session,
            {
                "policy_key": key,
                "name": key,
                "db_types": db_types,
                "enabled": True,
                "item_config": [
                    {
                        "key": "connection.basic",
                        "name": "连接与基础信息",
                        "enabled": True,
                    }
                ],
                "report_config": {
                    "assistant_default": default,
                    "assistant_default_db_types": db_types if default else [],
                },
            },
        )

    def test_new_default_only_replaces_overlapping_database_types(self):
        first = self._create("first", ["oracle", "mysql"], default=True)
        second = self._create("second", ["oracle"], default=True)

        first_row = policy_service.get_policy(self.session, str(first["id"]))
        second_row = policy_service.get_policy(self.session, str(second["id"]))

        self.assertEqual(first_row["assistant_default_db_types"], ["mysql"])
        self.assertEqual(second_row["assistant_default_db_types"], ["oracle"])

    def test_cloned_template_does_not_inherit_default_assignment(self):
        source = self._create("source", ["postgresql"], default=True)
        source_model = policy_service.get_policy_model(self.session, source["id"])

        clone = policy_service.clone_policy(
            self.session,
            source_model,
            "source_copy",
            "source copy",
        )

        self.assertFalse(clone["assistant_default"])
        self.assertEqual(clone["assistant_default_db_types"], [])


if __name__ == "__main__":
    unittest.main()
