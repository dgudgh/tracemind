"""C15: compatibility is a property of the surface, not a claim in a changelog.

The contract baseline already fails on semantic drift. This guard covers the other half: an
old operation, schema or table must never silently disappear while the baseline is being
refreshed, an unreviewed addition is drift in the opposite direction, and a table may only be
added after review.

The check runs in a **subprocess** on purpose: importing ``app.main`` here would create the
application (and touch the default database) inside the shared pytest process, which perturbs
unrelated tests that count audit rows.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Reviewed table additions for the capability layer. A new name here needs an owner and a
# migration in the same change; anything else means the schema grew without review.
REVIEWED_NEW_TABLES = {
    "db_capability_plan", "db_capability_plan_step", "db_capability_plan_revision",
    "db_capability_definition", "db_capability_preparation", "db_capability_batch",
    "db_capability_execution", "db_capability_control_command", "db_capability_target_fence",
    "db_capability_reconciliation", "db_capability_proposal",
    "db_capability_effect_receipt",
}


def _contract_diff() -> dict:
    result = subprocess.run([sys.executable, "scripts/check_backend_contract_baseline.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    text = result.stdout.strip()
    start = text.find("{")
    assert start >= 0, f"no JSON in checker output: {result.stdout[-400:]}{result.stderr[-400:]}"
    # The checker prints its JSON report followed by a human hint line.
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


def test_no_public_operation_or_schema_may_disappear():
    diff = _contract_diff()["openapi"]
    assert diff["removed_operations"] == [], diff["removed_operations"]
    assert diff["removed_schemas"] == [], diff["removed_schemas"]


def test_an_unreviewed_operation_is_drift_in_the_other_direction():
    diff = _contract_diff()["openapi"]
    assert diff["added_operations"] == [], diff["added_operations"]


def test_no_table_or_column_may_disappear_and_new_tables_stay_reviewed():
    diff = _contract_diff()["database"]
    assert diff["removed_tables"] == [], diff["removed_tables"]
    # A changed table means an existing column/type/index moved: that is a semantic change.
    assert diff["changed_tables"] == [], diff["changed_tables"]
    assert set(diff["added_tables"]) <= REVIEWED_NEW_TABLES, diff["added_tables"]
