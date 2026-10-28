#!/usr/bin/env python3
"""Validate S1 plan/readiness only; never run product, DB, SSH, service, or business tests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable

TASK_RE = re.compile(r"^EP-T\d+(?:[a-z])?(?:-[a-z]+)?$")
CASE_RE = re.compile(r"^EP-C\d+$")
EXECUTION_KEY = ("case_id", "variant_id", "cell_id", "requirement_context_digest")
SELECTOR_FIELDS = (*EXECUTION_KEY, "output")
FAMILY_IDS = {"EP-C67", "EP-C70", "EP-C73", "EP-C83", "EP-C84"}
VAGUE = {"from_frozen_matrix", "from_task_engine_os_cpu_matrix", "from_frozen_case_context", "from_profile"}
MODES = {"input_contract", "fixture", "downstream_retest", "blocking_implementation"}


def _dig(root: Any, path: str) -> Any:
    value = root
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def resolve_ref(value: Any, context: dict[str, Any], item: dict[str, Any] | None = None) -> Any:
    """Resolve an explicit ref. Missing refs are not_ready, never wildcard matches."""
    if not isinstance(value, dict) or set(value) != {"ref"} or not isinstance(value["ref"], str):
        return value
    bindings: dict[str, Any] = {"context": context, **context}
    if item is not None:
        bindings["item"] = item
    return _dig(bindings, value["ref"])


def resolve_selector(selector: dict[str, Any], context: dict[str, Any],
                     item: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    missing = [field for field in SELECTOR_FIELDS if field not in selector]
    if missing:
        return None, "selector_missing_fields:" + ",".join(missing)
    result: dict[str, Any] = {}
    for field in SELECTOR_FIELDS:
        try:
            result[field] = resolve_ref(selector[field], context, item)
        except KeyError as exc:
            return None, f"selector_unbound_ref:{exc.args[0]}"
        if result[field] in (None, ""):
            return None, f"selector_empty_value:{field}"
    return result, None


def _variant(case: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
    return next((item for item in case.get("variants", []) if item.get("variant_id") == variant_id), None)


def _required_roles(case: dict[str, Any], variant_id: str) -> set[str]:
    variant = _variant(case, variant_id)
    return set((variant or {}).get("acceptance_roles", case.get("acceptance_roles", [])))


def validate_instance_records(data: dict[str, Any], test_instances: Iterable[dict[str, Any]],
                              output_instances: Iterable[dict[str, Any]]) -> list[str]:
    """Pure leaf-instance validation for actual (empty today) or synthetic records."""
    errors: list[str] = []
    cases = {item["id"]: item for item in data["test_cases"]}
    tasks = {item["id"]: item for item in data["tasks"]}
    roles = set(data["roles"])

    def inspect(records: list[dict[str, Any]], kind: str) -> None:
        seen: set[tuple[Any, ...]] = set()
        for index, record in enumerate(records):
            prefix = f"{kind}[{index}]"
            missing = [field for field in EXECUTION_KEY if not record.get(field)]
            if missing:
                errors.append(f"{prefix}: missing execution key fields {','.join(missing)}")
                continue
            key = tuple(record[field] for field in EXECUTION_KEY)
            if key in seen:
                errors.append(f"{prefix}: duplicate execution key {key}")
            seen.add(key)
            case = cases.get(record["case_id"])
            if not case:
                errors.append(f"{prefix}: unknown case {record['case_id']}")
                continue
            variant_id = record["variant_id"]
            variant = _variant(case, variant_id)
            if case.get("directly_executable") is False:
                declared = {item["variant_id"] for item in case.get("variants", [])}
                if variant_id in {"__parent__", "parent", case["id"]}:
                    errors.append(f"{prefix}: family parent cannot be an executable instance")
                elif declared and variant_id not in declared:
                    errors.append(f"{prefix}: unknown family variant {variant_id}")
                elif not declared and not case.get("variant_template"):
                    errors.append(f"{prefix}: family has no leaf variant template")
            else:
                declared = set(case.get("execution_variants", ["default"]))
                if variant_id not in declared:
                    errors.append(f"{prefix}: undeclared execution variant {variant_id}")
            state = record.get("state")
            if state not in set(data["instance_model"]["leaf_state_values"]):
                errors.append(f"{prefix}: invalid leaf state {state!r}")
            acceptance = record.get("acceptance", [])
            accepted_roles = [item.get("role") for item in acceptance if isinstance(item, dict)]
            unknown = set(accepted_roles) - roles
            if unknown:
                errors.append(f"{prefix}: unknown acceptance role {sorted(unknown)}")
            if state == "passed":
                derived_receipt = bool(variant and variant.get("execution_mode") == "derived_receipt")
                if derived_receipt:
                    if kind != "output_instances":
                        errors.append(f"{prefix}: derived receipt is allowed only in output_instances")
                    if record.get("executed") is True:
                        errors.append(f"{prefix}: derived receipt must not be counted as executed")
                    if record.get("derived_only") is not True or not record.get("derived_from_execution_keys"):
                        errors.append(f"{prefix}: derived receipt requires provenance to executed leaf keys")
                elif record.get("executed") is not True:
                    errors.append(f"{prefix}: passed executable leaf requires executed=true")
                if case["id"] == "EP-C73" and not derived_receipt:
                    oracle_receipt = record.get("oracle_receipt")
                    if not isinstance(oracle_receipt, dict):
                        errors.append(f"{prefix}: C73 passed leaf requires oracle_receipt")
                    else:
                        oracle_ready, oracle_reasons = evaluate_c73_oracle(data, oracle_receipt)
                        if oracle_receipt.get("variant_id") != variant_id:
                            errors.append(f"{prefix}: C73 oracle_receipt variant mismatch")
                        if not oracle_ready:
                            errors.extend(f"{prefix}: {reason}" for reason in oracle_reasons)
                evidence = record.get("evidence", [])
                if not evidence or any(not isinstance(item, dict) or not item.get("id") or not item.get("digest")
                                       for item in evidence):
                    errors.append(f"{prefix}: passed leaf requires signed evidence id and digest")
                if any(not isinstance(item, dict) or not item.get("principal") or not item.get("signature")
                       for item in acceptance):
                    errors.append(f"{prefix}: passed leaf acceptance requires principal and signature")
                required = _required_roles(case, variant_id)
                if not required <= set(accepted_roles):
                    errors.append(f"{prefix}: passed leaf missing acceptance roles {sorted(required-set(accepted_roles))}")
            if kind == "output_instances":
                producer, output = record.get("producer_task"), record.get("output")
                if producer not in tasks:
                    errors.append(f"{prefix}: unknown producer task {producer}")
                declared_output = output in tasks.get(producer, {}).get("outputs", [])
                case_output = output == case.get("output") or any(
                    output == item.get("output") for item in case.get("variants", []))
                if not output or not (declared_output or case_output):
                    errors.append(f"{prefix}: undeclared output {output!r}")

    inspect(list(test_instances), "test_instances")
    inspect(list(output_instances), "output_instances")
    return errors


def _matching_output(data: dict[str, Any], gate: dict[str, Any], records: list[dict[str, Any]],
                     context: dict[str, Any], item: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    selector = gate.get("selector")
    if not isinstance(selector, dict):
        return None, f"missing_selector:{gate.get('task')}/{gate.get('output')}"
    resolved, error = resolve_selector(selector, context, item)
    if error:
        return None, error
    assert resolved is not None
    matches = [record for record in records
               if record.get("producer_task") == gate.get("task")
               and record.get("output") == gate.get("output")
               and all(record.get(field) == resolved[field] for field in EXECUTION_KEY)]
    if not matches:
        values = "/".join(str(resolved[field]) for field in SELECTOR_FIELDS)
        return None, f"output_not_found_or_selector_mismatch:{values}"
    if len(matches) != 1:
        return None, f"ambiguous_output_instance:{gate.get('task')}/{gate.get('output')}"
    record = matches[0]
    errors = validate_instance_records(data, [], [record])
    if errors:
        return None, errors[0]
    if record.get("state") != gate.get("expected_state", "passed"):
        return None, f"output_state_not_passed:{record.get('state')}"
    return record, None


def evaluate_task_readiness(data: dict[str, Any], task_id: str, state: dict[str, Any],
                            context: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Pure readiness evaluation. Output evidence may be usable before producer task acceptance."""
    tasks = {item["id"]: item for item in data["tasks"]}
    task = tasks.get(task_id)
    if not task:
        return False, [f"unknown_task:{task_id}"]
    context = copy.deepcopy(context if context is not None else state.get("task_contexts", {}).get(task_id, {}))
    accepted = set(state.get("accepted_tasks", []))
    reasons = [f"hard_dependency_not_accepted:{dep}" for dep in task.get("depends_on", []) if dep not in accepted]
    conditions = state.get("conditions", {})
    for branch in task.get("depends_on_if", []):
        active = conditions.get(branch["condition"])
        if active is None:
            reasons.append(f"conditional_gate_unbound:{branch['condition']}")
        elif active and branch["task"] not in accepted:
            reasons.append(f"conditional_dependency_not_accepted:{branch['task']}")
    outputs = list(state.get("output_instances", []))
    for gate in task.get("depends_on_outputs", []):
        _, error = _matching_output(data, gate, outputs, context)
        if error:
            reasons.append(f"output_gate_not_ready:{gate['task']}/{gate['output']}:{error}")
        elif gate["task"] == "EP-T14b" and gate["output"] in {"release-matrix-ready:test", "production-release-ready"}:
            ready, output_reasons = evaluate_output_readiness(data, gate["task"], gate["output"], state, context)
            if not ready:
                reasons.extend(f"output_prerequisite_not_ready:{gate['output']}:{item}" for item in output_reasons)
    for template in task.get("required_cell_gate_templates", []):
        try:
            items = resolve_ref(template["foreach"], context)
        except KeyError as exc:
            reasons.append(f"required_cells_unbound:{exc.args[0]}")
            continue
        if not isinstance(items, list) or not items:
            reasons.append("required_cells_empty")
            continue
        for item in items:
            gate = {"task": template["producer_task"], "output": item.get("output"),
                    "selector": template["selector"], "expected_state": template.get("expected_state", "passed")}
            _, error = _matching_output(data, gate, outputs, context, item)
            if error:
                reasons.append(f"required_cell_not_ready:{item.get('cell_id')}:{error}")
    if task_id == "EP-T09":
        receipt = context.get("migration_receipt")
        if not isinstance(receipt, dict) or any(receipt.get(field) in (None, "") for field in ("revision", "checksum", "verified")):
            reasons.append("migration_receipt_missing_or_incomplete")
        elif receipt.get("verified") is not True:
            reasons.append("migration_receipt_not_verified")
        if context.get("partial_ddl_reconciled") is not True:
            reasons.append("partial_ddl_not_reconciled")
        digest = context.get("requirement_context_digest")
        for gate in task.get("depends_on_outputs", []):
            record, error = _matching_output(data, gate, outputs, context)
            if error:
                near = [item for item in outputs
                        if item.get("producer_task") == gate["task"]
                        and item.get("output") == gate["output"]
                        and item.get("cell_id") == context.get("capacity_profile_cell_id")]
                if near and any(item.get("requirement_context_digest") != digest for item in near):
                    reasons.append(f"capacity_context_digest_mismatch:{gate['output']}")
            elif record and record.get("requirement_context_digest") != digest:
                reasons.append(f"capacity_context_digest_mismatch:{gate['output']}")
    if task_id == "EP-T14b":
        manifest = context.get("release_scope_manifest", {})
        contract = task.get("release_scope_contract", {})
        declared_engines = set(contract.get("declared_engines", []))
        manifest_engines = set(manifest.get("declared_engines", []))
        if manifest_engines != declared_engines:
            reasons.append("declared_six_engine_scope_mismatch")
        cells = manifest.get("required_cells")
        if isinstance(cells, list):
            cell_engines = {item.get("engine") for item in cells}
            if not declared_engines <= cell_engines:
                reasons.append("required_cell_engine_coverage_incomplete")
            if not cell_engines <= declared_engines:
                reasons.append("required_cell_contains_undeclared_engine")
        profiles = manifest.get("required_capacity_profiles")
        if not isinstance(profiles, list) or not profiles:
            reasons.append("required_capacity_profiles_empty_or_unbound")
        else:
            c67 = next(item for item in data["test_cases"] if item["id"] == "EP-C67")
            fields = c67["instance_schema"]["selector_fields"]
            profile_engines = {item.get("engine") for item in profiles}
            if not declared_engines <= profile_engines:
                reasons.append("required_capacity_engine_coverage_incomplete")
            if not profile_engines <= declared_engines:
                reasons.append("required_capacity_contains_undeclared_engine")
            for profile in profiles:
                missing = [field for field in fields if profile.get(field) in (None, "")]
                if missing:
                    reasons.append(f"capacity_profile_selector_incomplete:{','.join(missing)}")
                    continue
                matches = [record for record in outputs
                           if record.get("case_id") == "EP-C67"
                           and record.get("output") == "qualified-capacity-profile"
                           and record.get("variant_id") == profile.get("variant_id")
                           and record.get("cell_id") == profile.get("cell_id")
                           and all(record.get(field) == profile[field] for field in fields)]
                if len(matches) != 1:
                    reasons.append(f"qualified_capacity_profile_not_ready:{profile.get('load_profile')}")
                else:
                    record_errors = validate_instance_records(data, [], matches)
                    if record_errors or matches[0].get("state") != "passed":
                        reasons.append(f"qualified_capacity_profile_invalid:{profile.get('load_profile')}")
        combinations = manifest.get("required_real_combinations")
        if not isinstance(combinations, list) or not combinations:
            reasons.append("required_real_combinations_empty_or_unbound")
        else:
            combination_engines = {item.get("engine") for item in combinations}
            if not declared_engines <= combination_engines:
                reasons.append("required_real_combination_engine_coverage_incomplete")
            if not combination_engines <= declared_engines:
                reasons.append("required_real_combination_contains_undeclared_engine")
            c84 = next(item for item in data["test_cases"] if item["id"] == "EP-C84")
            selector_fields = c84["instance_schema"]["selector_fields"]
            pair_fields = c84["instance_schema"]["pair_fields"]
            ordering_fields = c84["instance_schema"]["ordering_fields"]
            for combination in combinations:
                required = selector_fields + pair_fields + ordering_fields + ["variant_id", "cell_id"]
                missing = [field for field in required if combination.get(field) in (None, "")]
                if missing:
                    reasons.append(f"real_combination_selector_incomplete:{','.join(missing)}")
                    continue
                matches = [record for record in outputs
                           if record.get("case_id") == "EP-C84"
                           and record.get("variant_id") == "real-combination"
                           and record.get("output") == "qualified-real-combination"
                           and record.get("cell_id") == combination["cell_id"]
                           and all(record.get(field) == combination[field]
                                   for field in selector_fields + pair_fields + ordering_fields)]
                if len(matches) != 1:
                    reasons.append(f"qualified_real_combination_not_ready:{combination.get('engine')}")
                    continue
                record = matches[0]
                record_errors = validate_instance_records(data, [], [record])
                if record_errors or record.get("state") != "passed":
                    reasons.append(f"qualified_real_combination_invalid:{combination.get('engine')}")
                source_v = combination["source_payload_v"]
                source_v1 = combination["source_payload_v1"]
                source_diff = combination["source_diff"]
                review = combination["review_evidence"]
                payload_required = {"source_revision", "normalized_payload_digest", "content_manifest_digest"}
                review_required = {"id", "digest", "reviewer_role", "signature"}
                if not isinstance(source_v, dict) or not isinstance(source_v1, dict) \
                        or not payload_required <= set(source_v) or not payload_required <= set(source_v1) \
                        or source_v.get("normalized_payload_digest") == source_v1.get("normalized_payload_digest") \
                        or not isinstance(source_diff, dict) or source_diff.get("metadata_only") is not False \
                        or not source_diff.get("changed_files") or not source_diff.get("semantic_change_digest") \
                        or not isinstance(review, dict) or not review_required <= set(review) \
                        or review.get("reviewer_role") != "REL":
                    reasons.append(f"real_pair_source_not_materially_different:{combination.get('engine')}")
                pair_selector = combination["pair_output_selector"]
                expected_pair = c84["instance_schema"]["pair_output_by_engine"].get(combination["engine"])
                pair_binding = next((item for item in data.get("output_bindings", [])
                                     if expected_pair and item.get("task") == expected_pair["task"]
                                     and item.get("output") == expected_pair["output"]), None)
                if not isinstance(pair_selector, dict) or set(pair_selector) != {"task", *SELECTOR_FIELDS} \
                        or not expected_pair or pair_selector.get("task") != expected_pair["task"] \
                        or pair_selector.get("output") != expected_pair["output"] or not pair_binding \
                        or pair_selector.get("case_id") != pair_binding.get("case_id") \
                        or pair_selector.get("variant_id") != pair_binding.get("variant_id"):
                    reasons.append(f"real_pair_output_selector_invalid:{combination.get('engine')}")
                else:
                    pair_gate = {"task": pair_selector["task"], "output": pair_selector["output"],
                                 "selector": {field: pair_selector[field] for field in SELECTOR_FIELDS}}
                    pair_record, pair_error = _matching_output(data, pair_gate, outputs, context)
                    if pair_error:
                        reasons.append(f"real_pair_output_not_ready:{combination.get('engine')}:{pair_error}")
                    elif pair_record and any(pair_record.get(field) != combination.get(field)
                                             for field in ("source_payload_v", "source_payload_v1",
                                                           "source_diff", "review_evidence")):
                        reasons.append(f"real_pair_output_evidence_mismatch:{combination.get('engine')}")
                if any(combination[field] is not True for field in ordering_fields):
                    reasons.append(f"real_combination_ordering_or_rollback_incomplete:{combination.get('engine')}")
                profile_matches = [profile for profile in (profiles or [])
                                   if all(profile.get(field) == combination.get(field) for field in selector_fields)]
                if len(profile_matches) != 1:
                    reasons.append(f"real_combination_capacity_profile_mismatch:{combination.get('engine')}")
    return not reasons, reasons


def evaluate_milestone_readiness(data: dict[str, Any], milestone_id: str, state: dict[str, Any],
                                 context: dict[str, Any]) -> tuple[bool, list[str]]:
    """Evaluate milestone tasks and output leaves without hidden whole-task aggregation."""
    milestone = next((item for item in data["milestones"] if item["id"] == milestone_id), None)
    if not milestone:
        return False, [f"unknown_milestone:{milestone_id}"]
    accepted = set(state.get("accepted_tasks", []))
    reasons = [f"milestone_task_not_accepted:{tid}" for tid in milestone.get("tasks", []) if tid not in accepted]
    for branch in milestone.get("conditional_tasks", []):
        active = state.get("conditions", {}).get(branch["condition"])
        if active is None:
            reasons.append(f"milestone_condition_unbound:{branch['condition']}")
        elif active and branch["task"] not in accepted:
            reasons.append(f"milestone_conditional_task_not_accepted:{branch['task']}")
    for gate in milestone.get("output_gates", []):
        _, error = _matching_output(data, gate, list(state.get("output_instances", [])), context)
        if error:
            reasons.append(f"milestone_output_not_ready:{gate['task']}/{gate['output']}:{error}")
    return not reasons, reasons


def _external_gate_ready(data: dict[str, Any], gate_id: str, state: dict[str, Any]) -> tuple[bool, str | None]:
    gate = next((item for item in data["external_inputs"] if item["id"] == gate_id), None)
    actual = state.get("external_inputs", {}).get(gate_id)
    if not gate or not isinstance(actual, dict) or actual.get("state") != "accepted":
        return False, f"external_gate_not_accepted:{gate_id}"
    if actual.get("source_case_id") == "EP-C83" and actual.get("source_variant_id") == "rehearsal":
        return False, "production_rehearsal_cannot_satisfy_E07"
    evidence = actual.get("evidence", {})
    missing = [field for field in gate.get("evidence_schema_fields", []) if not evidence.get(field)]
    if missing:
        return False, f"external_gate_evidence_missing:{','.join(missing)}"
    if not {"OPS", "SEC"} <= set(actual.get("accepted_roles", [])):
        return False, "external_gate_missing_ops_or_sec_acceptance"
    principals = actual.get("principals", {})
    people = [principals.get(role) for role in ("REL", "OPS", "SEC", "build_operator")]
    if any(not item for item in people) or len(set(people)) != len(people):
        return False, "sec_rel_ops_build_principals_not_distinct"
    return True, None


def evaluate_output_readiness(data: dict[str, Any], task_id: str, output: str,
                              state: dict[str, Any], context: dict[str, Any]) -> tuple[bool, list[str]]:
    tasks = {item["id"]: item for item in data["tasks"]}
    task = tasks.get(task_id)
    if not task or output not in task.get("outputs", []):
        return False, [f"undeclared_task_output:{task_id}/{output}"]
    binding = next((item for item in data.get("output_bindings", [])
                    if item["task"] == task_id and item["output"] == output), None)
    if not binding:
        return False, [f"missing_output_binding:{task_id}/{output}"]
    gate = {"task": task_id, "output": output,
            "selector": {"case_id": binding["case_id"], "variant_id": binding["variant_id"],
                         "cell_id": {"ref": binding["cell_ref"]},
                         "requirement_context_digest": {"ref": "context.requirement_context_digest"},
                         "output": output}}
    output_instances = list(state.get("output_instances", []))
    receipt, error = _matching_output(data, gate, output_instances, context)
    reasons = [error] if error else []
    if task_id == "EP-T14b" and receipt:
        combinations = context.get("release_scope_manifest", {}).get("required_real_combinations", [])
        expected_keys = {("EP-C84", "real-combination", item.get("cell_id"), item.get("requirement_context_digest"))
                         for item in combinations}
        provenance = receipt.get("derived_from_execution_keys", [])
        provenance_keys = {tuple(item.get(field) for field in EXECUTION_KEY)
                           for item in provenance if isinstance(item, dict)}
        if not expected_keys or provenance_keys != expected_keys:
            reasons.append("derived_receipt_provenance_not_exact")
        for key in expected_keys:
            leaves = [item for item in output_instances
                      if tuple(item.get(field) for field in EXECUTION_KEY) == key
                      and item.get("state") == "passed" and item.get("executed") is True]
            if len(leaves) != 1:
                reasons.append(f"derived_receipt_leaf_not_executed:{key[2]}")
    requirements = task.get("output_requirements", {}).get(output, {})
    if task_id == "EP-T14b" and output == "release-matrix-ready:test":
        task_ready, task_reasons = evaluate_task_readiness(data, "EP-T14b", state, context)
        if not task_ready:
            reasons.extend(f"test_matrix_prerequisite:{item}" for item in task_reasons)
    if requirements.get("external_gate"):
        ready, gate_error = _external_gate_ready(data, requirements["external_gate"], state)
        if not ready and gate_error:
            reasons.append(gate_error)
    if task_id == "EP-T14b" and output == "production-release-ready":
        test_ready, test_reasons = evaluate_output_readiness(
            data, "EP-T14b", "release-matrix-ready:test", state, context)
        if not test_ready:
            reasons.extend(f"production_requires_valid_test_matrix:{item}" for item in test_reasons)
        att = state.get("output_attestations", {}).get(f"{task_id}/{output}", {})
        if not att.get("same_tested_payload") or not att.get("tested_payload_digest") \
                or att.get("tested_payload_digest") != att.get("production_payload_digest") \
                or att.get("post_test_rebuild_or_regrouping") is not False:
            reasons.append("production_same_tested_payload_not_proved")
        if att.get("production_verifier_success") is not True:
            reasons.append("production_verifier_not_successful")
        if att.get("controlled_verify_install_smoke") is not True:
            reasons.append("controlled_verify_install_smoke_not_successful")
    return not reasons, reasons


def evaluate_c73_oracle(data: dict[str, Any], receipt: dict[str, Any]) -> tuple[bool, list[str]]:
    """Evaluate synthetic C73 planning receipts; this performs no build or cryptography."""
    case = next(item for item in data["test_cases"] if item["id"] == "EP-C73")
    variant_id = receipt.get("variant_id")
    variant = _variant(case, variant_id)
    reasons: list[str] = []
    if not variant:
        return False, [f"c73_unknown_variant:{variant_id}"]
    fields = variant.get("machine_oracle", {}).get("required_fields", [])
    missing = [field for field in fields if receipt.get(field) in (None, "", [])]
    if missing:
        reasons.append("c73_required_fields_missing:" + ",".join(missing))
    if variant_id == "self-payload":
        if receipt.get("builder_a_identity") == receipt.get("builder_b_identity"):
            reasons.append("c73_self_builders_not_independent")
        digests = {receipt.get("builder_a_payload_digest"), receipt.get("builder_b_payload_digest"),
                   receipt.get("normalized_payload_digest")}
        if None in digests or len(digests) != 1:
            reasons.append("c73_self_payload_digest_mismatch")
    elif variant_id == "third-party-integrity":
        if receipt.get("classification") != "third-party-opaque":
            reasons.append("c73_third_party_classification_invalid")
        if receipt.get("raw_artifact_hash") != receipt.get("packaged_raw_artifact_hash"):
            reasons.append("c73_third_party_raw_hash_drift")
    elif variant_id == "incident-rebuild":
        if receipt.get("new_signing_key_id") in set(receipt.get("compromised_key_ids", [])):
            reasons.append("c73_compromised_key_reactivated")
        if receipt.get("rebuilt_payload_digest") in set(receipt.get("revoked_payload_digests", [])):
            reasons.append("c73_revoked_digest_reactivated")
    package = receipt.get("package_classification", {})
    inventory = package.get("package_file_inventory", [])
    classification = package.get("file_classification", {})
    signed = package.get("whole_package_signed_files", [])
    if not inventory or set(classification) != set(inventory):
        reasons.append("c73_payload_classification_not_exhaustive")
    allowed = set(case["package_classification_oracle"]["allowed_classes"])
    if any(not isinstance(value, list) or len(value) != 1 or value[0] not in allowed
           for value in classification.values()):
        reasons.append("c73_payload_classification_not_mutually_exclusive")
    if set(signed) != set(inventory):
        reasons.append("c73_whole_package_signature_coverage_incomplete")
    return not reasons, reasons


def _graph(data: dict[str, Any], errors: list[str], checks: list[str]) -> tuple[dict[str, set[str]], list[str]]:
    tasks = {item["id"]: item for item in data["tasks"]}
    edges: dict[str, set[str]] = {}
    for tid, task in tasks.items():
        deps = set(task.get("depends_on", []))
        deps.update(item["task"] for item in task.get("depends_on_if", []))
        deps.update(item["task"] for item in task.get("depends_on_outputs", []))
        edges[tid] = deps
        (checks if deps <= set(tasks) and tid not in deps else errors).append(f"{tid}: valid dependencies")
    active: set[str] = set()
    visited: set[str] = set()
    order: list[str] = []

    def visit(tid: str) -> None:
        if tid in active:
            errors.append(f"dependency cycle at {tid}")
            return
        if tid in visited:
            return
        active.add(tid)
        for dep in sorted(edges[tid]):
            if dep in tasks:
                visit(dep)
        active.remove(tid)
        visited.add(tid)
        order.append(tid)

    for tid in tasks:
        visit(tid)
    if not any(item.startswith("dependency cycle") for item in errors):
        checks.append("whole/output/conditional task DAG")
    return edges, order


def validate_model(data: dict[str, Any], source_cases: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str], int]:
    """Pure structural validation independent of Markdown and filesystem state."""
    errors: list[str] = []
    checks: list[str] = []

    def check(condition: bool, description: str) -> None:
        (checks if condition else errors).append(description)

    tasks = {item["id"]: item for item in data.get("tasks", [])}
    cases = {item["id"]: item for item in data.get("test_cases", [])}
    roles = set(data.get("roles", []))
    check(data.get("format_version") == 3, "traceability schema v3")
    check(data.get("design_version") == "0.3", "design version 0.3")
    check(data.get("implementation_status") == "planned", "implementation remains planned")
    check(len(tasks) == len(data.get("tasks", [])) == 24, "24 unique schedulable task IDs")
    check(all(TASK_RE.fullmatch(tid) for tid in tasks), "task IDs support core/real suffixes")
    check("EP-T06a" not in tasks, "legacy T06a is not schedulable")
    alias = next((item for item in data.get("history_aliases", []) if item.get("id") == "EP-T06a"), {})
    check(alias.get("schedulable") is False, "legacy T06a is history_alias only")
    check(len(cases) == len(data.get("test_cases", [])) == 84, "84 unique case entry IDs")
    check(data.get("test_cases") == source_cases, "case source and traceability are identical")
    check(set(int(cid[4:]) for cid in cases) == set(range(1, 85)), "continuous EP-C01..EP-C84 IDs")
    check(data.get("test_instances") == [] and data.get("output_instances") == [], "actual instance collections remain empty")
    check("SEC" in roles, "SEC role declared")
    constraint = next((item for item in data.get("role_constraints", []) if item.get("role") == "SEC"), {})
    check(set(constraint.get("must_be_distinct_from", [])) == {"REL", "OPS", "build_operator"},
          "SEC distinct from REL/OPS/build operator")
    e07 = next((item for item in data.get("external_inputs", []) if item.get("id") == "E07"), {})
    check(e07.get("status") == "waiting_external" and e07.get("approval_role") == "SEC",
          "E07 remains waiting_external with SEC approval")
    edges, order = _graph(data, errors, checks)

    def ancestors(tid: str) -> set[str]:
        found: set[str] = set()
        pending = list(edges.get(tid, set()))
        while pending:
            dep = pending.pop()
            if dep not in found:
                found.add(dep)
                pending.extend(edges.get(dep, set()))
        return found

    work_ids: list[str] = []
    for tid, task in tasks.items():
        check(task.get("owner_role") in roles and task.get("review_role") in roles, f"{tid}: owner/reviewer roles")
        check(task.get("status") == "planned", f"{tid}: planned")
        check(bool(task.get("work_packages")) and bool(task.get("deliverables")) and bool(task.get("exit_criteria")),
              f"{tid}: actionable metadata")
        work_ids.extend(item["id"] for item in task.get("work_packages", []))
        owned = {case["id"] for case in cases.values() if case.get("primary_task") == tid}
        check(bool(owned) and set(task.get("tests", [])) == owned, f"{tid}: tests exactly equal primary cases")
        for gate in task.get("depends_on_outputs", []):
            check(gate.get("output") in tasks.get(gate.get("task"), {}).get("outputs", []),
                  f"{tid}: declared output {gate.get('output')}")
            selector = gate.get("selector")
            check(isinstance(selector, dict) and set(selector) == set(SELECTOR_FIELDS),
                  f"{tid}: exact leaf selector for {gate.get('output')}")
            check(not any(value in VAGUE for value in (selector or {}).values() if isinstance(value, str)),
                  f"{tid}: no vague selector token")
    check(len(work_ids) == len(set(work_ids)) == 109, "109 unique work-package IDs")

    for cid, case in cases.items():
        check(CASE_RE.fullmatch(cid) is not None, f"{cid}: valid case ID")
        check(all(key in case for key in ("level", "title", "primary_task", "collaborating_tasks", "collaborations", "acceptance_roles")),
              f"{cid}: complete common case fields")
        check(case.get("primary_task") in tasks, f"{cid}: existing primary")
        check(case.get("status") == "planned" and case.get("executed") is False, f"{cid}: no implied execution")
        acceptance = set(case.get("acceptance_roles", []))
        check("QA" in acceptance and len(acceptance) >= 2 and acceptance <= roles, f"{cid}: joint known-role acceptance")
        collaborations = case.get("collaborations", [])
        check({item.get("task") for item in collaborations} == set(case.get("collaborating_tasks", [])),
              f"{cid}: collaborators classified")
        for item in collaborations:
            task, mode, blocking = item.get("task"), item.get("mode"), item.get("blocks_primary_acceptance")
            check(task in tasks and task != case.get("primary_task"), f"{cid}: valid collaborator {task}")
            check(mode in MODES and isinstance(blocking, bool), f"{cid}: collaboration semantics {task}")
            if blocking:
                check(task in ancestors(case["primary_task"]), f"{cid}: blocking collaborator upstream {task}")
            selector = item.get("output_selector")
            template = item.get("output_selector_template")
            if mode == "fixture" and blocking and task == "EP-T14a":
                check(bool(selector or template), f"{cid}: environment fixture has leaf selector")
            if selector:
                check(set(selector) == set(SELECTOR_FIELDS), f"{cid}: exact fixture selector")
                check(not any(value in VAGUE for value in selector.values() if isinstance(value, str)),
                      f"{cid}: no vague fixture selector")
        if cid in FAMILY_IDS:
            check(case.get("kind") == "case_family" and case.get("directly_executable") is False,
                  f"{cid}: non-executable case family")
            check(case.get("execution_key") == list(EXECUTION_KEY), f"{cid}: exact execution key")
            check(case.get("parent_status_not_a_consumer_gate") is True, f"{cid}: parent cannot gate consumers")
            check(case.get("test_instances") == [] and case.get("output_instances") == [], f"{cid}: no invented family instances")
            check(case.get("status") != "passed", f"{cid}: family parent is not passed")
    check({item["variant_id"] for item in cases["EP-C70"]["variants"]} ==
          {"runtime", "core", "pminus1", "dm", "kingbase", "native"}, "C70 six variants")
    check(all(item.get("primary_task") == cases["EP-C70"]["primary_task"] for item in cases["EP-C70"]["variants"]),
          "C70 variants inherit primary")
    check(all(item.get("primary_task") == cases["EP-C73"]["primary_task"] for item in cases["EP-C73"]["variants"]),
          "C73 variants inherit primary")
    check(all(bool(item.get("machine_oracle", {}).get("required_fields"))
              and bool(item.get("machine_oracle", {}).get("reject_reasons"))
              and item.get("oracle_receipt_required") is True
              and item.get("oracle_receipt_field") == "oracle_receipt"
              for item in cases["EP-C73"]["variants"]), "C73 variants have machine oracles and rejection reasons")
    classification = cases["EP-C73"].get("package_classification_oracle", {})
    check(classification.get("exhaustive") is True and classification.get("mutually_exclusive") is True
          and set(classification.get("allowed_classes", [])) == {"self-built", "third-party-opaque"},
          "C73 payload classification is exhaustive and mutually exclusive")
    check({item["variant_id"] for item in cases["EP-C83"]["variants"]} == {"rehearsal", "production-handoff"},
          "C83 trust variants separated")
    check(next(item for item in cases["EP-C83"]["variants"] if item["variant_id"] == "production-handoff")["status"] == "waiting_external",
          "C83 production handoff waits for E07")
    check(cases["EP-C67"].get("instances_from") == {"ref": "release_scope_manifest.required_capacity_profiles"}
          and bool(cases["EP-C67"].get("variant_template")), "C67 has frozen profile instance template")
    c84dep = cases["EP-C84"].get("requires_case_outputs", [{}])[0]
    check(c84dep.get("case_id") == "EP-C67" and c84dep.get("output") == "qualified-capacity-profile"
          and set(c84dep.get("selector", {})) == set(SELECTOR_FIELDS)
          and c84dep.get("state") == "passed", "case DAG C67 -> C84 uses exact leaf selector")
    check(cases["EP-C84"].get("kind") == "case_family"
          and cases["EP-C84"].get("instances_from") == {"ref": "release_scope_manifest.required_real_combinations"},
          "C84 is a frozen-scope real-combination family")
    check({item["variant_id"] for item in cases["EP-C84"].get("variants", [])} ==
          {"real-combination", "test-matrix", "production-envelope"},
          "C84 separates real leaves from derived channel receipts")
    real_oracle = next(item for item in cases["EP-C84"]["variants"]
                       if item["variant_id"] == "real-combination").get("machine_oracle", {})
    check(bool(real_oracle.get("source_payload_schema")) and bool(real_oracle.get("source_diff_schema"))
          and real_oracle.get("source_diff_schema", {}).get("metadata_only_must_equal") is False
          and bool(real_oracle.get("review_evidence_schema")),
          "C84 real-pair oracle rejects metadata-only or unverifiable source changes")
    check(next(item for item in cases["EP-C84"]["variants"] if item["variant_id"] == "test-matrix")
          .get("directly_executable") is False
          and next(item for item in cases["EP-C84"]["variants"] if item["variant_id"] == "production-envelope")
          .get("directly_executable") is False, "C84 aggregate receipts are not directly executable")
    check(all(item.get("allowed_instance_collection") == "output_instances"
              and item.get("counts_as_business_execution") is False
              for item in cases["EP-C84"]["variants"]
              if item["variant_id"] in {"test-matrix", "production-envelope"}),
          "C84 derived receipts are output-only and never count as business execution")
    bindings = data.get("output_bindings", [])
    binding_cases = [(item["case_id"], item["variant_id"]) for item in bindings
                     if item["case_id"] not in FAMILY_IDS]
    check(all(variant in cases[cid].get("execution_variants", []) for cid, variant in binding_cases),
          "ordinary output bindings use declared execution variants")
    family_bindings = [(item["case_id"], item["variant_id"]) for item in bindings
                       if item["case_id"] in FAMILY_IDS]
    check(all(variant in {entry["variant_id"] for entry in cases[cid].get("variants", [])}
              for cid, variant in family_bindings), "family output bindings use declared variants")
    declared_outputs = {(tid, output) for tid, task in tasks.items() for output in task.get("outputs", [])}
    bound_outputs = {(item.get("task"), item.get("output")) for item in bindings}
    check(declared_outputs == bound_outputs and len(bindings) == len(bound_outputs),
          "every declared task output has one leaf binding")
    check(all(item.get("task") in tasks and item.get("case_id") in cases
              and isinstance(item.get("cell_ref"), str) and item["cell_ref"].startswith("context.")
              for item in bindings), "output bindings target known tasks/cases and explicit context refs")
    check(tasks["EP-T09"]["depends_on"] == ["EP-T05", "EP-T05a", "EP-T07", "EP-T08", "EP-T13b", "EP-T06a-core"],
          "T09 exact six hard dependencies")
    check({(item["task"], item["output"]) for item in tasks["EP-T09"]["depends_on_outputs"]} == {
          ("EP-T04", "single-provider-capacity-v0"), ("EP-T06a-core", "dual-generation-capacity-v0"),
          ("EP-T08", "platform-ab-budget-v0")}, "T09 exact three capacity outputs")
    check("EP-T05a" in tasks["EP-T08"]["depends_on"] and any(
          item["task"] == "EP-T05a" and item["output"] == "migrator-contract"
          for item in tasks["EP-T08"]["depends_on_outputs"]), "T08 hard dependency plus migrator-contract")
    check(not ({"EP-T06b", "EP-T11", "EP-T06a-real", "EP-T12"} & ancestors("EP-T09")),
          "T09 single-host lane independent of real/multi")
    milestones = {item["id"]: item for item in data["milestones"]}
    check(set(milestones) >= {"M1-base", "M1-DM", "M2-core", "M2-real", "M3-single", "final-matrix"},
          "full 0.3 milestones persisted")
    check("EP-T03" not in milestones["M1-base"]["tasks"]
          and milestones["M1-base"]["output_gates"][0]["output"] == "test-trust-ready",
          "M1-base uses trust output not T03 whole acceptance")
    check("EP-T14a" not in milestones["M0"]["tasks"] and milestones["M0"]["output_gates"][0]["output"] == "plan",
          "M0 uses T14a plan output not whole acceptance")
    check(tasks["EP-T15a"]["depends_on_outputs"][0]["output"] == "release-matrix-ready:test"
          and tasks["EP-T15b"]["depends_on_outputs"][0]["output"] == "production-release-ready",
          "T15a test output and T15b production output")
    check(tasks["EP-T14b"]["required_cell_gate_templates"][0]["foreach"] ==
          {"ref": "release_scope_manifest.required_cells"}, "T14b expands REL-frozen release cells only")
    scope_contract = tasks["EP-T14b"].get("release_scope_contract", {})
    check(set(scope_contract.get("declared_engines", [])) ==
          {"oracle", "mysql", "postgresql", "sqlserver", "dm", "kingbasees"}
          and scope_contract.get("must_cover_every_declared_engine") is True,
          "T14b final scope requires all six declared engines")
    check(tasks["EP-T14b"]["output_requirements"]["release-matrix-ready:test"].get("producer_task_readiness_required") is True
          and tasks["EP-T14b"]["output_requirements"]["production-release-ready"].get("requires_output") ==
          {"task": "EP-T14b", "output": "release-matrix-ready:test"},
          "T14b test receipt has full prerequisites and production requires valid test receipt")
    check("native-vv1-pair" in tasks["EP-T02c"].get("outputs", [])
          and cases["EP-C84"]["instance_schema"]["pair_output_by_engine"] == {
              "oracle": {"task": "EP-T02c", "output": "native-vv1-pair"},
              "mysql": {"task": "EP-T02c", "output": "native-vv1-pair"},
              "postgresql": {"task": "EP-T02c", "output": "native-vv1-pair"},
              "sqlserver": {"task": "EP-T02c", "output": "native-vv1-pair"},
              "dm": {"task": "EP-T06a-real", "output": "dm-vv1-pair"},
              "kingbasees": {"task": "EP-T12", "output": "kingbase-vv1-pair"},
          }, "C84 consumes exact native/DM/Kingbase real-pair outputs")
    check(set(data.get("instance_model", {}).get("parent_case_families", [])) == FAMILY_IDS,
          "instance model lists all five non-executable parent families")
    check(not validate_instance_records(data, data.get("test_instances", []), data.get("output_instances", [])),
          "empty actual instances valid")
    return errors, checks, order, len(work_ids)


def _canonical_docs(workspace_root: Path) -> Path:
    return workspace_root / "docs" / "blueprint" / "engine-extension-packs-2026-10-26"


def _link_exists(doc: Path, target: str, base: Path, workspace_root: Path) -> bool:
    target = target.strip("<>").split("#", 1)[0]
    if not target or "://" in target or target.startswith("mailto:"):
        return True
    if (doc.parent / target).resolve().exists():
        return True
    relative_doc = doc.relative_to(base)
    canonical_target = (_canonical_docs(workspace_root) / relative_doc).parent.joinpath(target).resolve()
    return canonical_target.exists() or (base / target).resolve().exists()


def validate_documents(data: dict[str, Any], base: Path, workspace_root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    checks: list[str] = []

    def check(condition: bool, description: str) -> None:
        (checks if condition else errors).append(description)

    tasks = {item["id"]: item for item in data["tasks"]}
    cases = {item["id"]: item for item in data["test_cases"]}
    task_md = (base / "development-tasks.md").read_text()
    case_md = (base / "test-cases.md").read_text()
    sections = re.findall(r"(?m)^### (EP-T\d+(?:[a-z])?(?:-[a-z]+)?)\s", task_md)
    rows: dict[str, list[str]] = {}
    for line in case_md.splitlines():
        if re.match(r"\| EP-C\d+ \|", line):
            cells = [item.strip() for item in line.strip("|").split("|")]
            rows[cells[0]] = cells
    check(set(sections) == set(tasks) and len(sections) == len(set(sections)), "Markdown and JSON task IDs")
    check(set(rows) == set(cases), "Markdown and JSON case IDs")
    for task in tasks.values():
        for work in task["work_packages"]:
            check(task_md.count(f"**{work['id']}**") == 1, f"{work['id']}: Markdown work package")
    for cid, case in cases.items():
        row = rows.get(cid)
        if not row:
            continue
        check(len(row) >= 6, f"{cid}: complete Markdown row")
        if len(row) < 6:
            continue
        check(row[1] == case["level"] and row[2] == case["primary_task"], f"{cid}: Markdown level/primary")
        collaborators = set(re.findall(r"EP-T\d+(?:[a-z])?(?:-[a-z]+)?", row[3]))
        check(collaborators == set(case["collaborating_tasks"]), f"{cid}: Markdown collaborators")
        check(set(row[4].split("、")) == set(case["acceptance_roles"]), f"{cid}: Markdown acceptance")
        check(row[5] == case["title"], f"{cid}: Markdown title")
    overview = (base / "README.md").read_text()
    count = sum(len(item["work_packages"]) for item in data["tasks"])
    case_count_text = re.search(rf"{len(cases)}\s*(?:个案例条目|个案例家族|条待执行案例)", overview)
    check(f"{len(tasks)} 项任务" in overview and f"{count} 个工作包" in overview
          and case_count_text is not None, "overview counts")
    for doc in sorted(base.glob("*.md")):
        content = doc.read_text()
        check(content.count("```") % 2 == 0, f"{doc.name}: balanced code fences")
        check(not any(line.rstrip() != line for line in content.splitlines()), f"{doc.name}: no trailing whitespace")
        for target in re.findall(r"\]\(([^)]+)\)", content):
            check(_link_exists(doc, target, base, workspace_root), f"{doc.name}: local link {target}")
    return errors, checks


def validate(base: Path, workspace_root: Path | None = None):
    """Filesystem wrapper; model and readiness functions above are importable and pure."""
    base = Path(base).resolve()
    workspace_root = Path(workspace_root or Path.cwd()).resolve()
    data = json.loads((base / "traceability.json").read_text())
    source = json.loads((base / "test-case-ownership.json").read_text())
    errors, checks, order, work_count = validate_model(data, source)
    doc_errors, doc_checks = validate_documents(data, base, workspace_root)
    return data, errors + doc_errors, checks + doc_checks, order, work_count


def _records(fixtures: dict[str, Any], refs: list[str]) -> list[dict[str, Any]]:
    return [copy.deepcopy(fixtures["catalog"]["output_instances"][ref]) for ref in refs]


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _state(fixtures: dict[str, Any], scenario: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = fixtures["catalog"]
    state = copy.deepcopy(catalog.get("states", {}).get(scenario.get("state_ref"), {}))
    state = _deep_merge(state, scenario.get("state", {}))
    state["output_instances"] = _records(fixtures, scenario.get("output_instance_refs", []))
    context = copy.deepcopy(catalog.get("contexts", {}).get(scenario.get("context_ref"), {}))
    context = _deep_merge(context, scenario.get("context", {}))
    return state, context


def run_self_tests(base: Path) -> tuple[list[str], list[str]]:
    """Run synthetic fixtures only. No business case is executed."""
    data = json.loads((base / "traceability.json").read_text())
    source = json.loads((base / "test-case-ownership.json").read_text())
    fixtures = json.loads((base / "readiness-fixtures.json").read_text())
    failures: list[str] = []
    passes: list[str] = []

    def expect(value: bool, description: str) -> None:
        (passes if value else failures).append(description)

    model_errors, _, _, _ = validate_model(data, source)
    expect(not model_errors, "base model pure validation: " + "; ".join(model_errors[:4]))
    for scenario in fixtures["readiness_scenarios"]:
        state, context = _state(fixtures, scenario)
        ready, reasons = evaluate_task_readiness(data, scenario["task_id"], state, context)
        expect(ready is scenario["expected_ready"], f"{scenario['id']}: expected {scenario['expected_ready']}, got {ready}: {reasons}")
        for text in scenario.get("expected_reason_contains", []):
            expect(any(text in item for item in reasons), f"{scenario['id']}: missing reason {text}: {reasons}")
    for scenario in fixtures["milestone_scenarios"]:
        state, context = _state(fixtures, scenario)
        ready, reasons = evaluate_milestone_readiness(data, scenario["milestone_id"], state, context)
        expect(ready is scenario["expected_ready"], f"{scenario['id']}: expected {scenario['expected_ready']}, got {ready}: {reasons}")
        for text in scenario.get("expected_reason_contains", []):
            expect(any(text in item for item in reasons), f"{scenario['id']}: missing reason {text}: {reasons}")
    for scenario in fixtures["output_scenarios"]:
        state, context = _state(fixtures, scenario)
        ready, reasons = evaluate_output_readiness(data, scenario["task_id"], scenario["output"], state, context)
        expect(ready is scenario["expected_ready"], f"{scenario['id']}: expected {scenario['expected_ready']}, got {ready}: {reasons}")
        for text in scenario.get("expected_reason_contains", []):
            expect(any(text in item for item in reasons), f"{scenario['id']}: missing reason {text}: {reasons}")
    for scenario in fixtures["instance_scenarios"]:
        test_records = _records(fixtures, scenario.get("test_instance_refs", []))
        test_records.extend(copy.deepcopy(scenario.get("test_instances", [])))
        outputs = _records(fixtures, scenario.get("output_instance_refs", []))
        if scenario.get("mutate") == "duplicate_first_output":
            outputs.append(copy.deepcopy(outputs[0]))
        elif scenario.get("mutate") == "remove_evidence":
            outputs[0]["evidence"] = []
        elif scenario.get("mutate") == "unknown_role":
            outputs[0]["acceptance"].append({"role": "UNKNOWN", "principal": "x", "signature": "sig-x"})
        elif scenario.get("mutate") == "executed_false":
            outputs[0]["executed"] = False
        elif scenario.get("mutate") == "derived_executed_true":
            outputs[0]["executed"] = True
        errors = validate_instance_records(data, test_records, outputs)
        expect(bool(errors) is (not scenario["expected_valid"]), f"{scenario['id']}: validity errors {errors}")
        for text in scenario.get("expected_error_contains", []):
            expect(any(text in item for item in errors), f"{scenario['id']}: missing error {text}: {errors}")
    for scenario in fixtures["model_mutation_scenarios"]:
        mutated = copy.deepcopy(data)
        if scenario["mutation"] == "family_parent_passed":
            next(item for item in mutated["test_cases"] if item["id"] == "EP-C70")["status"] = "passed"
        elif scenario["mutation"] == "missing_output_selector":
            next(item for item in mutated["tasks"] if item["id"] == "EP-T04")["depends_on_outputs"][0].pop("selector")
        errors, _, _, _ = validate_model(mutated, mutated["test_cases"])
        for text in scenario["expected_error_contains"]:
            expect(any(text in item for item in errors), f"{scenario['id']}: missing model error {text}: {errors[:8]}")
    for scenario in fixtures.get("c73_oracle_scenarios", []):
        ready, reasons = evaluate_c73_oracle(data, copy.deepcopy(scenario["receipt"]))
        expect(ready is scenario["expected_ready"],
               f"{scenario['id']}: C73 expected {scenario['expected_ready']}, got {ready}: {reasons}")
        for text in scenario.get("expected_reason_contains", []):
            expect(any(text in item for item in reasons),
                   f"{scenario['id']}: missing C73 reason {text}: {reasons}")
    synthetic_records = fixtures.get("catalog", {}).get("output_instances", {}).values()
    expect(all(item.get("fixture_only") is True for item in synthetic_records),
           "all synthetic output records are explicitly fixture_only")
    return failures, passes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    base = args.base.resolve()
    data, errors, checks, order, work_count = validate(base, args.workspace_root)
    full_validation_errors = list(errors)
    self_test_failures: list[str] = []
    self_test_passes: list[str] = []
    if args.self_test:
        self_test_failures, self_test_passes = run_self_tests(base)
        errors.extend(f"synthetic_self_test:{item}" for item in self_test_failures)
    scope = ("S1_full_plan_validation_plus_synthetic_readiness_self_tests"
             if args.self_test else "S1_plan_structure_links_and_traceability_only")
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "design_version": data["design_version"],
        "validation_scope": scope,
        "status": "passed" if not errors else "failed", "implementation_status": data["implementation_status"],
        "requirements": len(data["requirements"]), "tasks": len(data["tasks"]), "work_packages": work_count,
        "case_entries": len(data["test_cases"]), "non_executable_parent_families": len(FAMILY_IDS),
        "actual_test_instances": len(data.get("test_instances", [])),
        "actual_output_instances": len(data.get("output_instances", [])), "business_cases_executed": 0,
        "build_install_migration_database_ssh_runtime_or_field_actions": False,
        "full_validation": {
            "status": "passed" if not full_validation_errors else "failed",
            "checks_passed": len(checks),
            "errors": full_validation_errors,
        },
        "synthetic_self_tests": {
            "requested": args.self_test,
            "status": ("passed" if not self_test_failures else "failed") if args.self_test else "not_requested",
            "checks_passed": len(self_test_passes),
            "errors": self_test_failures,
        },
        "checks_passed": len(checks) + len(self_test_passes), "errors": errors, "dependency_order": order,
        "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(base.iterdir()) if path.is_file()},
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in {"files", "dependency_order"}},
                     ensure_ascii=False, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
