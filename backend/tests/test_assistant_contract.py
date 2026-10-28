"""Explicit assistant contract; governance never inherits arbitrary tool dispatch."""
import ast
import inspect
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

from app.api.v1 import assistant
from app.services.access_control import action_for_request


EXPECTED = {
    ("GET", "/api/v1/assistant/capabilities"),
    ("GET", "/api/v1/assistant/capabilities/{capability}"),
    ("POST", "/api/v1/assistant/dispatch-intent"),
    ("GET", "/api/v1/assistant/dispatch-intent/{request_id}"),
    ("GET", "/api/v1/assistant/dispatch-metrics"),
    ("POST", "/api/v1/assistant/plans"),
    ("GET", "/api/v1/assistant/plans"),
    ("GET", "/api/v1/assistant/plans/{plan_id}"),
    ("POST", "/api/v1/assistant/plans/{plan_id}/execute"),
    ("GET", "/api/v1/assistant/plans/{plan_id}/validation"),
    ("POST", "/api/v1/assistant/plans/{plan_id}/revisions"),
    ("GET", "/api/v1/assistant/plans/{plan_id}/approval"),
    ("POST", "/api/v1/assistant/plans/{plan_id}/approval/request"),
    ("POST", "/api/v1/assistant/plans/{plan_id}/approval/approve"),
    ("POST", "/api/v1/assistant/plans/{plan_id}/approval/revoke"),
    ("POST", "/api/v1/assistant/batches/prepare"),
    ("POST", "/api/v1/assistant/batches"),
    ("GET", "/api/v1/assistant/batches"),
    ("GET", "/api/v1/assistant/batches/preparation"),
    ("GET", "/api/v1/assistant/batches/admission"),
    ("GET", "/api/v1/assistant/batches/{batch_id}"),
    ("POST", "/api/v1/assistant/batches/{batch_id}/cancel"),
    ("GET", "/api/v1/assistant/executions/{capability_execution_id}/control"),
    ("POST", "/api/v1/assistant/executions/{capability_execution_id}/pause"),
    ("POST", "/api/v1/assistant/executions/{capability_execution_id}/resume"),
    ("POST", "/api/v1/assistant/executions/{capability_execution_id}/stop"),
    ("POST", "/api/v1/assistant/executions/{capability_execution_id}/probe"),
    ("GET", "/api/v1/assistant/workflows"),
    ("GET", "/api/v1/assistant/workflows/preparation"),
    ("GET", "/api/v1/assistant/workflows/proposals"),
    ("POST", "/api/v1/assistant/workflows/proposals"),
    ("GET", "/api/v1/assistant/workflows/providers"),
    ("GET", "/api/v1/assistant/workflows/{workflow_id}/versions/{workflow_version}"),
    ("POST", "/api/v1/assistant/workflows/match"),
    ("POST", "/api/v1/assistant/workflows/{workflow_id}/versions/{workflow_version}/prepare"),
    ("POST", "/api/v1/assistant/workflows/plans/{plan_id}/revisions"),
}


def contract():
    app = FastAPI()
    app.include_router(assistant.router, prefix="/api/v1")
    return app.openapi()


def test_only_reviewed_routes_are_registered_and_have_an_explicit_floor():
    document = contract()
    actual = {(method.upper(), path) for path, item in document["paths"].items() for method in item}
    assert actual == EXPECTED
    for method, path in EXPECTED:
        if "/executions/" in path:
            floor = {"control": "control.probe", "pause": "control.pause", "resume": "control.resume",
                     "stop": "control.stop", "probe": "control.probe"}[path.rsplit("/", 1)[-1]]
        else:
            floor = "automation.approve" if path.endswith("/approval/approve") else "asset.read"
        assert action_for_request(method, path) == floor
    for method, suffix in (("POST", "/capabilities/sync"), ("POST", "/plans/x/approve"),
                           ("POST", "/plans/x/cancel"), ("DELETE", "/plans/x"),
                           ("GET", "/future"), ("POST", "/plans/x/execute/future")):
        assert action_for_request(method, "/api/v1/assistant" + suffix) is None
    assert action_for_request("POST", "/api/v1/assistant/plans/x/validation") is None
    assert action_for_request("GET", "/api/v1/assistant/plans/x/validation/future") is None
    assert action_for_request("POST", "/api/v1/assistant/workflows/x/versions/1/execute") is None
    assert action_for_request("GET", "/api/v1/assistant/workflows/x/versions/1/future") is None


def test_inline_input_contract_matches_strict_model_and_reference_alias():
    paths = contract()["paths"]
    create = paths["/api/v1/assistant/plans"]["post"]
    body = create["requestBody"]["content"]["application/json"]["schema"]
    assert body == jsonable_encoder(assistant._request_schema(assistant.PlanCreateInput), exclude_none=True)
    assert "$ref" not in json.dumps(body) and "$defs" not in body
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {"capability_id", "definition_version"}
    steps = next(item for item in body["properties"]["steps"]["anyOf"] if item["type"] == "array")
    ref = steps["items"]["properties"]["input_refs"]["additionalProperties"]
    assert "schema" in ref["properties"] and "source_schema" not in ref["properties"]
    assert steps["maxItems"] == 20 and ref["additionalProperties"] is False
    assert {"200", "201", "422"} <= create["responses"].keys()
    header = next(item for item in create["parameters"] if item["name"] == "Idempotency-Key")
    assert header["schema"]["maxLength"] == 128
    execute = paths["/api/v1/assistant/plans/{plan_id}/execute"]["post"]
    assert execute["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "object", "additionalProperties": False, "maxProperties": 0,
    }


def test_frozen_baseline_contains_exact_assistant_contract_and_no_dynamic_dispatch():
    root = Path(__file__).resolve().parents[2]
    frozen = json.loads((root / "contracts/baselines/openapi-v1.json").read_text())
    for path, item in contract()["paths"].items():
        assert frozen["paths"][path] == item
    tree = ast.parse(inspect.getsource(assistant))
    names = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not names & {"eval", "exec", "__import__"}
    assert not any(isinstance(node, ast.Import) and any(alias.name == "importlib" for alias in node.names) for node in ast.walk(tree))
