from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_backend_contract_baseline", ROOT / "scripts" / "check_backend_contract_baseline.py"
)
assert SPEC and SPEC.loader
CONTRACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACTS)


class BackendContractBaselineTest(unittest.TestCase):
    def test_openapi_diff_reports_added_removed_and_changed_operations(self):
        baseline = {
            "paths": {
                "/kept": {"get": {"operationId": "old"}},
                "/removed": {"post": {"operationId": "removed"}},
            },
            "components": {"schemas": {"Kept": {"type": "object"}, "Removed": {"type": "object"}}},
        }
        current = {
            "paths": {
                "/kept": {"get": {"operationId": "new"}},
                "/added": {"patch": {"operationId": "added"}},
            },
            "components": {"schemas": {"Kept": {"type": "string"}, "Added": {"type": "object"}}},
        }

        diff = CONTRACTS.summarize_openapi_diff(baseline, current)

        self.assertEqual(diff["added_operations"], ["PATCH /added"])
        self.assertEqual(diff["removed_operations"], ["POST /removed"])
        self.assertEqual(diff["changed_operations"], ["GET /kept"])
        self.assertEqual(diff["added_schemas"], ["Added"])
        self.assertEqual(diff["removed_schemas"], ["Removed"])
        self.assertEqual(diff["changed_schemas"], ["Kept"])

    def test_database_diff_reports_table_contract_drift(self):
        baseline = {"tables": {"kept": {"columns": ["id"]}, "removed": {"columns": []}}}
        current = {"tables": {"kept": {"columns": ["id", "name"]}, "added": {"columns": []}}}

        diff = CONTRACTS.summarize_database_diff(baseline, current)

        self.assertEqual(diff["added_tables"], ["added"])
        self.assertEqual(diff["removed_tables"], ["removed"])
        self.assertEqual(diff["changed_tables"], ["kept"])

    def test_inspection_job_control_surface_is_frozen_in_the_baselines(self):
        """The four job routes and the two audit columns must be in the *reviewed* contract.

        ``check_backend_contract_baseline.py --update`` would have written the whole current
        document, freezing whatever else happened to be uncommitted at the time (ISS-50). This
        asserts the frozen files carry exactly this batch's surface.
        """
        openapi = json.loads(CONTRACTS.OPENAPI_BASELINE.read_text(encoding="utf-8"))
        schema = json.loads(CONTRACTS.SCHEMA_BASELINE.read_text(encoding="utf-8"))

        for method, path in (
            ("get", "/api/v1/inspection/jobs"),
            ("get", "/api/v1/inspection/jobs/{job_id}"),
            ("post", "/api/v1/inspection/jobs/{job_id}/cancel"),
            ("post", "/api/v1/inspection/jobs/{job_id}/retry"),
        ):
            self.assertIn(path, openapi["paths"], path)
            self.assertIn(method, openapi["paths"][path], f"{method.upper()} {path}")

        table = schema["tables"]["db_inspection_job"]
        columns = {column["name"] for column in table["columns"]}
        self.assertLessEqual({"cancel_requested_at", "cancel_requested_by"}, columns)
        self.assertIn(
            "ix_db_inspection_job_created_at",
            {index["name"] for index in table["indexes"]},
        )
        # The outcome is derived, never stored: two truths would eventually disagree.
        self.assertNotIn("outcome", columns)


if __name__ == "__main__":
    unittest.main()
